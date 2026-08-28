#!/usr/bin/env python3
"""
deduplicate_exports.py — Remove overlapping sessions from multiple NXAL exports.

Takes multiple export JSON files, identifies sessions that appear in multiple
exports, and keeps each session only in the export that "owns" it. The result
is a set of incremental exports where each file contains only unique sessions.

Ownership rules:
  - If any export has the Closed event for a session → oldest such export owns it
  - If no export has Closed → oldest export that has Launched owns it

Usage:
    python3 deduplicate_exports.py <input1.json> <input2.json> [...]

Each input is backed up as <name>.json.bak, then overwritten with deduplicated data.

Exit code: 0 on success, 1 on error.
"""

import sys
import os
import shutil

from nxal_common import (
    calculate_playtime,
    count_launches,
    event_key,
    first_last_from_events,
    load_json,
    sort_events,
    write_export_json,
)

# Session identity uses the same 3-field NXAL key as merge_exports
# (type, clockTimestamp, steadyTimestamp) applied to the session's
# 'Launched' marker. This makes deduplicate_exports agree with
# merge_exports about which events are the "same", so that
# merge(originalExports) and merge(deduplicatedExports) converge to the
# same content. Two sessions whose Launched events differ in
# steadyTimestamp are distinct (e.g. across a reboot) and are both kept.
SESSION_KEY_FIELDS = ("type", "clockTimestamp", "steadyTimestamp")


def extract_sessions(events):
    """Extract sessions from an events array.

    A session starts with 'Launched' and ends with 'Closed', another 'Launched',
    or the end of the events array.

    Returns list of dicts:
        {
            "start": clockTimestamp of Launched,
            "end": clockTimestamp of Closed (or None if incomplete),
            "events": [all events in this session],
            "complete": bool
        }
    """
    sessions = []
    current = None

    for ev in events:
        et = ev.get("type")

        if et == "Launched":
            if current is not None:
                current["complete"] = False
                sessions.append(current)
            current = {
                "start": ev["clockTimestamp"],
                "end": None,
                "events": [ev],
                "complete": False,
            }
        elif et == "Closed" and current is not None:
            current["events"].append(ev)
            current["end"] = ev["clockTimestamp"]
            current["complete"] = True
            sessions.append(current)
            current = None
        elif current is not None:
            current["events"].append(ev)

    if current is not None:
        current["complete"] = False
        sessions.append(current)

    return sessions


def session_key(session, title_id, user_id):
    """Unique key for a session.

    Uses the 3-field NXAL identity (SESSION_KEY_FIELDS) of the session's
    'Launched' event, combined with title and user IDs. This matches how
    merge_exports decides two events are the same, so dedup and merge
    agree and merge(dedup(x)) converges with merge(x).
    """
    launched = session["events"][0]
    return (title_id, user_id, event_key(launched, SESSION_KEY_FIELDS))


def build_session_index(exports):
    """Build an index of all sessions across all exports.

    Returns dict: session_key -> list of (export_idx, session, has_closed)
    """
    index = {}

    for export_idx, (_, data) in enumerate(exports):
        for user in data.get("users", []):
            user_id = user.get("id")
            for title in user.get("titles", []):
                title_id = title.get("id")
                sessions = extract_sessions(title.get("events", []))
                for sess in sessions:
                    key = session_key(sess, title_id, user_id)
                    if key not in index:
                        index[key] = []
                    index[key].append((export_idx, sess, sess["complete"]))

    return index


def determine_ownership(session_index, num_exports):
    """Determine which export owns each session.

    Rules:
      - If any export has Closed → oldest such export owns it
      - If no export has Closed → oldest export that has Launched owns it

    Returns dict: session_key -> owning export_idx
    """
    ownership = {}

    for key, entries in session_index.items():
        exports_with_closed = [
            export_idx for export_idx, sess, has_closed in entries if has_closed
        ]

        if exports_with_closed:
            ownership[key] = min(exports_with_closed)
        else:
            ownership[key] = min(export_idx for export_idx, _, _ in entries)

    return ownership


def rebuild_export(data, ownership, export_idx):
    """Rebuild an export keeping only sessions owned by this export.

    Returns (new_data, stats_dict)
    """
    new_users = []
    total_sessions_before = 0
    total_sessions_after = 0
    total_events_before = 0
    total_events_after = 0

    for user in data.get("users", []):
        user_id = user.get("id")
        new_titles = []

        for title in user.get("titles", []):
            title_id = title.get("id")
            events = title.get("events", [])
            sessions = extract_sessions(events)

            total_sessions_before += len(sessions)
            total_events_before += len(events)

            owned_sessions = []
            for sess in sessions:
                key = session_key(sess, title_id, user_id)
                if ownership[key] == export_idx:
                    owned_sessions.append(sess)

            total_sessions_after += len(owned_sessions)

            if owned_sessions:
                new_events = []
                for sess in owned_sessions:
                    new_events.extend(sess["events"])
                new_events = sort_events(new_events)

                total_events_after += len(new_events)

                summary = title.get("summary", {})
                first_played, last_played = first_last_from_events(
                    new_events,
                    # NXAL/PDM reports firstPlayed/lastPlayed as the first
                    # "Account Login" and last "Lost Focus" events, NOT the
                    # raw Launched...Closed extremes. Earlier versions took
                    # new_events[0]/[-1] here, which widened the span beyond
                    # what NXAL would report and so diverged from a plain
                    # merge of the same exports (see first_last_from_events).
                    fallback_first=summary.get("firstPlayed", 0),
                    fallback_last=summary.get("lastPlayed", 0),
                )
                new_titles.append({
                    "id": title_id,
                    "name": title.get("name", ""),
                    "summary": {
                        "firstPlayed": first_played,
                        "lastPlayed": last_played,
                        "launches": count_launches(new_events),
                        "playtime": calculate_playtime(new_events),
                    },
                    "events": new_events,
                })

        if new_titles:
            new_users.append({
                "id": user_id,
                "name": user.get("name", ""),
                "titles": new_titles,
            })

    return {
        "exportString": data.get("exportString", ""),
        "exportTimestamp": data.get("exportTimestamp", 0),
        "exportVersion": data.get("exportVersion", ""),
        "users": new_users,
    }, {
        "sessions_before": total_sessions_before,
        "sessions_after": total_sessions_after,
        "events_before": total_events_before,
        "events_after": total_events_after,
        "titles_after": sum(len(u.get("titles", [])) for u in new_users),
    }


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input1.json> <input2.json> [...]")
        sys.exit(2)

    input_paths = sys.argv[1:]

    # Validate all inputs exist
    for path in input_paths:
        if not os.path.exists(path):
            print(f"Error: {path} not found")
            sys.exit(1)

    # Load all inputs
    exports = []
    for path in input_paths:
        try:
            data = load_json(path)
            exports.append((path, data))
        except Exception as e:
            print(f"Error loading {path}: {e}")
            sys.exit(1)

    # Sort by exportTimestamp (oldest first)
    exports.sort(key=lambda x: x[1].get("exportTimestamp", 0))

    print(f"Loaded {len(exports)} files, sorted by date:")
    for i, (path, data) in enumerate(exports):
        ts = data.get("exportTimestamp", 0)
        users = data.get("users", [])
        titles = sum(len(u.get("titles", [])) for u in users)
        print(f"  {i + 1}. {os.path.basename(path)} ({titles} titles, ts={ts})")
    print()

    # Build session index and determine ownership
    session_index = build_session_index(exports)
    ownership = determine_ownership(session_index, len(exports))

    # Create backups and rebuild each export
    results = []
    for export_idx, (path, data) in enumerate(exports):
        backup_path = path + ".bak"
        shutil.copy2(path, backup_path)

        new_data, stats = rebuild_export(data, ownership, export_idx)

        write_export_json(path, new_data)
        basename = os.path.basename(path)
        sessions_removed = stats["sessions_before"] - stats["sessions_after"]
        events_removed = stats["events_before"] - stats["events_after"]

        print(f"=== {basename} ===")
        print(f"  Backup: {os.path.basename(backup_path)}")
        print(f"  Sessions: {stats['sessions_after']}/{stats['sessions_before']} ({sessions_removed} removed)")
        print(f"  Events: {stats['events_after']}/{stats['events_before']} ({events_removed} removed)")
        print(f"  Titles: {stats['titles_after']}")
        print()

    print("Done. Originals saved as .bak files.")


if __name__ == "__main__":
    main()
