#!/usr/bin/env python3
"""
deduplicate_exports.py — Remove overlapping sessions from multiple NXAL exports.

Takes multiple export JSON files, identifies sessions that appear in multiple
exports, and keeps each session only in the export that "owns" it. The result
is a set of incremental exports where each file contains only unique sessions.

Ownership rules:
  - If any export has the Closed event for a session → oldest such export owns it
  - If no export has Closed → oldest export that has Launched owns it

Matching: sessions are matched across exports by (title id, user, and the
3-field Launched event key). By default the user component is the display
NAME, matching how merge_exports merges users, so that a user-ID change
between exports (user/console migration) still recognizes the same person's
shared history. Use --match-by id to fall back to strict user-ID matching.

Renames: --rename OLD=NEW (repeatable) maps an old user name to a new one so
pre/post-rename history collides; it applies only to cross-file matching and
never changes the names stored in the output.

Usage:
    python3 deduplicate_exports.py [--match-by name|id] [--rename OLD=NEW ...] <input1.json> <input2.json> [...]

Options:
    --match-by name|id   Identity for user component of the session key
                         (default: name)
    --rename OLD=NEW     Treat OLD and NEW as the same user name when matching;
                         repeatable (only meaningful when matching by name)

Each input is backed up as <name>.json.bak, then overwritten with deduplicated data.

Exit code: 0 on success, 1 on error.
"""

import sys
import os
import shutil

from nxal_codec import (
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


def session_key(session, title_id, user_identity):
    """Unique key for a session.

    Uses the 3-field NXAL identity (SESSION_KEY_FIELDS) of the session's
    'Launched' event, combined with title id and a user identity. We key the
    user component by display NAME by default (resolved through any --rename
    mapping), matching how merge_exports merges users, so that sessions stay
    recognized as the same person's even when their user ID changes between
    exports (a user/console migration). Pass --match-by id to use the raw
    user ID instead. This keeps dedup and merge in agreement, so
    merge(dedup(x)) converges with merge(x).
    """
    launched = session["events"][0]
    return (title_id, user_identity, event_key(launched, SESSION_KEY_FIELDS))


def user_identity(user, match_by, renames):
    """Return the user component used for cross-file session matching.

    By name (default) we return the display name, resolved through any
    --rename mapping, so that a user renamed (or a user ID changed by a
    migration) still matches the same person's history across exports. By
    id we return the raw user ID, which strictly separates accounts.
    Never used for output; output always stores each file's native id/name.
    """
    if match_by == "id":
        return user.get("id")
    name = user.get("name", "")
    return renames.get(name, name)


def build_session_index(exports, match_by, renames):
    """Build an index of all sessions across all exports.

    Returns dict: session_key -> list of (export_idx, session, has_closed)
    """
    index = {}

    for export_idx, (_, data) in enumerate(exports):
        for user in data.get("users", []):
            ident = user_identity(user, match_by, renames)
            for title in user.get("titles", []):
                title_id = title.get("id")
                sessions = extract_sessions(title.get("events", []))
                for sess in sessions:
                    key = session_key(sess, title_id, ident)
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


def rebuild_export(data, ownership, export_idx, match_by, renames):
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
        match_id = user_identity(user, match_by, renames)
        new_titles = []

        for title in user.get("titles", []):
            title_id = title.get("id")
            events = title.get("events", [])
            sessions = extract_sessions(events)

            total_sessions_before += len(sessions)
            total_events_before += len(events)

            owned_sessions = []
            for sess in sessions:
                key = session_key(sess, title_id, match_id)
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
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Usage: {sys.argv[0]} [--match-by name|id] [--rename OLD=NEW ...] <input1.json> <input2.json> [...]")
        print(f"  --match-by name|id   Identity for session-key user component (default: name)")
        print(f"  --rename OLD=NEW     Treat OLD and NEW as the same user name (repeatable)")
        sys.exit(2)

    args = sys.argv[1:]

    match_by = "name"
    if "--match-by" in args:
        i = args.index("--match-by")
        if i + 1 >= len(args) or args[i + 1] not in ("name", "id"):
            print("Error: --match-by requires 'name' or 'id'")
            sys.exit(2)
        match_by = args[i + 1]
        del args[i:i + 2]

    renames = {}
    while "--rename" in args:
        i = args.index("--rename")
        if i + 1 >= len(args) or "=" not in args[i + 1]:
            print("Error: --rename requires OLD=NEW, e.g. --rename 'OldName=NewName'")
            sys.exit(2)
        old_name, new_name = args[i + 1].split("=", 1)
        renames[old_name] = new_name
        del args[i:i + 2]
    if renames and match_by == "id":
        print("Warning: --rename has no effect with --match-by id")

    input_paths = args

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
    session_index = build_session_index(exports, match_by, renames)
    ownership = determine_ownership(session_index, len(exports))

    # Create backups and rebuild each export
    results = []
    for export_idx, (path, data) in enumerate(exports):
        backup_path = path + ".bak"
        shutil.copy2(path, backup_path)

        new_data, stats = rebuild_export(data, ownership, export_idx, match_by, renames)

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
