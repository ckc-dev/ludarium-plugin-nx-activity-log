"""GameTime: PLUGIN_TOOLS for the NXAL adapter.

Reuses only verified nxal_common helpers (load_json, count_launches,
calculate_playtime, find_duplicate_events, event_key, deduplicate_events,
sort_events, normalize_export, write_export_json) plus duplicate-event
primitives so tool output stays byte-compatible with the hosted CLI.
"""

import os

from nxal_common import (load_json, count_launches, calculate_playtime,
                         find_duplicate_events, event_key,
                         deduplicate_events, sort_events, normalize_export,
                         write_export_json)


def verify_export(params, ctx):
    path = (params.get("path") or "").strip()
    if not os.path.isfile(path):
        return {"ok": False, "message": f"File not found: {path}",
                "lines": []}
    try:
        data = load_json(path)
    except Exception as e:
        return {"ok": False, "message": f"Could not read: {e}", "lines": []}

    users = data.get("users", [])
    titles = sum(len(u.get("titles", [])) for u in users)
    launches = sum(count_launches(t.get("events", []))
                   for u in users for t in u.get("titles", []))
    minutes = sum(calculate_playtime(t.get("events", [])) for u in users
                  for t in u.get("titles", [])) / 60.0
    dupes = sum(len(find_duplicate_events(t.get("events", [])))
                for u in users for t in u.get("titles", []))
    lines = [f"Users: {len(users)}", f"Titles: {titles}",
             f"Launches: {launches}", f"Playtime: {minutes:.1f} min"]
    if dupes:
        lines.append(f"WARNING: {dupes} duplicate-event group(s) found")
    return {"ok": True, "message": "Export looks valid", "lines": lines}


def fix_import(params, ctx):
    path = (params.get("path") or "").strip()
    if not os.path.isfile(path):
        return {"ok": False, "message": f"File not found: {path}",
                "lines": []}
    try:
        data = load_json(path)
    except Exception as e:
        return {"ok": False, "message": f"Could not read: {e}", "lines": []}

    fixed = 0
    for u in data.get("users", []):
        for t in u.get("titles", []):
            evs = t.get("events", [])
            before = len(evs)
            evs = deduplicate_events(evs, ("titleId", "timestamp", "eventType"))
            evs = sort_events(evs)
            fixed += before - len(evs)
            t["events"] = evs
    data = normalize_export(data)
    write_export_json(path, data)
    return {"ok": True, "message": f"Fixed {fixed} duplicate-ish event(s)",
            "lines": [f"Rewrote: {path}"]}


PLUGIN_TOOLS = [
    {
        "key": "verify_export",
        "name": "Verify export",
        "title": "Verify export",
        "description": "Verify users/titles/launches/playtime in an export.",
        "fields": [
            {"name": "path", "label": "Export path", "type": "path",
             "required": True,
             "help": "Absolute path to an NXAL import.json export."},
        ],
        "execute": verify_export,
    },
    {
        "key": "fix_import",
        "name": "Fix import",
        "title": "Fix import",
        "description": "Deduplicate + sort events and rewrite the export in place.",
        "fields": [
            {"name": "path", "label": "Export path", "type": "path",
             "required": True,
             "help": "Absolute path to an NXAL import.json export."},
        ],
        "execute": fix_import,
    },
]
