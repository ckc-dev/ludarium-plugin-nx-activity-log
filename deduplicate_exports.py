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
import json
import shutil

from nxal_common import load_json


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
    """Unique key for a session: (title_id, user_id, start_timestamp)."""
    return (title_id, user_id, session["start"])


def count_launches(events):
    """Count Launched events."""
    return sum(1 for ev in events if ev.get("type") == "Launched")


def calculate_playtime(events):
    """Calculate playtime from focus events (using clockTimestamp)."""
    playtime = 0
    last_focus_start = None
    for ev in events:
        if ev.get("type") == "Gained Focus":
            last_focus_start = ev.get("clockTimestamp")
        elif ev.get("type") == "Lost Focus" and last_focus_start is not None:
            playtime += ev["clockTimestamp"] - last_focus_start
            last_focus_start = None
    return playtime


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
                new_events.sort(key=lambda e: e["clockTimestamp"])

                total_events_after += len(new_events)

                summary = title.get("summary", {})
                new_titles.append({
                    "id": title_id,
                    "name": title.get("name", ""),
                    "summary": {
                        "firstPlayed": new_events[0]["clockTimestamp"] if new_events else summary.get("firstPlayed", 0),
                        "lastPlayed": new_events[-1]["clockTimestamp"] if new_events else summary.get("lastPlayed", 0),
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

        with open(path, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=4)

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
