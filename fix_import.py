#!/usr/bin/env python3
"""
fix_import.py — Interactively fix errors in an NX-ActivityLog import.json file.

Shows each fix before applying and asks for confirmation.

Usage:
    python3 fix_import.py <import.json> [--warnings]

Flags:
    --warnings  Also fix warnings (default: errors only)

Exit code: 0 on success, 1 if user declines any fix, 2 on error.
"""

import sys
import json
import os
from datetime import datetime

from nxal_common import load_json

FIX_WARNINGS = "--warnings" in sys.argv


def format_ts(ts):
    """Format Unix timestamp to human-readable datetime."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def find_first_last_sessions(titles):
    """Find first and last Launched events across all titles."""
    first = None
    last = None

    for title in titles:
        for ev in title.get("events", []):
            if ev["type"] == "Launched":
                ts = ev["clockTimestamp"]
                info = {"game": title.get("name", "Unknown"), "timestamp": ts}

                if first is None or ts < first["timestamp"]:
                    first = info
                if last is None or ts > last["timestamp"]:
                    last = info

    return first, last


def find_focus_errors(events):
    """Find Lost Focus events without preceding Gained Focus."""
    errors = []
    focus_depth = 0

    for i, ev in enumerate(events):
        if ev["type"] == "Gained Focus":
            focus_depth += 1
        elif ev["type"] == "Lost Focus":
            if focus_depth > 0:
                focus_depth -= 1
            else:
                errors.append(i)

    return errors


def fix_focus_errors(events, title_name):
    """Fix unbalanced focus events by inserting missing Gained Focus."""
    indices = find_focus_errors(events)
    if not indices:
        return events, False

    print(f"\n  Found {len(indices)} 'Lost Focus' event(s) without preceding 'Gained Focus':")
    for idx in indices[:5]:
        ev = events[idx]
        print(f"    [{idx}] Lost Focus at {format_ts(ev['clockTimestamp'])} ({ev['clockTimestamp']})")
    if len(indices) > 5:
        print(f"    ... and {len(indices) - 5} more")

    print(f"\n  Fix: Insert 'Gained Focus' before each orphaned 'Lost Focus'")
    print(f"       Timestamp: previous event timestamp + 1 second")

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
            print(f"    + Inserted Gained Focus at {format_ts(insert_ts)} ({insert_ts})")

        fixed_events.append(ev)
        last_focus_type = ev["type"]

    return fixed_events, True


def find_out_of_order(events):
    """Find events not sorted by clockTimestamp."""
    timestamps = [ev["clockTimestamp"] for ev in events]
    return timestamps != sorted(timestamps)


def fix_out_of_order(events):
    """Sort events by clockTimestamp."""
    print(f"\n  Events are not sorted by clockTimestamp")
    print(f"\n  Fix: Sort all events by clockTimestamp")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    events.sort(key=lambda e: e["clockTimestamp"])
    print(f"    + Sorted {len(events)} events by clockTimestamp")
    return events, True


def find_duplicate_events(events):
    """Find duplicate events (same type and clockTimestamp)."""
    seen = {}
    duplicates = []

    for i, ev in enumerate(events):
        key = (ev["type"], ev["clockTimestamp"])
        if key in seen:
            duplicates.append((i, ev))
        else:
            seen[key] = i

    return duplicates


def fix_duplicate_events(events):
    """Remove duplicate events."""
    duplicates = find_duplicate_events(events)
    if not duplicates:
        return events, False

    print(f"\n  Found {len(duplicates)} duplicate event(s):")
    for idx, ev in duplicates[:5]:
        print(f"    [{idx}] {ev['type']} at {format_ts(ev['clockTimestamp'])} ({ev['clockTimestamp']})")
    if len(duplicates) > 5:
        print(f"    ... and {len(duplicates) - 5} more")

    print(f"\n  Fix: Remove duplicate events (keep first occurrence)")

    confirm = input("\n  Apply fix? [y/N]: ").strip().lower()
    if confirm != "y":
        return events, False

    seen = set()
    fixed_events = []
    removed = 0

    for ev in events:
        key = (ev["type"], ev["clockTimestamp"])
        if key not in seen:
            seen.add(key)
            fixed_events.append(ev)
        else:
            removed += 1

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

    print(f"\n  Fix: Remove events with missing required fields")

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
    actual = sum(1 for ev in events if ev["type"] == "Launched")

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

    calc = 0
    last_start = None
    for ev in events:
        if ev["type"] == "Gained Focus":
            last_start = ev["clockTimestamp"]
        elif ev["type"] == "Lost Focus" and last_start is not None:
            calc += ev["clockTimestamp"] - last_start
            last_start = None

    if declared == calc:
        return summary, False

    diff_pct = abs(declared - calc) / calc * 100 if calc > 0 else 0
    print(f"\n  Playtime mismatch: declared={declared}s, calculated={calc}s (diff={diff_pct:.1f}%)")
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
        print(f"  --warnings  Also fix warnings (default: errors only)")
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

        first, last = find_first_last_sessions(titles)
        if first:
            print(f"  First session: {format_ts(first['timestamp'])} - {first['game']}")
        if last:
            print(f"  Last session:  {format_ts(last['timestamp'])} - {last['game']}")
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

                focus_indices = find_focus_errors(events)
                if focus_indices:
                    events, fixed = fix_focus_errors(events, title_name)
                    if fixed:
                        title_fixes += 1
                        fixes_applied += 1
                        changed = True
                        continue

                if FIX_WARNINGS and find_out_of_order(events):
                    events, fixed = fix_out_of_order(events)
                    if fixed:
                        title_fixes += 1
                        fixes_applied += 1
                        changed = True
                        continue

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

            if title_fixes == 0 and not any([
                find_focus_errors(events),
                find_out_of_order(events),
                find_duplicate_events(events),
                find_null_fields(events),
            ]):
                print(f"    No errors found")

        print()

    if fixes_applied > 0:
        backup_path = import_path + ".bak"
        with open(backup_path, "w") as f:
            json.dump(data, f, indent=4)
        print(f"Backup saved: {backup_path}")

        with open(import_path, "w") as f:
            json.dump(data, f, indent=4)
        print(f"Fixed file saved: {import_path}")
        print(f"\nApplied {fixes_applied} fix(es)")
    else:
        print("No fixes applied")


if __name__ == "__main__":
    main()
