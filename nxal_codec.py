#!/usr/bin/env python3
"""
nxal_codec.py — NXAL event, session and export codec helpers.

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

# NXAL deduplicates events on import/merge by
# (type, titleID, eventType, clockTimestamp, steadyTimestamp). titleID/type are
# matched at the title/user level, so this is the shared per-event identity.
EVENT_IDENTITY_KEY = ("type", "clockTimestamp", "steadyTimestamp")

USER_ID_HEX_LENGTH = 32
GAME_ID_HEX_LENGTH = 16


def load_json(path):
    with open(path, encoding="utf-8") as f:
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


def focus_spans(events):
    """Return (clock start, clock end, credited seconds) for focus pairs.

    NXAL's countPlaytime() uses ``steadyTimestamp`` for a normal focus pair
    because that clock is monotonic. The wall clock is only the right choice
    when steady time is unavailable or a reboot made it non-monotonic. Keeping
    the credited duration here also gives callers the active intervals needed
    to place play correctly across hours, calendar days and midnight.

    Each Gained Focus is paired with at most one Lost Focus. NXAL's
    countPlaytime() can add an extra account-separated orphan OutFocus using
    the previous focus start; that is a logging artifact, so the adapter
    reports it through the session warning/Fix path instead of reproducing
    the double count here.
    """
    spans = []
    start_steady = None
    start_clock = None
    for ev in events:
        if ev.get("type") == "Gained Focus":
            start_steady = ev.get("steadyTimestamp")
            start_clock = ev.get("clockTimestamp")
        elif ev.get("type") == "Lost Focus" and start_clock is not None:
            end_steady = ev.get("steadyTimestamp")
            end_clock = ev.get("clockTimestamp")
            duration = None
            if (isinstance(start_steady, int)
                    and isinstance(end_steady, int)
                    and end_steady >= start_steady):
                duration = end_steady - start_steady
            elif (isinstance(start_clock, int)
                  and isinstance(end_clock, int)
                  and end_clock >= start_clock):
                duration = end_clock - start_clock
            if (duration is not None and duration >= 0
                    and isinstance(end_clock, int)):
                spans.append((start_clock, end_clock, duration))
            start_steady = None
            start_clock = None
    return spans


def focus_intervals(events):
    """Return (clock start, credited seconds) for completed focus pairs."""
    return [(start, duration) for start, _end, duration in focus_spans(events)]


def calculate_playtime(events):
    """Calculate credited playtime from completed Gained/Lost Focus pairs.

    This is the host-side active-play accounting used for logical session
    segments. It intentionally ignores account-separated orphan Lost Focus
    events; see calculate_nxal_playtime() for NXAL's export accounting.
    """
    return sum(duration for _start, duration in focus_intervals(events))


def _nxal_playtime_for_group(group):
    """CountPlaytime() accounting for one NXAL-recognized session group."""
    total = 0
    last_ts = 0
    index = 0
    while index < len(group):
        event = group[index]
        event_type = event.get("type")
        if event_type == "Launched":
            last_ts = 0
        elif event_type == "Gained Focus":
            last_ts = event.get("steadyTimestamp", 0)
        elif event_type == "Lost Focus":
            steady = event.get("steadyTimestamp")
            if isinstance(steady, int):
                total += steady - last_ts
            while (index + 1 < len(group)
                   and group[index + 1].get("type") == "Lost Focus"):
                index += 1
        index += 1
    return total


def calculate_nxal_playtime(events):
    """Reproduce NXAL ``countPlaytime()`` summary accounting.

    NXAL only counts sessions with an account event matching the active user;
    accountless groups are excluded. Within each session, repeated Lost Focus
    events are skipped, while a Lost Focus separated by account events is
    counted again from the previous focus start.
    """
    return sum(
        _nxal_playtime_for_group(group)
        for group in nxal_session_groups(events)
    )


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

    Consecutive OutFocus events are a known PDM logging artifact; NXAL's
    getPlayEvents() explicitly skips a Lost Focus whose immediate predecessor
    is another Lost Focus, so those are not reported as orphans. A Lost Focus
    separated from the previous one by an account event (or without any focus
    start) is the malformed case this detector is meant to surface.

    Launched/Closed boundaries reset the focus depth so an open focus pair in
    one session cannot mask an orphan in the next.
    """
    orphans = []
    depth = 0
    previous_type = None
    for i, ev in enumerate(events):
        event_type = ev.get("type")
        if event_type in ("Launched", "Closed"):
            depth = 0
        elif event_type == "Gained Focus":
            depth += 1
        elif event_type == "Lost Focus":
            if depth > 0:
                depth -= 1
            elif previous_type != "Lost Focus":
                orphans.append(i)
        previous_type = event_type
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
    """Return valid event mappings sorted by clock/steady timestamp.

    Non-mapping entries are ignored so a malformed persisted event cannot
    crash dashboard reads or repair tools. Validation still reports them
    from the original file.
    """
    return sorted(
        (event for event in events if isinstance(event, dict)),
        key=lambda event: (
            event.get("clockTimestamp", 0),
            event.get("steadyTimestamp", 0),
        ),
    )


def normalize_export(data):
    """Deep-sort dict keys alphabetically to match NXAL export output.

    NXAL serializes export JSON with nlohmann::json, whose default
    container (std::map) writes keys in sorted/alphabetical order
    (Application.ExportJob.cpp). So a pristine NXAL export always has
    title keys as (events, id, name, summary), user keys as (id, name,
    titles), etc. Our merge/dedup rebuild dicts in varying insertion
    orders and pass some titles through untouched, so two runs over the
    same data can byte-differ even when the parsed content is identical.
    Sorting every dict's keys here normalizes all outputs to be
    byte-identical to each other AND to an NXAL export.
    """
    if isinstance(data, dict):
        return {k: normalize_export(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return [normalize_export(v) for v in data]
    return data


def write_export_json(path, data):
    """Write an export to `path` byte-compatible with a NXAL export.

    NXAL writes export.json using nlohmann::json.dump(4) then a trailing
    std::endl (Application.ExportJob.cpp:118). Reading a NXAL file back
    with json.load and re-dumping must reproduce it byte-for-byte, so we:
      - sort keys (see normalize_export — nlohmann std::map sorts keys),
      - keep UTF-8 literal with ensure_ascii=False (NXAL does not escape
        non-ASCII title/name characters), and
      - append a trailing newline (the << std::endl).
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(normalize_export(data), indent=4, ensure_ascii=False) + "\n")


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
    valid_events = [
        event for event in events
        if isinstance(event, dict)
        and isinstance(event.get("clockTimestamp"), int)
    ]
    if not valid_events:
        return fallback_first, fallback_last

    # Use extrema rather than file order so imported events appended after
    # newer PDM events still produce a stable proxy.
    first_login = min(
        (ev["clockTimestamp"] for ev in valid_events
         if ev.get("type") == "Account Login"),
        default=None)
    last_focus = max(
        (ev["clockTimestamp"] for ev in valid_events
         if ev.get("type") == "Lost Focus"),
        default=None)

    earliest = min(ev["clockTimestamp"] for ev in valid_events)
    latest = max(ev["clockTimestamp"] for ev in valid_events)

    first_played = first_login if first_login is not None else earliest
    last_played = last_focus if last_focus is not None else latest
    return first_played, last_played


def format_timestamp(ts):
    """Format Unix timestamp to human-readable datetime."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def is_sorted(events):
    """Return True if valid events are sorted by clockTimestamp."""
    timestamps = [
        event.get("clockTimestamp") for event in events
        if isinstance(event, dict)
        and isinstance(event.get("clockTimestamp"), int)
    ]
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
            if (isinstance(ev, dict)
                    and ev.get("type") == "Launched"
                    and isinstance(ev.get("clockTimestamp"), int)):
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


def _title_session_pairs(titles):
    """Yield (user_name, title, event_group) for every native session group.

    A group opens at a "Launched" event and closes at the next "Closed"
    event. If a title ends without a "Closed" (crash), or a new Launched
    arrives first, the incomplete group is still emitted using the remaining
    events so the host can warn and repair it.
    """
    for t in titles:
        group = []
        in_group = False
        for ev in t.get("events", []):
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "Launched":
                if group and any(e["type"] == "Launched" for e in group):
                    yield group
                group = [ev]
                in_group = True
            elif in_group:
                group.append(ev)
                if ev.get("type") == "Closed":
                    yield group
                    group = []
                    in_group = False
        if group and any(e["type"] == "Launched" for e in group):
            yield group
    return


def normalize_event_order(events):
    """Normalize native event order without crossing session boundaries.

    Sorting the whole title array by timestamp interleaves two sessions that
    begin at the same clock/steady instant. NXAL can legitimately contain
    duplicate or near-duplicate sessions, so normalize each session group
    independently and then order the groups by their first timestamp.
    """
    valid_events = [event for event in events if isinstance(event, dict)]
    groups = list(_title_session_pairs([{"events": valid_events}]))
    if not groups:
        return sort_events(valid_events)
    # If the original order does not describe complete group coverage (for
    # example a deliberately reversed whole-title array), group boundaries
    # are unknowable. Fall back to a global stable sort; normal NXAL/imported
    # data has complete groups and keeps its boundaries.
    if sum(len(group) for group in groups) < len(valid_events):
        return sort_events(valid_events)
    groups_by_start = sorted(
        groups,
        key=lambda group: (
            group[0].get("clockTimestamp", 0),
            group[0].get("steadyTimestamp", 0),
        ),
    )
    normalized = []
    for group in groups_by_start:
        normalized.extend(sort_events(group))
    return normalized



def normalize_export_events(data):
    """Normalize event order for every title in an export in place."""
    for user in data.get("users", []):
        for title in user.get("titles", []):
            events = title.get("events")
            if isinstance(events, list):
                title["events"] = normalize_event_order(events)
    return data


def nxal_session_groups(events):
    """Return session groups NXAL would recognize for the title/user.

    NXAL's getPDSessions() requires an account event before it emits a
    session; accountless groups are therefore not counted in summary
    playtime or launches.
    """
    return [
        group for group in _title_session_pairs([{"events": events}])
        if any(event.get("type") in ("Account Login", "Account Logout")
               for event in group)
    ]


def count_nxal_launches(events):
    """Return NXAL's session-based launch count."""
    return len(nxal_session_groups(events))


def count_accountless_sessions(events):
    """Count session groups that have no account event."""
    return sum(
        1 for group in _title_session_pairs([{"events": events}])
        if not any(event.get("type") in ("Account Login", "Account Logout")
                   for event in group)
    )


def _parse_dt(value):
    """Parse 'YYYY-MM-DD HH:MM:SS' or ISO 'T' into epoch seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())


def _anchor_for(anchors, clock):
    """Pick the (steady, clock) anchor to derive `clock` from.

    Prefer the nearest preceding anchor, then the nearest following one.
    """
    preceding = [a for a in anchors if a[1] <= clock]
    following = [a for a in anchors if a[1] > clock]
    if preceding:
        return max(preceding, key=lambda a: a[1])
    if following:
        return min(following, key=lambda a: a[1])
    return None


def _steady_from(anchor, clock):
    """steadyTimestamp for `clock` given an (anchor steady, anchor clock).

    steadyTimestamp is the monotonic seconds-since-boot clock; across one
    boot session it advances in lockstep with clockTimestamp, so the offset
    from a neighbour carries over:
        steady = anchor steady + (clock - anchor clock).
    """
    if anchor is None:
        return 0
    return anchor[0] + (clock - anchor[1])


def _event_anchors(events):
    """Extract valid (steady, clock) anchor pairs from an event list."""
    return [
        (ev["steadyTimestamp"], ev["clockTimestamp"])
        for ev in events
        if isinstance(ev.get("steadyTimestamp"), int)
        and isinstance(ev.get("clockTimestamp"), int)
    ]


def _all_anchors(data):
    """Anchor pairs from every title in the export.

    Used when a title has no events yet (a game created by add_game), so a
    brand-new session still gets a steadyTimestamp consistent with the
    console's real boot offsets instead of a bogus 0.
    """
    anchors = []
    for user in data.get("users", []):
        for title in user.get("titles", []):
            anchors.extend(_event_anchors(title.get("events", [])))
    return anchors
