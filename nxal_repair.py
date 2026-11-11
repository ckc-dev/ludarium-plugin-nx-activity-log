"""Shared NXAL repair and summary primitives.

The interactive ``fix_import.py`` CLI wraps these transformations with
confirmation prompts, while the host Tools page applies them as one in-place
repair. Merge and deduplicate use the same summary calculation, so repaired,
merged and deduplicated exports cannot drift apart.
"""

from nxal_codec import (
    EVENT_IDENTITY_KEY,
    VALID_EVENT_TYPES,
    calculate_nxal_playtime,
    count_launches,
    deduplicate_events,
    find_orphaned_lost_focus,
    first_last_from_events,
    normalize_event_order,
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
    """Build NXAL summary fields from an event stream.

    Playtime uses NXAL ``countPlaytime()`` accounting, including its handling
    of repeated and account-separated Lost Focus events. First/last remain
    event-derived proxies; callers that edit existing exports should normally
    preserve valid PDM values instead (see update_summary).
    """
    first_played, last_played = first_last_from_events(
        events, fallback_first, fallback_last)
    return {
        "firstPlayed": first_played,
        "lastPlayed": last_played,
        "launches": count_launches(events),
        "playtime": calculate_nxal_playtime(events),
    }


def _preserved_first_last(summary, computed):
    """Keep valid existing first/last values unless explicitly recalculating."""
    first = summary.get("firstPlayed")
    last = summary.get("lastPlayed")
    if (isinstance(first, int) and first > 0
            and isinstance(last, int) and last > 0
            and first <= last):
        return first, last
    return computed["firstPlayed"], computed["lastPlayed"]


def update_summary(summary, events, *, recalculate_first_last=False,
                   fallback_first=0, fallback_last=0,
                   preserve_empty=False):
    """Update summary fields using the shared NXAL accounting policy.

    By default, valid PDM first/last values are preserved and event-derived
    values are used only when the stored pair is missing or invalid. Playtime
    and launches always follow the event stream (NXAL's export summary is
    event-derived for those fields). With ``preserve_empty`` a summary-only
    title is left untouched when there are no events.
    """
    if not events:
        if preserve_empty:
            return summary
        summary.update(summary_for_events(
            events, fallback_first, fallback_last))
        return summary
    computed = summary_for_events(events, fallback_first, fallback_last)
    if not recalculate_first_last:
        computed["firstPlayed"], computed["lastPlayed"] = (
            _preserved_first_last(summary, computed))
    summary.update(computed)
    return summary


def repair_events(events):
    """Apply the noninteractive event repair pipeline.

    Invalid events are removed first so sorting/detection never sees missing
    fields. Events are then deduplicated and sorted before orphan focus is
    checked; otherwise a deliberately out-of-order file could make a valid
    Gained/Lost pair look orphaned.
    """
    events, invalid = remove_invalid_events(events)
    events, duplicates = remove_duplicate_events(events)
    events = normalize_event_order(events)
    events, orphans = remove_orphaned_lost_focus(events)
    return events, {
        "invalid": invalid,
        "duplicates": duplicates,
        "orphans": orphans,
        "events_removed": invalid + duplicates + orphans,
    }


def updated_summary(summary, events, *, recalculate_first_last=False,
                    fallback_first=0, fallback_last=0,
                    preserve_empty=False):
    """Return an updated copy of a summary without mutating the input."""
    updated = dict(summary or {})
    update_summary(
        updated,
        events,
        recalculate_first_last=recalculate_first_last,
        fallback_first=fallback_first,
        fallback_last=fallback_last,
        preserve_empty=preserve_empty,
    )
    return updated


def repair_title(title, *, preserve_summary_only=True,
                 recalculate_first_last=False):
    """Repair one title's events and summary in place; return repair counts."""
    events, stats = repair_events(title.get("events", []))
    title["events"] = events
    summary = title.setdefault("summary", {})
    update_summary(
        summary,
        events,
        recalculate_first_last=recalculate_first_last,
        preserve_empty=preserve_summary_only,
    )
    stats["summary"] = summary
    return stats


def repair_export(data, *, preserve_summary_only=True,
                  recalculate_first_last=False):
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
            stats = repair_title(
                title,
                preserve_summary_only=preserve_summary_only,
                recalculate_first_last=recalculate_first_last,
            )
            totals["titles"] += 1
            for key in ("invalid", "duplicates", "orphans", "events_removed"):
                totals[key] += stats[key]
    return totals
