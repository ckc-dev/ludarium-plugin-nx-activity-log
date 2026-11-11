#!/usr/bin/env python3
"""
merge_exports.py — Merge multiple NX-ActivityLog import.json files into one.

NXAL's import overwrites importedData.json completely, so importing a second
export loses the first. This script merges multiple exports into a single
import.json that can be imported once, preserving all data.

Usage:
    python3 merge_exports.py <output.json> <input1.json> <input2.json> [input3.json ...]

Exit code: 0 on success, 1 on error.
"""

import os
import shutil
import sys

from nxal_codec import (
    calculate_playtime,
    count_launches,
    deduplicate_events,
    first_last_from_events,
    load_json,
    sort_events,
    write_export_json,
)

# NXAL deduplicates events on import/merge by a 5-field match:
# (type, titleID, eventType, clockTimestamp, steadyTimestamp) — see
# PlayData.cpp:386-388. titleID/type are matched at the title/user level,
# so the remaining identity key here is the 3-tuple below. steadyTimestamp
# (the monotonic boot clock) is NOT redundant with clockTimestamp: two
# events can share a wall-clock second yet be distinct (e.g. across a
# reboot), so it must be kept to avoid deleting real events.
MERGE_DEDUP_KEY = ("type", "clockTimestamp", "steadyTimestamp")


def merge_users(source_users, target_users):
    """Merge source users into target users by display name."""
    merged = {}
    for user in list(target_users) + list(source_users):
        name = user["name"]
        if name in merged:
            merged[name] = merge_single_user(merged[name], user)
        else:
            merged[name] = normalize_user_titles(user)
    return list(merged.values())


def normalize_user_titles(user):
    """Return `user` with every title's events deduplicated and sorted.

    Used so that every user entering the merged result — including the
    first file's users, which are otherwise stored as-is — has normalized
    events. Without this, a title present in only one input keeps its raw
    events (including PDM-artifact duplicate 'Account Logout' events) while
    a title present in two inputs is deduplicated by merge_title, making
    merge(dedup(x)) differ from merge(x) depending on where a title lands.
    """
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "titles": [normalize_title_events(t) for t in user.get("titles", [])],
    }


def merge_single_user(target, source):
    """Merge two users with the same name."""
    merged_titles = {t["id"]: t for t in target.get("titles", [])}

    for title in source.get("titles", []):
        tid = title["id"]
        if tid in merged_titles:
            merged_titles[tid] = merge_title(merged_titles[tid], title)
        else:
            merged_titles[tid] = normalize_title_events(title)

    return {
        "id": target["id"],
        "name": target["name"],
        "titles": list(merged_titles.values()),
    }


def normalize_title_events(title):
    """Return `title` with its events deduplicated and sorted in place.

    A title that appears in only one input is passed through merge_title
    untouched, so its raw events (which may contain PDM-artifact duplicate
    'Account Logout' events with identical 3-field keys) are preserved. A
    title that appears in two inputs is deduplicated by merge_title. That
    asymmetry made merge(dedup(x)) differ from merge(x): name-based dedup
    moves a shared title into a single file, so it flips from the merged
    path to the pass-through path. Normalizing events here (dedup + sort)
    keeps every output title consistent regardless of source-count, while
    leaving the stored summary untouched.
    """
    events = title.get("events", [])
    if not events:
        return title
    normalized = {
        "id": title["id"],
        "name": title["name"],
        "summary": title["summary"],
        "events": sort_events(deduplicate_events(events, MERGE_DEDUP_KEY)),
    }
    return normalized


def merge_title(target, source):
    """Merge two title entries for the same game."""
    target_events = target.get("events", [])
    source_events = source.get("events", [])

    merged_events = deduplicate_events(target_events + source_events, MERGE_DEDUP_KEY)
    merged_events = sort_events(merged_events)
    merged_summary = merge_summary(target.get("summary", {}), source.get("summary", {}), merged_events)

    return {
        "id": target["id"],
        "name": target["name"],
        "summary": merged_summary,
        "events": merged_events,
    }


def merge_summary(target, source, events):
    """Merge two summary objects, recomputing from merged events.

    firstPlayed/lastPlayed are ALWAYS recomputed from the merged events,
    never inherited from the inputs. NXAL/PDM reports these as the first
    "Account Login" and last "Lost Focus" events (see
    first_last_from_events); taking min/max of the inputs' stored summaries
    instead would silently inherit whatever drift the input files carry
    (e.g. summary edits or readded sessions), making output
    order-dependent and unfaithful. The input summaries are kept only as
    the empty-events fallback.
    """
    first_played, last_played = first_last_from_events(
        events,
        fallback_first=target.get("firstPlayed") or source.get("firstPlayed") or 0,
        fallback_last=target.get("lastPlayed") or source.get("lastPlayed") or 0,
    )

    launches = count_launches(events)
    playtime = calculate_playtime(events)

    return {
        "firstPlayed": first_played,
        "lastPlayed": last_played,
        "launches": launches,
        "playtime": playtime,
    }


def merge_metadata(inputs):
    """Create merged metadata from multiple input files."""
    latest_ts = max(d.get("exportTimestamp", 0) for d in inputs)
    latest_str = max((d.get("exportString", "") for d in inputs), key=lambda s: s)
    versions = set(d.get("exportVersion", "") for d in inputs)
    version = max(versions, key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit())) if versions else "unknown"

    return {
        "exportString": f"{latest_str} (merged from {len(inputs)} exports)",
        "exportTimestamp": latest_ts,
        "exportVersion": version,
    }


def merge_paths(output_path, input_paths, backup=True):
    """Merge input exports into ``output_path`` in place.

    The output is backed up first when it already exists. Input files are only
    read. This is the shared implementation used by both the CLI and the
    environment-scoped host tool.
    """
    inputs = []
    loaded = []
    for path in input_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found")
        data = load_json(path)
        inputs.append(data)
        users = data.get("users", [])
        titles = sum(len(u.get("titles", [])) for u in users)
        loaded.append((path, len(users), titles))

    merged_users = []
    for data in inputs:
        merged_users = merge_users(data.get("users", []), merged_users)

    metadata = merge_metadata(inputs)
    total_titles = sum(len(u.get("titles", [])) for u in merged_users)
    total_events = sum(
        len(t["events"]) for u in merged_users for t in u.get("titles", []))

    if backup and os.path.exists(output_path):
        shutil.copy2(output_path, output_path + ".bak")
    write_export_json(output_path, {
        **metadata,
        "users": merged_users,
    })
    return {
        "path": output_path,
        "loaded": loaded,
        "users": len(merged_users),
        "titles": total_titles,
        "events": total_events,
        "backup": (output_path + ".bak" if backup
                   and os.path.exists(output_path + ".bak") else None),
    }


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <output.json> <input1.json> <input2.json> [...]")
        sys.exit(2)

    output_path = sys.argv[1]
    input_paths = sys.argv[2:]

    try:
        stats = merge_paths(output_path, input_paths)
    except Exception as e:
        print(f"Error merging exports: {e}")
        sys.exit(1)
    for path, users, titles in stats["loaded"]:
        print(f"Loaded {path}: {users} users, {titles} titles")
    if stats["backup"]:
        print(f"Backup: {os.path.basename(stats['backup'])}")
    print(f"\nMerged output: {output_path}")
    print(f"  Users: {stats['users']}")
    print(f"  Titles: {stats['titles']}")
    print(f"  Events: {stats['events']}")


if __name__ == "__main__":
    main()
