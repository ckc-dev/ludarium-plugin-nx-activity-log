#!/usr/bin/env python3
"""
nxal_common.py — Shared utilities for NX-ActivityLog verification scripts.

Provides the canonical helpers shared across validate/fix/merge/dedup so
they cannot drift apart: event identity (event_key/deduplicate_events),
deterministic ordering (sort_events), summary recomputation
(first_last_from_events), playtime/launch accounting, and the PDM-artifact
detectors (find_duplicate_events/find_orphaned_lost_focus).
"""

import json
from datetime import datetime

EVENT_TYPE_MAP = {
    "Launched": 0,
    "Closed": 1,
    "Gained Focus": 2,
    "Lost Focus": 3,
    "Account Login": 4,
    "Account Logout": 5,
}

VALID_EVENT_TYPES = set(EVENT_TYPE_MAP.keys())

SUMMARY_FIELDS = ["firstPlayed", "lastPlayed", "launches", "playtime"]

USER_ID_HEX_LENGTH = 32
GAME_ID_HEX_LENGTH = 16


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def hex_to_user_id_array(hex_id):
    return [int(hex_id[:16], 16), int(hex_id[16:], 16)]


def hex_to_game_id(hex_id):
    return int(hex_id, 16)


def is_valid_hex(s, expected_length):
    if not isinstance(s, str):
        return False
    if len(s) != expected_length:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def count_launches(events):
    """Count 'Launched' events in an events array."""
    return sum(1 for ev in events if ev.get("type") == "Launched")


def calculate_playtime(events):
    """Calculate playtime from focus events.

    Only counts completed focus periods (Gained Focus → Lost Focus),
    using clockTimestamp. Unclosed sessions are ignored since we can't
    know when they ended.
    """
    playtime = 0
    last_focus_start = None
    for ev in events:
        if ev.get("type") == "Gained Focus":
            last_focus_start = ev.get("clockTimestamp")
        elif ev.get("type") == "Lost Focus" and last_focus_start is not None:
            playtime += ev["clockTimestamp"] - last_focus_start
            last_focus_start = None
    return playtime


def find_duplicate_events(events):
    """Find events sharing the same (type, clockTimestamp) key.

    PDM logging artifact. Returns a list of (index, first_index, event)
    for each occurrence after the first.

    This is a broad 2-field *detection* heuristic used to flag potential
    PDM double-log artifacts (warnings), NOT a safe removal key. To
    actually delete duplicates use deduplicate_events with the strict
    3-field (type, clockTimestamp, steadyTimestamp) NXAL identity key —
    see the note there.
    """
    seen = {}
    duplicates = []
    for i, ev in enumerate(events):
        key = (ev["type"], ev["clockTimestamp"])
        if key in seen:
            duplicates.append((i, seen[key], ev))
        else:
            seen[key] = i
    return duplicates


def find_orphaned_lost_focus(events):
    """Find indices of 'Lost Focus' events without a preceding 'Gained Focus'.

    PDM artifact. Returns a list of event indices.
    """
    orphans = []
    depth = 0
    for i, ev in enumerate(events):
        if ev["type"] == "Gained Focus":
            depth += 1
        elif ev["type"] == "Lost Focus":
            if depth > 0:
                depth -= 1
            else:
                orphans.append(i)
    return orphans


def event_key(ev, key_fields):
    """Build a dedup identity tuple for an event from the given field names.

    key_fields is a tuple of event field names (e.g. ("type",
    "clockTimestamp", "steadyTimestamp")). Missing fields become None.
    """
    return tuple(ev.get(f) for f in key_fields)


def deduplicate_events(events, key_fields):
    """Remove duplicate events by a caller-specified key.

    Only removes events that are identical across `key_fields`. This is
    distinct from `find_duplicate_events` (the 2-field PDM-artifact
    detector): removal must be strict so that two genuinely different
    events (e.g. the same wall-clock second logged before and after a
    console reboot, which differ in steadyTimestamp) are never deleted.
    """
    seen = set()
    unique = []
    for ev in events:
        key = event_key(ev, key_fields)
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def sort_events(events):
    """Return `events` sorted by (clockTimestamp, steadyTimestamp).

    Two-level sort gives a deterministic byte order even when multiple
    events share the same clockTimestamp (e.g. NXAL consecutive events
    logged within the same second), so merged/deduplicated output does
    not depend on input ordering.
    """
    return sorted(events, key=lambda e: (e.get("clockTimestamp", 0), e.get("steadyTimestamp", 0)))


def normalize_export(data):
    """Rebuild an export dict with a fixed, canonical key order.

    Different code paths build title/user dicts in different orders (the
    source exports write "events" first; merge_title rebuilds as
    id/name/summary/events; deduplicate_exports as id/name/summary/events),
    and titles that are passed through untouched keep their original order.
    That makes two runs over the same data byte-differ even though the
    parsed contents are identical. Normalizing before serialization makes
    merge and dedup output byte-identical and order-independent.
    """
    SUMMARY_ORDER = ("firstPlayed", "lastPlayed", "launches", "playtime")
    TITLE_ORDER = ("id", "name", "summary", "events")
    USER_ORDER = ("id", "name", "titles")

    def ordered(obj, order):
        canonical = {k: obj[k] for k in order if k in obj}
        canonical.update({k: v for k, v in obj.items() if k not in order})
        return canonical

    def norm_event(ev):
        if not isinstance(ev, dict):
            return ev
        return {k: ev[k] for k in ("clockTimestamp", "steadyTimestamp", "type") if k in ev}

    def norm_title(t):
        if not isinstance(t, dict):
            return t
        out = ordered(t, TITLE_ORDER)
        if "summary" in out and isinstance(out["summary"], dict):
            out["summary"] = ordered(out["summary"], SUMMARY_ORDER)
        if "events" in out and isinstance(out["events"], list):
            out["events"] = [norm_event(e) for e in out["events"]]
        return out

    def norm_user(u):
        if not isinstance(u, dict):
            return u
        out = ordered(u, USER_ORDER)
        if "titles" in out and isinstance(out["titles"], list):
            out["titles"] = [norm_title(t) for t in out["titles"]]
        return out

    def norm_top(d):
        if not isinstance(d, dict):
            return d
        out = ordered(d, ("exportString", "exportTimestamp", "exportVersion", "users"))
        if "users" in out and isinstance(out["users"], list):
            out["users"] = [norm_user(u) for u in out["users"]]
        return out

    return norm_top(data)


def first_last_from_events(events, fallback_first=0, fallback_last=0):
    """Compute summary firstPlayed/lastPlayed from events (NXAL semantics).

    NXAL does NOT derive these from the events array. In
    getStatisticsForUser (PlayData.cpp:529-542) they come straight from
    the Nintendo Switch PDM firmware database
    (pdmqryQueryPlayStatisticsByApplicationIdAndUserAccountId) as
    first_timestamp_user / last_timestamp_user, falling back to the
    imported summary only when PDM returns 0/0.

    Empirically (verified against a pristine export), those PDM values map
    to the first "Account Login" event and the last "Lost Focus" event —
    NOT the raw Launched...Closed extremes. So when a tool must produce a
    summary, we recompute from the events with those semantics so the
    result stays faithful to what NXAL would report.

    Fallbacks (when the canonical event is absent):
      - no "Account Login"  -> earliest event's clockTimestamp
      - no "Lost Focus"     -> latest event's clockTimestamp
      - no events at all    -> the passed fallback values (source summary)

    Caveat: PDM can hold data from sessions not captured in the events
    array, so event-derived values are the best available proxy, not an
    exact reproduction of PDM (this most often affects lastPlayed).
    """
    if not events:
        return fallback_first, fallback_last

    first_login = next(
        (ev["clockTimestamp"] for ev in events if ev["type"] == "Account Login"), None
    )
    last_focus = next(
        (ev["clockTimestamp"] for ev in reversed(events) if ev["type"] == "Lost Focus"),
        None,
    )

    earliest = min(ev["clockTimestamp"] for ev in events)
    latest = max(ev["clockTimestamp"] for ev in events)

    first_played = first_login if first_login is not None else earliest
    last_played = last_focus if last_focus is not None else latest
    return first_played, last_played


def format_timestamp(ts):
    """Format Unix timestamp to human-readable datetime."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def is_sorted(events):
    """Return True if events are sorted by clockTimestamp (non-decreasing)."""
    timestamps = [ev["clockTimestamp"] for ev in events]
    return timestamps == sorted(timestamps)


def find_first_last_launch(titles):
    """Find the first and last 'Launched' event across titles.

    Returns (first, last), where each is a dict {"game", "timestamp"}
    or None if no Launched events exist.
    """
    first = None
    last = None

    for title in titles:
        for ev in title.get("events", []):
            if ev["type"] == "Launched":
                ts = ev["clockTimestamp"]
                info = {"game": title.get("name", "Unknown"), "timestamp": ts}

                if first is None or ts < first["timestamp"]:
                    first = info
                if last is None or ts > last["timestamp"]:
                    last = info

    return first, last


class VerificationResult:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)
        print(f"  [FAIL] {msg}")

    def warn(self, msg):
        self.warnings.append(msg)
        print(f"  [WARN] {msg}")

    def ok(self, msg):
        print(f"  [PASS] {msg}")

    @property
    def passed(self):
        return len(self.errors) == 0

    def summary(self):
        if self.passed:
            print(f"\nRESULT: ALL CHECKS PASSED ({len(self.warnings)} warning(s))")
        else:
            print(
                f"\nRESULT: FAILED — {len(self.errors)} error(s), "
                f"{len(self.warnings)} warning(s)"
            )
