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
    EVENT_IDENTITY_KEY,
    deduplicate_events,
    load_json,
    normalize_event_order,
    write_export_json,
)
from nxal_repair import updated_summary

# The shared EVENT_IDENTITY_KEY is NXAL's per-event identity. steadyTimestamp
# (the monotonic boot clock) is NOT redundant with clockTimestamp: two events
# can share a wall-clock second yet be distinct (e.g. across a reboot), so it
# must be kept to avoid deleting real events.


def merge_users(source_users, target_users, *, preserve_summary_only=True,
                recalculate_first_last=False):
    """Merge source users into target users by display name."""
    merged = {}
    for user in list(target_users) + list(source_users):
        name = user["name"]
        if name in merged:
            merged[name] = merge_single_user(
                merged[name], user,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last)
        else:
            merged[name] = normalize_user_titles(
                user,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last)
    return list(merged.values())


def normalize_user_titles(user, *, preserve_summary_only=True,
                          recalculate_first_last=False):
    """Return `user` with every title's events normalized and summarized."""
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "titles": [
            normalize_title_events(
                t,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last)
            for t in user.get("titles", [])
        ],
    }


def merge_single_user(target, source, *, preserve_summary_only=True,
                      recalculate_first_last=False):
    """Merge two users with the same name."""
    merged_titles = {
        t["id"]: normalize_title_events(
            t,
            preserve_summary_only=preserve_summary_only,
            recalculate_first_last=recalculate_first_last)
        for t in target.get("titles", [])
    }

    for title in source.get("titles", []):
        tid = title["id"]
        if tid in merged_titles:
            merged_titles[tid] = merge_title(
                merged_titles[tid], title,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last)
        else:
            merged_titles[tid] = normalize_title_events(
                title,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last)

    return {
        "id": target["id"],
        "name": target["name"],
        "titles": list(merged_titles.values()),
    }


def normalize_title_events(title, *, preserve_summary_only=True,
                           recalculate_first_last=False):
    """Return `title` with events deduplicated/sorted and summary updated.

    Summary-only titles are preserved by default instead of being zeroed.
    Valid stored first/last values are also preserved unless the caller asks
    for an explicit event-derived recalculation.
    """
    events = title.get("events", [])
    normalized_events = normalize_event_order(
        deduplicate_events(events, EVENT_IDENTITY_KEY))
    summary = updated_summary(
        title.get("summary", {}),
        normalized_events,
        recalculate_first_last=recalculate_first_last,
        preserve_empty=preserve_summary_only,
    )
    return {
        "id": title["id"],
        "name": title["name"],
        "summary": summary,
        "events": normalized_events,
    }


def merge_title(target, source, *, preserve_summary_only=True,
                recalculate_first_last=False):
    """Merge two title entries for the same game."""
    target_events = target.get("events", [])
    source_events = source.get("events", [])

    merged_events = deduplicate_events(
        target_events + source_events, EVENT_IDENTITY_KEY)
    merged_events = normalize_event_order(merged_events)
    merged_summary = merge_summary(
        target.get("summary", {}), source.get("summary", {}), merged_events,
        preserve_summary_only=preserve_summary_only,
        recalculate_first_last=recalculate_first_last)

    return {
        "id": target["id"],
        "name": target["name"],
        "summary": merged_summary,
        "events": merged_events,
    }


def merge_summary(target, source, events, *, preserve_summary_only=True,
                  recalculate_first_last=False):
    """Merge summaries using the shared NXAL accounting policy.

    Playtime/launches are event-derived. Valid stored first/last values are
    preserved unless recalculation is requested. For summary-only titles the
    merged first/last use the earliest/latest available values and playtime/
    launches use the largest available value rather than zero.
    """
    target_summary = target or {}
    source_summary = source or {}
    firsts = [
        value for value in (
            target_summary.get("firstPlayed"),
            source_summary.get("firstPlayed"))
        if isinstance(value, int) and value > 0
    ]
    lasts = [
        value for value in (
            target_summary.get("lastPlayed"),
            source_summary.get("lastPlayed"))
        if isinstance(value, int) and value > 0
    ]
    base = {
        "firstPlayed": min(firsts) if firsts else 0,
        "lastPlayed": max(lasts) if lasts else 0,
        "playtime": max(
            target_summary.get("playtime", 0) or 0,
            source_summary.get("playtime", 0) or 0),
        "launches": max(
            target_summary.get("launches", 0) or 0,
            source_summary.get("launches", 0) or 0),
    }
    return updated_summary(
        base,
        events,
        recalculate_first_last=recalculate_first_last,
        fallback_first=base["firstPlayed"],
        fallback_last=base["lastPlayed"],
        preserve_empty=preserve_summary_only,
    )


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


def merge_paths(output_path, input_paths, backup=True, *,
                preserve_summary_only=True, recalculate_first_last=False):
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
        merged_users = merge_users(
            data.get("users", []), merged_users,
            preserve_summary_only=preserve_summary_only,
            recalculate_first_last=recalculate_first_last)

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
    args = sys.argv[1:]
    recalculate_first_last = "--recalculate-first-last" in args
    drop_summary_only = "--drop-summary-only" in args
    args = [
        arg for arg in args
        if arg not in ("--recalculate-first-last", "--drop-summary-only")
    ]
    if len(args) < 3:
        print(f"Usage: {sys.argv[0]} [--recalculate-first-last] "
              "[--drop-summary-only] <output.json> <input1.json> "
              "<input2.json> [...]")
        sys.exit(2)

    output_path = args[0]
    input_paths = args[1:]

    try:
        stats = merge_paths(
            output_path, input_paths,
            preserve_summary_only=not drop_summary_only,
            recalculate_first_last=recalculate_first_last)
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
