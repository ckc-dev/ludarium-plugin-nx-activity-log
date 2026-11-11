"""Native NXAL profile discovery for the host environment editor.

This module deliberately reads only the export/import JSON format. It returns
stable native ids plus display names so host bindings can survive a profile
rename and so duplicate names can be reported instead of silently merged.
"""

from nxal_codec import load_json


def list_profiles(path, config=None):
    """Return ``[{'key': native_id, 'name': display_name}, ...]``."""
    data = load_json(path)
    users = data.get("users")
    if not isinstance(users, list):
        raise ValueError("NXAL file has no 'users' array")
    profiles = []
    seen_keys = set()
    seen_names = set()
    for index, user in enumerate(users):
        if not isinstance(user, dict):
            raise ValueError(f"NXAL user #{index} is not an object")
        name = user.get("name")
        native_id = user.get("id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                "NXAL file has no profile names; use export.json/import.json, "
                "not importedData.json")
        if not isinstance(native_id, str) or not native_id.strip():
            # Older/manual files may omit the console id; fall back to a
            # normalized name only when no native identity exists.
            key = "name:" + name.strip().casefold()
        else:
            key = native_id.strip()
        if key in seen_keys:
            raise ValueError(f"Duplicate NXAL profile id: {key}")
        if name.strip().casefold() in seen_names:
            raise ValueError(f"Duplicate NXAL profile name: {name}")
        seen_keys.add(key)
        seen_names.add(name.strip().casefold())
        profiles.append({"key": key, "name": name.strip()})
    return profiles
