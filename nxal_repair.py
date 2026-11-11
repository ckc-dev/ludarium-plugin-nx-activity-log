"""Shared NXAL repair and summary primitives.

The interactive ``fix_import.py`` CLI wraps these transformations with
confirmation prompts, while the host Tools page applies them as one in-place
repair. Merge and deduplicate use the same summary calculation, so repaired,
merged and deduplicated exports cannot drift apart.
"""

from nxal_codec import (
    EVENT_IDENTITY_KEY,
    VALID_EVENT_TYPES,
    calculate_playtime,
    count_launches,
    deduplicate_events,
    find_orphaned_lost_focus,
    first_last_from_events,
    sort_events,
)


def find_invalid_event_fields(events):
    """Return ``(index, field_name)`` for events NXAL cannot import.

    Only the first invalid field for each event is reported. The event itself
    is invalid if it is not a dict, has an unknown type, or is missing either
    timestamp.
    """
    invalid = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            invalid.append((index, "event"))
        elif event.get("type") not in VALID_EVENT_TYPES:
            invalid.append((index, "type"))
        elif not isinstance(event.get("clockTimestamp"), int):
            invalid.append((index, "clockTimestamp"))
        elif not isinstance(event.get("steadyTimestamp"), int):
            invalid.append((index, "steadyTimestamp"))
    return invalid


def remove_invalid_events(events):
    """Remove events with invalid/missing fields, returning ``(events, count)``."""
    invalid = find_invalid_event_fields(events)
    invalid_indices = {index for index, _field in invalid}
    cleaned = [
        event for index, event in enumerate(events)
        if index not in invalid_indices
    ]
    return cleaned, len(invalid)


def remove_duplicate_events(events):
    """Remove strict NXAL-identity duplicates, returning ``(events, count)``."""
    cleaned = deduplicate_events(events, EVENT_IDENTITY_KEY)
    return cleaned, len(events) - len(cleaned)


def remove_orphaned_lost_focus(events):
    """Remove orphan focus events, returning ``(events, count)``."""
    orphaned = set(find_orphaned_lost_focus(events))
    cleaned = [
        event for index, event in enumerate(events)
        if index not in orphaned
    ]
    return cleaned, len(orphaned)


def summary_for_events(events, fallback_first=0, fallback_last=0):
    """Build NXAL summary fields from an event stream."""
    first_played, last_played = first_last_from_events(
        events, fallback_first, fallback_last)
    return {
        "firstPlayed": first_played,
        "lastPlayed": last_played,
        "launches": count_launches(events),
        "playtime": calculate_playtime(events),
    }


def repair_events(events):
    """Apply the noninteractive event repair pipeline.

    Invalid events are removed first so sorting/detection never sees missing
    fields. Events are then deduplicated and sorted before orphan focus is
    checked; otherwise a deliberately out-of-order file could make a valid
    Gained/Lost pair look orphaned.
    """
    events, invalid = remove_invalid_events(events)
    events, duplicates = remove_duplicate_events(events)
    events = sort_events(events)
    events, orphans = remove_orphaned_lost_focus(events)
    return events, {
        "invalid": invalid,
        "duplicates": duplicates,
        "orphans": orphans,
        "events_removed": invalid + duplicates + orphans,
    }


def repair_title(title):
    """Repair one title's events and summary in place; return repair counts."""
    events, stats = repair_events(title.get("events", []))
    title["events"] = events
    summary = title.setdefault("summary", {})
    summary.update(summary_for_events(events))
    stats["summary"] = summary
    return stats


def repair_export(data):
    """Repair every title in an export in place and return aggregate counts."""
    totals = {
        "titles": 0,
        "invalid": 0,
        "duplicates": 0,
        "orphans": 0,
        "events_removed": 0,
    }
    for user in data.get("users", []):
        for title in user.get("titles", []):
            stats = repair_title(title)
            totals["titles"] += 1
            for key in ("invalid", "duplicates", "orphans", "events_removed"):
                totals[key] += stats[key]
    return totals
