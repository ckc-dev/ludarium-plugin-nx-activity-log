#!/usr/bin/env python3
"""
validate_import.py — Validate an NX-ActivityLog import.json file for structural and data integrity.

Checks:
  - Metadata structure (exportString, exportTimestamp, exportVersion, users)
  - User structure (id: 32-char hex, name: string, titles: array)
  - Title structure (id: 16-char hex, name: string, summary: object, events: array)
  - Event structure (clockTimestamp: int, steadyTimestamp: int, type: valid string)
  - No duplicate user IDs across users
  - No duplicate title IDs within a user
  - All event type strings are valid
  - Timestamps are reasonable Unix values (2017-2030)
  - firstPlayed <= lastPlayed for each title
  - firstPlayed usually matches the first "Account Login" event
  - lastPlayed usually matches the last "Lost Focus" event (PDM can differ)
  - Events sorted by clockTimestamp (non-decreasing)
  - launches == count of "Launched" events
  - playtime consistent with focus event pairs (warn if >5% off)
  - Logical event sequences (Launched before Closed, etc.)

Usage:
    python3 validate_import.py <import.json>

Exit code: 0 if all checks pass (warnings OK), 1 if any error is found.
"""

import sys

from nxal_codec import (
    GAME_ID_HEX_LENGTH,
    USER_ID_HEX_LENGTH,
    VALID_EVENT_TYPES,
    VerificationResult,
    calculate_playtime,
    find_duplicate_events,
    find_first_last_launch,
    find_orphaned_lost_focus,
    format_timestamp,
    is_sorted,
    is_valid_hex,
    load_json,
)

REASONABLE_TS_MIN = 1483228800  # 2017-01-01 (Switch launch year)
REASONABLE_TS_MAX = 1893456000  # 2030-01-01


def validate_metadata(data, result):
    print("== Metadata ==")

    required = ["exportString", "exportTimestamp", "exportVersion", "users"]
    for field in required:
        if field in data:
            result.ok(f"'{field}' present")
        else:
            result.error(f"'{field}' missing from top level")

    if "exportString" in data:
        val = data["exportString"]
        if isinstance(val, str) and len(val) > 0:
            result.ok(f"exportString is non-empty: \"{val}\"")
        else:
            result.error("exportString is empty or not a string")

    if "exportTimestamp" in data:
        val = data["exportTimestamp"]
        if isinstance(val, int) and REASONABLE_TS_MIN <= val <= REASONABLE_TS_MAX:
            result.ok(f"exportTimestamp is reasonable: {val}")
        elif isinstance(val, int):
            result.warn(f"exportTimestamp is outside expected range: {val}")
        else:
            result.error(f"exportTimestamp is not an integer: {type(val).__name__}")

    if "exportVersion" in data:
        val = data["exportVersion"]
        if isinstance(val, str) and len(val) > 0:
            result.ok(f"exportVersion: {val}")
        else:
            result.error("exportVersion is empty or not a string")

    extra = set(data.keys()) - {"exportString", "exportTimestamp", "exportVersion", "users"}
    for field in sorted(extra):
        result.warn(f"Unexpected top-level field: '{field}'")


def validate_users(data, result):
    print("\n== Users ==")

    users = data.get("users")
    if not isinstance(users, list):
        result.error(f"'users' is not an array: {type(users).__name__}")
        return

    if len(users) == 0:
        result.warn("'users' array is empty")
        return

    result.ok(f"users array has {len(users)} entries")

    seen_ids = {}
    for i, user in enumerate(users):
        name = user.get("name", f"User#{i}")
        prefix = f"User '{name}'"

        # Required fields
        for field in ["id", "name", "titles"]:
            if field not in user:
                result.error(f"{prefix}: missing field '{field}'")

        # ID format
        uid = user.get("id", "")
        if is_valid_hex(uid, USER_ID_HEX_LENGTH):
            result.ok(f"{prefix}: id is valid 32-char hex")
        else:
            result.error(f"{prefix}: id is not a valid 32-char hex string: '{uid}'")

        # Duplicate ID check
        if uid in seen_ids:
            result.error(f"{prefix}: duplicate user id (also in {seen_ids[uid]})")
        else:
            seen_ids[uid] = name

        # Name
        name_val = user.get("name")
        if isinstance(name_val, str) and len(name_val) > 0:
            result.ok(f"{prefix}: name is non-empty")
        else:
            result.error(f"{prefix}: name is empty or not a string")

        # Titles
        titles = user.get("titles")
        if isinstance(titles, list):
            result.ok(f"{prefix}: has {len(titles)} titles")
            validate_titles(name, titles, result)
            print_user_sessions(user)
        else:
            result.error(f"{prefix}: 'titles' is not an array")


def validate_titles(user_name, titles, result):
    seen_ids = {}
    for i, title in enumerate(titles):
        title_name = title.get("name", f"Title#{i}")
        prefix = f"User '{user_name}' / '{title_name}'"

        # Required fields
        for field in ["id", "name", "summary", "events"]:
            if field not in title:
                result.error(f"{prefix}: missing field '{field}'")

        # ID format
        tid = title.get("id", "")
        if is_valid_hex(tid, GAME_ID_HEX_LENGTH):
            pass  # ok
        else:
            result.error(f"{prefix}: id is not a valid 16-char hex string: '{tid}'")

        # Duplicate ID check
        if tid in seen_ids:
            result.error(f"{prefix}: duplicate title id (also in {seen_ids[tid]})")
        else:
            seen_ids[tid] = title_name

        # Name
        name_val = title.get("name")
        if isinstance(name_val, str) and len(name_val) > 0:
            pass  # ok
        else:
            result.error(f"{prefix}: name is empty or not a string")

        # Summary
        summary = title.get("summary")
        if isinstance(summary, dict):
            validate_summary(prefix, summary, result)
        else:
            result.error(f"{prefix}: 'summary' is not an object")

        # Events
        events = title.get("events")
        if isinstance(events, list):
            validate_events(prefix, events, summary, result)
        else:
            result.error(f"{prefix}: 'events' is not an array")


def validate_summary(prefix, summary, result):
    required = ["firstPlayed", "lastPlayed", "launches", "playtime"]
    for field in required:
        if field not in summary:
            result.error(f"{prefix}: summary missing field '{field}'")

    fp = summary.get("firstPlayed")
    lp = summary.get("lastPlayed")
    if isinstance(fp, int) and isinstance(lp, int):
        if fp <= lp:
            result.ok(f"{prefix}: summary.firstPlayed <= lastPlayed ({fp} <= {lp})")
        else:
            result.error(f"{prefix}: summary.firstPlayed > lastPlayed ({fp} > {lp})")

    launches = summary.get("launches")
    if isinstance(launches, int) and launches >= 0:
        result.ok(f"{prefix}: summary.launches = {launches}")
    elif launches is not None:
        result.error(f"{prefix}: summary.launches is not a non-negative integer: {launches}")

    playtime = summary.get("playtime")
    if isinstance(playtime, int) and playtime >= 0:
        result.ok(f"{prefix}: summary.playtime = {playtime}")
    elif playtime is not None:
        result.error(f"{prefix}: summary.playtime is not a non-negative integer: {playtime}")


def validate_events(prefix, events, summary, result):
    if len(events) == 0:
        result.warn(f"{prefix}: has 0 events")
        return

    # Check each event structure
    valid_types = True
    for i, ev in enumerate(events):
        ep = f"{prefix} / Event#{i}"

        ct = ev.get("clockTimestamp")
        st = ev.get("steadyTimestamp")
        et = ev.get("type")

        if not isinstance(ct, int):
            result.error(f"{ep}: clockTimestamp is not an integer")
            valid_types = False
        elif not (REASONABLE_TS_MIN <= ct <= REASONABLE_TS_MAX):
            result.warn(f"{ep}: clockTimestamp outside expected range: {ct}")

        if not isinstance(st, int):
            result.error(f"{ep}: steadyTimestamp is not an integer")
            valid_types = False

        if et not in VALID_EVENT_TYPES:
            result.error(f"{ep}: unknown event type: '{et}'")
            valid_types = False

    if not valid_types:
        return

    # Check for duplicate events (PDM logging artifact, not a real error)
    for i, first_idx, _ev in find_duplicate_events(events):
        result.warn(f"{prefix}: duplicate event at index {i} (same as index {first_idx})")

    # Events sorted by clockTimestamp
    if is_sorted(events):
        result.ok(f"{prefix}: events sorted by clockTimestamp")
    else:
        result.warn(f"{prefix}: events NOT sorted by clockTimestamp")

    # firstPlayed matches first "Account Login" event (NXAL behavior)
    fp = summary.get("firstPlayed") if summary else None
    if fp is not None:
        first_login = next(
            (ev["clockTimestamp"] for ev in events if ev["type"] == "Account Login"),
            None,
        )
        if first_login is not None:
            if fp == first_login:
                result.ok(f"{prefix}: summary.firstPlayed == first Account Login ({fp})")
            else:
                result.warn(
                    f"{prefix}: summary.firstPlayed ({fp}) != first Account Login ({first_login})"
                )
        else:
            result.warn(f"{prefix}: no Account Login events to validate firstPlayed against")

    # lastPlayed comes from the Nintendo PDM system API (last_timestamp_user), NOT from
    # iterating over events. It typically matches the last "Lost Focus" event (85% of titles),
    # but may differ if PDM has data from sessions not captured in the events array.
    # We warn on mismatches since they're expected in some cases.
    lp = summary.get("lastPlayed") if summary else None
    if lp is not None:
        last_focus = next(
            (ev["clockTimestamp"] for ev in reversed(events) if ev["type"] == "Lost Focus"),
            None,
        )
        if last_focus is not None:
            if lp == last_focus:
                result.ok(f"{prefix}: summary.lastPlayed == last Lost Focus ({lp})")
            else:
                result.warn(
                    f"{prefix}: summary.lastPlayed ({lp}) != last Lost Focus ({last_focus})"
                )
        else:
            last_event = events[-1].get("clockTimestamp") if events else None
            result.warn(
                f"{prefix}: no Lost Focus events, lastPlayed ({lp}) vs last event ({last_event})"
            )

    # launches == count of "Launched" events
    launch_count = sum(1 for ev in events if ev["type"] == "Launched")
    declared_launches = summary.get("launches") if summary else None
    if declared_launches is not None:
        if launch_count == declared_launches:
            result.ok(f"{prefix}: launches count matches ({launch_count})")
        else:
            result.error(
                f"{prefix}: launches mismatch: declared={declared_launches} "
                f"actual={launch_count}"
            )

    # playtime check: calculate from focus pairs
    declared_playtime = summary.get("playtime") if summary else None
    if declared_playtime is not None:
        calc_playtime = calculate_playtime(events)
        if calc_playtime > 0:
            diff_pct = abs(declared_playtime - calc_playtime) / calc_playtime * 100
            if diff_pct <= 5:
                result.ok(
                    f"{prefix}: playtime consistent (declared={declared_playtime}, "
                    f"calculated={calc_playtime}, diff={diff_pct:.1f}%)"
                )
            else:
                result.warn(
                    f"{prefix}: playtime differs by {diff_pct:.1f}% "
                    f"(declared={declared_playtime}, calculated={calc_playtime})"
                )
        elif declared_playtime == 0:
            result.ok(f"{prefix}: playtime = 0 (no focus time)")
        else:
            result.warn(
                f"{prefix}: playtime={declared_playtime} but no focus events to validate"
            )

    # Logical event sequences
    validate_event_sequences(prefix, events, result)


def print_user_sessions(user):
    """Print the first and last sessions for a user."""
    titles = user.get("titles", [])

    if not titles:
        return

    first_session, last_session = find_first_last_launch(titles)

    if first_session:
        print(f"  First session: {format_timestamp(first_session['timestamp'])} - {first_session['game']}")
    if last_session:
        print(f"  Last session:  {format_timestamp(last_session['timestamp'])} - {last_session['game']}")


def print_overall_sessions(data):
    """Print the overall first and last session across all users.

    Complements the per-user printouts in validate_users with a global
    view: the earliest and latest 'Launched' event anywhere in the export,
    including which user/account owned it.
    """
    first = None
    last = None

    for user in data.get("users", []):
        user_name = user.get("name", "Unknown")
        for title in user.get("titles", []):
            for ev in title.get("events", []):
                if ev.get("type") != "Launched":
                    continue
                ts = ev["clockTimestamp"]
                info = {
                    "timestamp": ts,
                    "game": title.get("name", "Unknown"),
                    "user": user_name,
                }
                if first is None or ts < first["timestamp"]:
                    first = info
                if last is None or ts > last["timestamp"]:
                    last = info

    print("\n== Overall Sessions ==")
    if first is None:
        print("  No sessions found")
        return
    print(f"  First session: {format_timestamp(first['timestamp'])} - {first['game']} ({first['user']})")
    print(f"  Last session:  {format_timestamp(last['timestamp'])} - {last['game']} ({last['user']})")


def validate_event_sequences(prefix, events, result):
    launched_count = 0
    closed_count = 0

    for i, ev in enumerate(events):
        et = ev["type"]

        if et == "Launched":
            launched_count += 1
        elif et == "Closed":
            closed_count += 1
            if closed_count > launched_count:
                result.error(
                    f"{prefix}: 'Closed' at event#{i} without preceding 'Launched'"
                )

    orphaned_lost = find_orphaned_lost_focus(events)

    if orphaned_lost:
        result.warn(
            f"{prefix}: {len(orphaned_lost)} 'Lost Focus' event(s) without preceding 'Gained Focus' (PDM artifact)"
        )

    if launched_count > 0:
        result.ok(
            f"{prefix}: {launched_count} launch(es), {closed_count} close(es), "
            f"focus balance OK"
        )


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <import.json>")
        sys.exit(2)

    import_path = sys.argv[1]

    try:
        data = load_json(import_path)
    except Exception as e:
        print(f"Error loading {import_path}: {e}")
        sys.exit(2)

    print(f"Validating: {import_path}\n")

    result = VerificationResult()

    validate_metadata(data, result)
    validate_users(data, result)

    print_overall_sessions(data)

    print("\n" + "=" * 60)
    result.summary()
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
