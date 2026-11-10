#!/usr/bin/env python3
"""
verify_import.py — Verify that importedData.json was correctly derived from import.json.

Checks:
  - User matching by name (NXAL uses display name, not ID, to match users)
  - User ID update detection (warns if ID changed, e.g. cross-console migration)
  - Game ID conversions (hex string → int)
  - Event type mappings (string → int 0-5)
  - Timestamp preservation (clockTimestamp, steadyTimestamp)
  - Summary field preservation (firstPlayed, lastPlayed, launches, playtime)
  - Game name preservation
  - Correct skipping of users with 0 titles
  - Metadata field correspondence

Usage:
    python3 verify_import.py <import.json> <importedData.json>

Exit code: 0 if all checks pass (warnings OK), 1 if any error is found.
"""

import sys

from nxal_codec import (
    EVENT_TYPE_MAP,
    SUMMARY_FIELDS,
    VerificationResult,
    hex_to_game_id,
    hex_to_user_id_array,
    load_json,
)


def verify_metadata(import_data, imported_data, result):
    print("== Metadata ==")

    if "importString" in imported_data:
        result.ok(f"importString present: \"{imported_data['importString']}\"")
    else:
        result.error("importString missing from importedData")

    if "importTimestamp" in imported_data:
        result.ok(f"importTimestamp present: {imported_data['importTimestamp']}")
    else:
        result.error("importTimestamp missing from importedData")

    if "importVersion" in imported_data:
        if imported_data["importVersion"] == import_data.get("exportVersion"):
            result.ok(f"Version matches: {imported_data['importVersion']}")
        else:
            result.error(
                f"Version mismatch: import={imported_data['importVersion']} "
                f"!= export={import_data.get('exportVersion')}"
            )
    else:
        result.error("importVersion missing from importedData")

    if "aWarning" in imported_data:
        result.ok(f"aWarning present: \"{imported_data['aWarning']}\"")
    else:
        result.warn("aWarning field missing from importedData (not critical)")

    for old_field in ["exportString", "exportTimestamp", "exportVersion"]:
        if old_field in imported_data:
            result.warn(f"'{old_field}' still present in importedData (should be removed)")
        else:
            result.ok(f"'{old_field}' correctly removed from importedData")


def verify_users(import_data, imported_data, result):
    print("\n== User Matching ==")

    import_users = import_data.get("users", [])
    imported_users = imported_data.get("users", [])

    imported_by_id = {}
    for u in imported_users:
        imported_by_id[tuple(u["id"])] = u

    imported_by_games = {}
    for u in imported_users:
        game_names = frozenset(t.get("name") for t in u.get("titles", []) if t.get("name"))
        if game_names:
            imported_by_games[game_names] = u

    matched_imported_ids = set()

    expected_count = 0
    for imp_user in import_users:
        hex_id = imp_user["id"]
        name = imp_user.get("name", "(unknown)")
        titles = imp_user.get("titles", [])

        expected_id_array = hex_to_user_id_array(hex_id)

        if len(titles) == 0:
            if tuple(expected_id_array) in imported_by_id:
                result.error(
                    f"User '{name}' has 0 titles but IS present in importedData"
                )
            else:
                result.ok(
                    f"User '{name}' (hex={hex_id}) has 0 titles, correctly skipped"
                )
            continue

        expected_count += 1

        impd_user = None
        match_method = None

        if tuple(expected_id_array) in imported_by_id:
            impd_user = imported_by_id[tuple(expected_id_array)]
            match_method = "id"
        else:
            imp_game_names = frozenset(t.get("name") for t in titles if t.get("name"))
            if imp_game_names in imported_by_games:
                impd_user = imported_by_games[imp_game_names]
                match_method = "name"

        if impd_user is None:
            result.error(
                f"User '{name}' (hex={hex_id}) missing from importedData"
            )
            continue

        actual_id_array = impd_user["id"]
        matched_imported_ids.add(tuple(actual_id_array))

        if match_method == "id":
            result.ok(f"User '{name}' matched by ID: {hex_id}")
        else:
            impd_hex = format(actual_id_array[0], "016X") + format(actual_id_array[1], "016X")
            result.warn(
                f"User '{name}' matched by name (ID changed: "
                f"{hex_id} -> {impd_hex})"
            )

        if "name" in impd_user:
            result.warn(
                f"User '{name}' still has 'name' field in importedData (expected removed)"
            )
        else:
            result.ok(f"User '{name}' 'name' field correctly removed")

        imp_titles = imp_user.get("titles", [])
        impd_titles = impd_user.get("titles", [])

        if len(imp_titles) != len(impd_titles):
            result.error(
                f"User '{name}' title count mismatch: "
                f"import={len(imp_titles)} imported={len(impd_titles)}"
            )
            continue
        else:
            result.ok(f"User '{name}' title count matches: {len(imp_titles)}")

        for i, (imp_title, impd_title) in enumerate(zip(imp_titles, impd_titles, strict=False)):
            verify_title(name, i, imp_title, impd_title, result)

    unmatched = len(imported_users) - len(matched_imported_ids)
    if unmatched > 0:
        result.warn(
            f"importedData has {unmatched} user(s) not matched by any import user"
        )


def verify_title(user_name, index, imp_title, impd_title, result):
    title_name = imp_title.get("name", f"Title#{index}")
    prefix = f"User '{user_name}' / '{title_name}'"

    if imp_title.get("name") == impd_title.get("name"):
        result.ok(f"{prefix}: name matches")
    else:
        result.error(
            f"{prefix}: name mismatch: import='{imp_title.get('name')}' "
            f"imported='{impd_title.get('name')}'"
        )

    hex_id = imp_title.get("id", "")
    expected_id = hex_to_game_id(hex_id)
    actual_id = impd_title.get("id")
    if expected_id == actual_id:
        result.ok(f"{prefix}: id matches ({hex_id} -> {actual_id})")
    else:
        result.error(
            f"{prefix}: id mismatch: {hex_id} -> expected {expected_id} got {actual_id}"
        )

    imp_summary = imp_title.get("summary", {})
    impd_summary = impd_title.get("summary", {})
    for field in SUMMARY_FIELDS:
        imp_val = imp_summary.get(field)
        impd_val = impd_summary.get(field)
        if imp_val == impd_val:
            result.ok(f"{prefix}: summary.{field} = {imp_val}")
        else:
            result.error(
                f"{prefix}: summary.{field} mismatch: import={imp_val} imported={impd_val}"
            )

    imp_events = imp_title.get("events", [])
    impd_events = impd_title.get("events", [])

    if len(imp_events) != len(impd_events):
        result.error(
            f"{prefix}: event count mismatch: import={len(imp_events)} "
            f"imported={len(impd_events)}"
        )
        return

    result.ok(f"{prefix}: event count matches ({len(imp_events)})")

    for j, (imp_ev, impd_ev) in enumerate(zip(imp_events, impd_events, strict=False)):
        verify_event(user_name, title_name, j, imp_ev, impd_ev, result)


def verify_event(user_name, title_name, index, imp_ev, impd_ev, result):
    prefix = f"User '{user_name}' / '{title_name}' / Event#{index}"

    if imp_ev.get("clockTimestamp") != impd_ev.get("clockTimestamp"):
        result.error(
            f"{prefix}: clockTimestamp mismatch: "
            f"import={imp_ev.get('clockTimestamp')} imported={impd_ev.get('clockTimestamp')}"
        )

    if imp_ev.get("steadyTimestamp") != impd_ev.get("steadyTimestamp"):
        result.error(
            f"{prefix}: steadyTimestamp mismatch: "
            f"import={imp_ev.get('steadyTimestamp')} imported={impd_ev.get('steadyTimestamp')}"
        )

    imp_type_str = imp_ev.get("type", "")
    expected_type_int = EVENT_TYPE_MAP.get(imp_type_str)
    actual_type_int = impd_ev.get("type")

    if expected_type_int is None:
        result.error(f"{prefix}: unknown event type string '{imp_type_str}'")
        return

    if expected_type_int != actual_type_int:
        result.error(
            f"{prefix}: event type mismatch: '{imp_type_str}' -> "
            f"expected {expected_type_int} got {actual_type_int}"
        )


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <import.json> <importedData.json>")
        sys.exit(2)

    import_path = sys.argv[1]
    imported_path = sys.argv[2]

    try:
        import_data = load_json(import_path)
    except Exception as e:
        print(f"Error loading {import_path}: {e}")
        sys.exit(2)

    try:
        imported_data = load_json(imported_path)
    except Exception as e:
        print(f"Error loading {imported_path}: {e}")
        sys.exit(2)

    print(f"Verifying: {import_path} -> {imported_path}\n")

    result = VerificationResult()

    verify_metadata(import_data, imported_data, result)
    verify_users(import_data, imported_data, result)

    print("\n" + "=" * 60)
    result.summary()
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
