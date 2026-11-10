#!/usr/bin/env python3
"""
fix_import.py — Interactively fix errors in an NX-ActivityLog import.json file.

Shows each fix before applying and asks for confirmation.

Usage:
    python3 fix_import.py <import.json> [--warnings]

Flags:
    --warnings  Also fix warnings (default: errors only)

A backup of the original is always saved as <import.json>.bak before any fixes are applied.

Exit code: 0 on success, 1 if user declines any fix, 2 on error.
"""

import os
import shutil
import sys

from nxal_codec import (
    calculate_playtime,
    count_launches,
    deduplicate_events,
    find_duplicate_events,
    find_first_last_launch,
    find_orphaned_lost_focus,
    format_timestamp,
    is_sorted,
    load_json,
    write_export_json,
)

FIX_WARNINGS = "--warnings" in sys.argv


def fix_focus_errors(events, title_name):
    """Fix unbalanced focus events by inserting missing Gained Focus."""
    indices = find_orphaned_lost_focus(events)
    if not indices:
        return events, False

    print(f"\n  Found {len(indices)} 'Lost Focus' event(s) without preceding 'Gained Focus':")
    for idx in indices[:5]:
        ev = events[idx]
        print(f"    [{idx}] Lost Focus at {format_timestamp(ev['clockTimestamp'])} ({ev['clockTimestamp']})")
    if len(indices) > 5:
        print(f"    ... and {len(indices) - 5} more")

    print("\n  Fix: Insert 'Gained Focus' before each orphaned 'Lost Focus'")
    print("       Timestamp: previous event timestamp + 1 second")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    fixed_events = []
    last_focus_type = None

    for i, ev in enumerate(events):
        if ev["type"] == "Lost Focus" and last_focus_type != "Gained Focus":
            insert_ts = events[i - 1]["clockTimestamp"] + 1 if i > 0 else ev["clockTimestamp"] - 1
            insert_steady = events[i - 1]["steadyTimestamp"] + 1 if i > 0 else ev["steadyTimestamp"] - 1

            fixed_events.append({
                "clockTimestamp": insert_ts,
                "steadyTimestamp": insert_steady,
                "type": "Gained Focus"
            })
            print(f"    + Inserted Gained Focus at {format_timestamp(insert_ts)} ({insert_ts})")

        fixed_events.append(ev)
        last_focus_type = ev["type"]

    return fixed_events, True


def fix_out_of_order(events):
    """Sort events by clockTimestamp."""
    print("\n  Events are not sorted by clockTimestamp")
    print("\n  Fix: Sort all events by clockTimestamp")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    events.sort(key=lambda e: e["clockTimestamp"])
    print(f"    + Sorted {len(events)} events by clockTimestamp")
    return events, True


# Removal uses the strict 3-field NXAL identity key (same as merge_exports).
# find_duplicate_events below is only the broad 2-field *detection* heuristic
# used to surface potential PDM artifacts to the user; actually deleting events
# must require steadyTimestamp too, or we risk removing genuinely distinct
# events (e.g. same wall-clock second across a reboot, differing in steady).
REMOVE_DUP_KEY = ("type", "clockTimestamp", "steadyTimestamp")


def fix_duplicate_events(events):
    """Remove duplicate events.

    Only events identical under the strict 3-field NXAL identity key are
    removed, so distinct events that merely share (type, clockTimestamp)
    are never deleted.
    """
    duplicates = find_duplicate_events(events)
    if not duplicates:
        return events, False

    # Detect what WOULD be flagged by the broad key, then compute how many
    # are truly removable under the strict key (keeps first occurrence).
    pre_count = len(events)
    fixed_events = deduplicate_events(events, REMOVE_DUP_KEY)
    removed = pre_count - len(fixed_events)

    print(f"\n  Found {len(duplicates)} potential duplicate event(s) (PDM artifact):")
    for idx, _, ev in duplicates[:5]:
        print(f"    [{idx}] {ev['type']} at {format_timestamp(ev['clockTimestamp'])} ({ev['clockTimestamp']})")
    if len(duplicates) > 5:
        print(f"    ... and {len(duplicates) - 5} more")

    if removed == 0:
        print("\n  None are true duplicates: all differ in steadyTimestamp, so they are")
        print("  distinct events (e.g. across a reboot) and none are removed.")
        return events, False

    if removed < len(duplicates):
        print(f"\n  Note: {len(duplicates) - removed} flagged event(s) differ in steadyTimestamp and are")
        print("        distinct events, so they are NOT removed.")

    print(f"\n  Fix: Remove the {removed} event(s) that are true duplicates "
          f"(same type, clockTimestamp, steadyTimestamp)")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    print(f"    + Removed {removed} duplicate event(s)")
    return fixed_events, True


def find_null_fields(events):
    """Find events with missing required fields."""
    nulls = []

    for i, ev in enumerate(events):
        if not isinstance(ev.get("clockTimestamp"), int):
            nulls.append((i, "clockTimestamp"))
        elif not isinstance(ev.get("steadyTimestamp"), int):
            nulls.append((i, "steadyTimestamp"))
        elif not isinstance(ev.get("type"), str):
            nulls.append((i, "type"))

    return nulls


def fix_null_fields(events):
    """Remove events with null fields."""
    nulls = find_null_fields(events)
    if not nulls:
        return events, False

    print(f"\n  Found {len(nulls)} event(s) with missing fields:")
    for idx, field in nulls[:5]:
        ev = events[idx]
        print(f"    [{idx}] Missing '{field}' (type={ev.get('type', 'N/A')}, ts={ev.get('clockTimestamp', 'N/A')})")
    if len(nulls) > 5:
        print(f"    ... and {len(nulls) - 5} more")

    print("\n  Fix: Remove events with missing required fields")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    null_indices = set(idx for idx, _ in nulls)
    fixed_events = [ev for i, ev in enumerate(events) if i not in null_indices]
    print(f"    + Removed {len(null_indices)} event(s) with missing fields")
    return fixed_events, True


def fix_launches(summary, events):
    """Recalculate launches count."""
    declared = summary.get("launches", 0)
    actual = count_launches(events)

    if declared == actual:
        return summary, False

    print(f"\n  Launches mismatch: declared={declared}, actual={actual}")
    print(f"\n  Fix: Update launches to {actual}")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return summary, False

    summary["launches"] = actual
    print(f"    + Updated launches: {declared} → {actual}")
    return summary, True


def fix_playtime(summary, events):
    """Recalculate playtime."""
    declared = summary.get("playtime", 0)

    calc = calculate_playtime(events)

    if declared == calc:
        return summary, False

    if calc > 0:
        diff_pct = abs(declared - calc) / calc * 100
        diff_str = f"(diff={diff_pct:.1f}%)"
    else:
        diff_str = f"(declared={declared}s, no focus time in events)"
    print(f"\n  Playtime mismatch: declared={declared}s, calculated={calc}s {diff_str}")
    print(f"\n  Fix: Update playtime to {calc}s")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return summary, False

    summary["playtime"] = calc
    print(f"    + Updated playtime: {declared}s → {calc}s")
    return summary, True


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Usage: {sys.argv[0]} <import.json> [--warnings]")
        print("  --warnings  Also fix warnings (default: errors only)")
        sys.exit(2)

    import_path = sys.argv[1]

    if not os.path.exists(import_path):
        print(f"Error: {import_path} not found")
        sys.exit(1)

    try:
        data = load_json(import_path)
    except Exception as e:
        print(f"Error loading {import_path}: {e}")
        sys.exit(1)

    backup_path = import_path + ".bak"
    shutil.copy2(import_path, backup_path)
    print(f"Backup saved: {backup_path}")
    print()

    print(f"Fixing: {import_path}")
    if FIX_WARNINGS:
        print("Mode: errors + warnings")
    else:
        print("Mode: errors only (use --warnings to also fix warnings)")
    print()

    users = data.get("users", [])
    fixes_applied = 0

    for user in users:
        name = user.get("name", "Unknown")
        titles = user.get("titles", [])

        if not titles:
            continue

        print(f"== User '{name}' ==")

        first, last = find_first_last_launch(titles)
        if first:
            print(f"  First session: {format_timestamp(first['timestamp'])} - {first['game']}")
        if last:
            print(f"  Last session:  {format_timestamp(last['timestamp'])} - {last['game']}")
        print()

        for title in titles:
            title_name = title.get("name", "Unknown")
            events = title.get("events", [])
            summary = title.get("summary", {})

            if not events:
                continue

            print(f"  == {title_name} ==")

            title_fixes = 0
            fixed_launches = False
            fixed_playtime = False

            changed = True
            while changed:
                changed = False

                if FIX_WARNINGS:
                    focus_indices = find_orphaned_lost_focus(events)
                    if focus_indices:
                        events, fixed = fix_focus_errors(events, title_name)
                        if fixed:
                            title_fixes += 1
                            fixes_applied += 1
                            changed = True
                            continue

                if FIX_WARNINGS and not is_sorted(events):
                    events, fixed = fix_out_of_order(events)
                    if fixed:
                        title_fixes += 1
                        fixes_applied += 1
                        changed = True
                        continue

                if FIX_WARNINGS:
                    duplicates = find_duplicate_events(events)
                    if duplicates:
                        events, fixed = fix_duplicate_events(events)
                        if fixed:
                            title_fixes += 1
                            fixes_applied += 1
                            changed = True
                            continue

                nulls = find_null_fields(events)
                if nulls:
                    events, fixed = fix_null_fields(events)
                    if fixed:
                        title_fixes += 1
                        fixes_applied += 1
                        changed = True
                        continue

                summary, fixed_launches = fix_launches(summary, events)
                if fixed_launches:
                    title_fixes += 1
                    fixes_applied += 1
                    changed = True
                    continue

                if FIX_WARNINGS:
                    summary, fixed_playtime = fix_playtime(summary, events)
                    if fixed_playtime:
                        title_fixes += 1
                        fixes_applied += 1
                        changed = True
                        continue

            title["events"] = events
            title["summary"] = summary

            if title_fixes == 0 and not find_null_fields(events):
                print("    No errors found")

        print()

    if fixes_applied > 0:
        write_export_json(import_path, data)
        print(f"Fixed file saved: {import_path}")
        print(f"\nApplied {fixes_applied} fix(es)")
    else:
        print("No fixes applied")


if __name__ == "__main__":
    main()
