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

import sys
import json
import os

from nxal_common import load_json


def merge_users(source_users, target_users):
    """Merge source users into target users by display name."""
    merged = {u["name"]: u for u in target_users}

    for user in source_users:
        name = user["name"]
        if name in merged:
            merged[name] = merge_single_user(merged[name], user)
        else:
            merged[name] = user

    return list(merged.values())


def merge_single_user(target, source):
    """Merge two users with the same name."""
    merged_titles = {t["id"]: t for t in target.get("titles", [])}

    for title in source.get("titles", []):
        tid = title["id"]
        if tid in merged_titles:
            merged_titles[tid] = merge_title(merged_titles[tid], title)
        else:
            merged_titles[tid] = title

    return {
        "id": target["id"],
        "name": target["name"],
        "titles": list(merged_titles.values()),
    }


def merge_title(target, source):
    """Merge two title entries for the same game."""
    target_events = target.get("events", [])
    source_events = source.get("events", [])

    merged_events = deduplicate_events(target_events + source_events)
    merged_summary = merge_summary(target.get("summary", {}), source.get("summary", {}), merged_events)

    return {
        "id": target["id"],
        "name": target["name"],
        "summary": merged_summary,
        "events": merged_events,
    }


def deduplicate_events(events):
    """Remove duplicate events by 5-field match."""
    seen = set()
    unique = []

    for ev in events:
        key = (
            ev.get("type"),
            ev.get("clockTimestamp"),
            ev.get("steadyTimestamp"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    unique.sort(key=lambda e: e["clockTimestamp"])
    return unique


def merge_summary(target, source, events):
    """Merge two summary objects, recalculating from merged events."""
    fp_target = target.get("firstPlayed", 0)
    fp_source = source.get("firstPlayed", 0)
    lp_target = target.get("lastPlayed", 0)
    lp_source = source.get("lastPlayed", 0)

    first_played = min(fp_target, fp_source) if (fp_target and fp_source) else (fp_target or fp_source)
    last_played = max(lp_target, lp_source) if (lp_target and lp_source) else (lp_target or lp_source)

    launches = count_launches(events)
    playtime = calculate_playtime(events)

    return {
        "firstPlayed": first_played,
        "lastPlayed": last_played,
        "launches": launches,
        "playtime": playtime,
    }


def count_launches(events):
    return sum(1 for ev in events if ev.get("type") == "Launched")


def calculate_playtime(events):
    playtime = 0
    last_focus_start = None
    for ev in events:
        if ev.get("type") == "Gained Focus":
            last_focus_start = ev.get("clockTimestamp")
        elif ev.get("type") == "Lost Focus" and last_focus_start is not None:
            playtime += ev["clockTimestamp"] - last_focus_start
            last_focus_start = None
    return playtime


def merge_metadata(inputs):
    """Create merged metadata from multiple input files."""
    latest_ts = max(d.get("exportTimestamp", 0) for d in inputs)
    latest_str = max((d.get("exportString", "") for d in inputs), key=lambda s: s)
    versions = set(d.get("exportVersion", "") for d in inputs)
    version = max(versions) if versions else "unknown"

    return {
        "exportString": f"{latest_str} (merged from {len(inputs)} exports)",
        "exportTimestamp": latest_ts,
        "exportVersion": version,
    }


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <output.json> <input1.json> <input2.json> [...]")
        sys.exit(2)

    output_path = sys.argv[1]
    input_paths = sys.argv[2:]

    inputs = []
    for path in input_paths:
        if not os.path.exists(path):
            print(f"Error: {path} not found")
            sys.exit(1)
        try:
            data = load_json(path)
            inputs.append(data)
            users = data.get("users", [])
            titles = sum(len(u.get("titles", [])) for u in users)
            print(f"Loaded {path}: {len(users)} users, {titles} titles")
        except Exception as e:
            print(f"Error loading {path}: {e}")
            sys.exit(1)

    merged_users = []
    for data in inputs:
        merged_users = merge_users(data.get("users", []), merged_users)

    metadata = merge_metadata(inputs)

    total_titles = sum(len(u.get("titles", [])) for u in merged_users)
    total_events = sum(len(t["events"]) for u in merged_users for t in u.get("titles", []))

    result = {
        **metadata,
        "users": merged_users,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=4)

    print(f"\nMerged output: {output_path}")
    print(f"  Users: {len(merged_users)}")
    print(f"  Titles: {total_titles}")
    print(f"  Events: {total_events}")


if __name__ == "__main__":
    main()
