"""Ludarium: PLUGIN_HEALTH for the NXAL adapter (export-level checks).

Signature honored by the host: ``PLUGIN_HEALTH(path, config, ctx) -> [{'level','message'}]``
"""

import os

from nxal_codec import calculate_nxal_playtime, count_launches, load_json


def check_path(path, config, ctx):
    items = []
    if not os.path.isfile(path):
        return [{"level": "error", "message": f"Export missing: {path}"}]
    try:
        data = load_json(path)
    except Exception as e:
        return [{"level": "error", "message": f"Could not read export: {e}"}]

    users = data.get("users", [])
    launches = sum(count_launches(t.get("events", []))
                   for u in users for t in u.get("titles", []))
    minutes = sum(calculate_nxal_playtime(t.get("events", [])) for u in users
                  for t in u.get("titles", [])) / 60.0
    items.append({"level": "ok",
                  "message": f"Users={len(users)} launches={launches} "
                             f"playtime={minutes:.1f}min"})
    return items


PLUGIN_HEALTH = check_path
