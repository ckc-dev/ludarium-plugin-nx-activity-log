import os

from nxal_codec import (
    calculate_playtime,
    count_launches,
    deduplicate_events,
    find_duplicate_events,
    find_orphaned_lost_focus,
    first_last_from_events,
    load_json,
    sort_events,
    write_export_json,
)


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
        lines.append(f"WARN: {dupes} duplicate event(s) present")
    return {"ok": True, "message": "Export verified", "lines": lines}


def fix_import(params, ctx):
    path, err = _path(params, ctx)
    if err:
        return {"ok": False, "message": err, "lines": []}
    try:
        data = load_json(path)
    except Exception as e:
        return {"ok": False, "message": f"Could not read file: {e}", "lines": []}
    removed_events = 0
    for u in data.get("users", []):
        for t in u.get("titles", []):
            evs = deduplicate_events(
                t.get("events", []),
                ("type", "clockTimestamp", "steadyTimestamp"))
            before = len(t.get("events", []))
            evs = sort_events(evs)
            removed_events += before - len(evs)
            orphaned = set(find_orphaned_lost_focus(evs))
            if orphaned:
                evs = [
                    event for index, event in enumerate(evs)
                    if index not in orphaned
                ]
                removed_events += len(orphaned)
            t["events"] = evs
            if not evs:
                continue
            summary = t.setdefault("summary", {})
            first, last = first_last_from_events(
                evs, summary.get("firstPlayed", 0),
                summary.get("lastPlayed", 0))
            # NXAL's firstPlayed/lastPlayed come from PDM, not events.
            # Preserve existing non-zero values; fill only a missing pair.
            if not summary.get("firstPlayed"):
                summary["firstPlayed"] = first
            if not summary.get("lastPlayed"):
                summary["lastPlayed"] = last
            summary["playtime"] = calculate_playtime(evs)
            summary["launches"] = count_launches(evs)
    write_export_json(path, data)
    return {"ok": True,
            "message": f"Rewrote {path} (removed {removed_events} event(s))",
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
