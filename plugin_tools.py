import os

from nxal_common import (load_json, write_export_json, count_launches,
                         calculate_playtime, find_duplicate_events,
                         deduplicate_events, sort_events)


def _path(params, ctx):
    path = (params.get("path") or "").strip()
    if not path:
        path = (ctx.path or "").strip()
    if not path:
        return None, ("Provide a path to an NXAL import.json or select an "
                      "environment above.")
    if not os.path.isfile(path):
        return None, f"File not found: {path}"
    return path, None


def verify_export(params, ctx):
    path, err = _path(params, ctx)
    if err:
        return {"ok": False, "message": err, "lines": []}
    try:
        data = load_json(path)
    except Exception as e:
        return {"ok": False, "message": f"Could not read file: {e}", "lines": []}
    users = data.get("users", [])
    titles = sum(len(u.get("titles", [])) for u in users)
    launches = sum(count_launches(t.get("events", []))
                   for u in users for t in u.get("titles", []))
    minutes = sum(calculate_playtime(t.get("events", []))
                  for u in users for t in u.get("titles", [])) / 60.0
    dupes = sum(len(find_duplicate_events(t.get("events", [])))
                for u in users for t in u.get("titles", []))
    lines = [f"File: {path}", f"Users: {len(users)}", f"Titles: {titles}",
             f"Launches: {launches}", f"Playtime: {minutes:.1f} min"]
    if dupes:
        lines.append(f"WARN: {dupes} duplicate event group(s) present")
    return {"ok": True, "message": "Export verified", "lines": lines}


def fix_import(params, ctx):
    path, err = _path(params, ctx)
    if err:
        return {"ok": False, "message": err, "lines": []}
    try:
        data = load_json(path)
    except Exception as e:
        return {"ok": False, "message": f"Could not read file: {e}", "lines": []}
    fixed = 0
    for u in data.get("users", []):
        for t in u.get("titles", []):
            evs = t.get("events", [])
            before = len(evs)
            evs = deduplicate_events(evs, ("titleId", "timestamp", "eventType"))
            evs = sort_events(evs)
            fixed += before - len(evs)
            t["events"] = evs
    write_export_json(path, data)
    return {"ok": True, "message": f"Rewrote {path} (removed {fixed} dup events)",
            "lines": []}


PLUGIN_TOOLS = [
    {"key": "verify_export", "name": "Verify export",
     "description": "Validate users/titles/launches/playtime in an NXAL export.",
     "needs_environment": True,
     "fields": [
         {"name": "path", "label": "Export path", "type": "path",
          "required": False,
          "help": "Leave empty to use the selected environment path."},
     ],
     "execute": verify_export},
    {"key": "fix_import", "name": "Fix export",
     "description": "Deduplicate + sort events, write back byte-compatibly.",
     "needs_environment": True,
     "fields": [
         {"name": "path", "label": "Export path", "type": "path",
          "required": False,
          "help": "Leave empty to use the selected environment path."},
     ],
     "execute": fix_import},
]
