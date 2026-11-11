"""NX-ActivityLog live adapter — read/write over a single NXAL import.json.

The Ludarium host treats an environment of type `nx_activity_log` as a LIVE
source backed by one `import.json` export (the adapters' env path). All reads
are derived from the JSON on each call; all mutations write back through
`nxal_codec.write_export_json` so the file stays byte-compatible with a
pristine NXAL export (sorted keys, UTF-8 literal, trailing newline).

Session model (mirrors NXAL / PlayData.cpp):
  - A "session" is a Launched…Closed group inside a title's events array.
  - start       = the Launched event's clockTimestamp
  - duration    = credited focus playtime, derived from steadyTimestamp
                  between the group's Gained Focus / Lost Focus events
  - end_time    = wall-clock close (or last observed event) of the group
  - game        = the title; game_id = 'nx_' + title.id (hex), name = title.name
  - firstPlayed/lastPlayed/lastPlayed derived via nxal_codec (first Login /
    last Lost Focus semantics, falling back to event extremes).

The 18-method surface below is the GameTrackerAdapter contract. The heavy
advanced operations (merge/dedup/verify/fix) live in plugin_tools.py /
plugin_health.py and reuse the in-repo CLI logic (nxal_codec + merge_exports /
deduplicate_exports / fix_import / validate_import).
"""

import copy
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nxal_codec import (
    _all_anchors,
    _anchor_for,
    _event_anchors,
    _parse_dt,
    _steady_from,
    _title_session_pairs,
    calculate_playtime,
    count_launches,
    first_last_from_events,
    focus_intervals,
    focus_spans,
    format_timestamp,
    load_json,
    sort_events,
    write_export_json,
)

from plugin_sdk import AdapterOperationError


def _group_end_clock(group):
    """NXAL's PlaySession end: Closed, else the last Lost Focus/event."""
    for event in reversed(group):
        if event.get("type") in ("Closed", "Lost Focus"):
            value = event.get("clockTimestamp")
            if isinstance(value, int):
                return value
    for event in reversed(group):
        value = event.get("clockTimestamp")
        if isinstance(value, int):
            return value
    return None


def _group_active_segments(group):
    """Native active focus intervals, in the adapter's timestamp format."""
    return [
        {"start_time": format_timestamp(start), "duration": duration}
        for start, duration in focus_intervals(group)
        if isinstance(start, int)
    ]


def _group_fingerprint(group):
    """Exact native event identity, used to detect duplicate groups."""
    return tuple(
        (event.get("type"), event.get("clockTimestamp"),
         event.get("steadyTimestamp"))
        for event in group
    )


def _segment_part_id(title_id, session_start, segment_number, segment_start):
    return f"{title_id}|{session_start}|{segment_number}|{segment_start}"


def _decode_segment_part_id(value):
    text = str(value)
    try:
        (title_id, session_start, segment_number,
         segment_start) = text.rsplit("|", 3)
        return (title_id, int(session_start), int(segment_number),
                int(segment_start))
    except (TypeError, ValueError) as error:
        raise AdapterOperationError(
            f"Invalid play segment: {value!r}") from error


def _build_session_events(start_ts, end_ts, segments, complete, anchor,
                          start_steady=None):
    """Build a complete NXAL lifecycle from logical active-play segments.

    ``segments`` is a sorted list of ``(start, end, credited_duration)``.
    Account and boundary events are generated; callers never edit them.
    A Lost Focus steady timestamp is derived from the segment's credited
    duration, not from the wall-clock end, so rebuilding a session preserves
    steady-time playtime across NTP/clock jumps.
    """
    if end_ts < start_ts:
        raise AdapterOperationError("Session end precedes its start.")
    if anchor is None:
        # A brand-new export has no native steady-clock anchor. Keep the
        # synthesized block internally consistent instead of writing zeros.
        anchor = (start_steady if isinstance(start_steady, int) else 0,
                  start_ts)
    previous_end = start_ts
    for segment_start, segment_end, duration in segments:
        if duration < 0:
            raise AdapterOperationError(
                "Play segment duration cannot be negative.")
        if segment_start < start_ts or segment_end > end_ts:
            raise AdapterOperationError(
                "A play segment must stay inside its session.")
        if segment_end < segment_start:
            raise AdapterOperationError(
                "A play segment cannot end before it starts.")
        if segment_start < previous_end:
            raise AdapterOperationError("Play segments cannot overlap.")
        previous_end = segment_end

    launch_steady = (start_steady if isinstance(start_steady, int)
                     else _steady_from(anchor, start_ts))
    events = [{
        "type": "Launched",
        "clockTimestamp": start_ts,
        "steadyTimestamp": launch_steady,
    }]
    last_end = start_ts
    last_steady = launch_steady
    for index, (segment_start, segment_end, duration) in enumerate(segments):
        gained_steady = max(
            _steady_from(anchor, segment_start), last_steady)
        events.append({
            "type": "Gained Focus",
            "clockTimestamp": segment_start,
            "steadyTimestamp": gained_steady,
        })
        if index == 0:
            login = min(segment_start + 1, segment_end)
            login_steady = min(
                max(_steady_from(anchor, login), gained_steady),
                gained_steady + duration)
            events.append({
                "type": "Account Login",
                "clockTimestamp": login,
                "steadyTimestamp": login_steady,
            })
        lost_steady = gained_steady + duration
        events.append({
            "type": "Lost Focus",
            "clockTimestamp": segment_end,
            "steadyTimestamp": lost_steady,
        })
        last_end = segment_end
        last_steady = lost_steady

    if complete:
        if segments:
            logout = min(last_end + 1, end_ts)
        else:
            login = min(start_ts + 1, end_ts)
            login_steady = max(_steady_from(anchor, login), last_steady)
            events.append({
                "type": "Account Login",
                "clockTimestamp": login,
                "steadyTimestamp": login_steady,
            })
            last_steady = login_steady
            logout = min(start_ts + 2, end_ts)
        logout_steady = max(_steady_from(anchor, logout), last_steady)
        events.append({
            "type": "Account Logout",
            "clockTimestamp": logout,
            "steadyTimestamp": logout_steady,
        })
        events.append({
            "type": "Closed",
            "clockTimestamp": end_ts,
            "steadyTimestamp": max(
                _steady_from(anchor, end_ts), logout_steady),
        })
    return events


def _warning_text(incomplete, duplicate):
    parts = []
    if incomplete:
        parts.append(
            "Incomplete session: NXAL has no Closed event. Use Fix to close it.")
    if duplicate:
        parts.append(
            "Duplicate session: another session has the same native event "
            "group. Use Fix to remove the duplicate.")
    return " ".join(parts)


def _session_id(user_name, title, group):
    """Stable host-side identity for one native NXAL session.

    The Launched event's (clock, steady) pair is stable across duration edits,
    part edits and incomplete-session repair. Including the NXAL profile and
    title id separates sessions that start in the same second in different
    games or profiles. Two completely identical Launched events in one title
    remain inherently indistinguishable without a persistent native id, and
    two different groups sharing that start identity also collide because the
    id is anchored to the start rather than the full event body.
    """
    launched = group[0] if group else {}
    parts = [
        str(user_name),
        str(title.get("id", "")),
        str(launched.get("type")),
        str(launched.get("clockTimestamp")),
        str(launched.get("steadyTimestamp")),
    ]
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"nx_{digest[:24]}"


class NXALGameTrackerAdapter:
    """GameTrackerAdapter contract implemented over live NXAL import.json."""

    def __init__(self, source_id, display_name, path, default_user_id=1,
                 config=None):
        self.source_id = source_id
        self.display_name = display_name
        self.path = path
        self.type = "nx_activity_log"
        self._messages = []
        self.config = config or {}
        self.default_user_id = self._config_default_user(default_user_id)
        self.profile_mapping = self._config_profile_mapping()
        self._cache = None
        self._cache_stamp = None

    # NXAL supports live edits, but its raw export/import round-trip is
    # lossy (users/events are reconstructed), so it must not be offered as a
    # database-merge endpoint until that contract is made complete.
    supports_session_write = True
    supports_session_ids = True
    supports_session_repair = True
    supports_game_write = True
    supports_game_merge = True
    supports_raw_transfer = False

    # ---------- host integration ----------
    def report(self, message):
        if not hasattr(self, "_messages"):
            self._messages = []
        self._messages.append(message)

    def drain_messages(self):
        msgs = getattr(self, "_messages", [])
        self._messages = []
        return msgs

    # ---------- per-environment identity config ----------
    def _config_default_user(self, fallback):
        """The environment default, with a legacy config override if set."""
        raw = self.config.get("default_user_id")
        if raw in (None, ""):
            return fallback
        try:
            return int(raw)
        except (TypeError, ValueError):
            self.report(
                f"Ignoring invalid default_user_id config: {raw!r}")
            return fallback

    def _config_profile_mapping(self):
        """Map an NXAL profile name to a Ludarium user id.

        The value is stored as a JSON object in environments.config because
        the environment form only has scalar fields. A malformed mapping is
        reported and treated as empty rather than breaking the dashboard.
        """
        raw = self.config.get("profile_mapping")
        if raw in (None, ""):
            return {}
        if isinstance(raw, dict):
            mapping = raw
        else:
            try:
                mapping = json.loads(raw)
            except (TypeError, ValueError):
                self.report("Ignoring invalid profile_mapping JSON.")
                return {}
        if not isinstance(mapping, dict):
            self.report("profile_mapping must be a JSON object.")
            return {}
        result = {}
        for name, user_id in mapping.items():
            try:
                result[str(name).strip().casefold()] = int(user_id)
            except (TypeError, ValueError):
                self.report(
                    f"Ignoring invalid profile mapping for {name!r}: "
                    f"{user_id!r}")
        return result

    def _user_id_for(self, user_name):
        key = str(user_name or "").strip().casefold()
        return self.profile_mapping.get(key, self.default_user_id)

    # ---------- storage ----------
    #
    # The export is a ~1MB JSON document and the host calls _load() once per
    # session while rendering a dashboard, so an uncached read re-parses the
    # whole file hundreds of times per page (~3s at 400+ sessions). We cache
    # the parsed document keyed on (mtime, size) so repeated reads inside one
    # request are free, while any external write still invalidates the cache.
    def _load(self):
        try:
            stat = os.stat(self.path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None

        if stamp is not None and self._cache is not None and self._cache_stamp == stamp:
            return self._cache

        data = load_json(self.path)
        self._cache = data
        try:
            stat = os.stat(self.path)
            self._cache_stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._cache_stamp = None
        return data

    def _save(self, data):
        write_export_json(self.path, data)
        # Keep the cache: `data` is the object we just serialized, and the
        # refreshed stamp below prevents a stale read of our own write.
        self._cache = data
        try:
            stat = os.stat(self.path)
            self._cache_stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._cache_stamp = None

    def _invalidate_cache(self):
        self._cache = None
        self._cache_stamp = None

    def _titles(self, data):
        for u in data.get("users", []):
            for t in u.get("titles", []):
                yield u.get("name", "?"), t

    def _find_title(self, data, game_id):
        prefix = "nx_"
        hex_id = str(game_id)
        if hex_id.startswith(prefix):
            hex_id = hex_id[len(prefix):]
        for _, t in self._titles(data):
            if str(t.get("id", "")).lower() == hex_id.lower():
                return t
        return None

    def _find_session_by_id(self, data, session_id):
        """Return (user_name, title, group) for a synthetic session id."""
        wanted = str(session_id)
        for user_name, title in self._titles(data):
            for group in _title_session_pairs([title]):
                if _session_id(user_name, title, group) == wanted:
                    return user_name, title, group
        return None

    @staticmethod
    def _group_start_index(events, group):
        """Find the list index of the exact first event object in a group."""
        first = group[0]
        for index, event in enumerate(events):
            if event is first:
                return index
        raise AdapterOperationError("Could not locate the session event group.")

    def _remove_session_by_id(self, data, session_id):
        found = self._find_session_by_id(data, session_id)
        if found is None:
            return False
        _user_name, title, group = found
        events = title.setdefault("events", [])
        start = self._group_start_index(events, group)
        end = start + len(group)
        del events[start:end]
        self._recompute_title(data, title)
        return True

    def _find_group_by_start(self, title, target):
        """Return (start index, end index, group) for one session start."""
        events = title.setdefault("events", [])
        group_start = None
        for index, event in enumerate(events):
            if (event.get("type") == "Launched"
                    and event.get("clockTimestamp") == target):
                group_start = index
                break
        if group_start is None:
            return None
        group_end = group_start + 1
        while (group_end < len(events)
               and events[group_end].get("type") not in ("Launched", "Closed")):
            group_end += 1
        if (group_end < len(events)
                and events[group_end].get("type") == "Closed"):
            group_end += 1
        return group_start, group_end, events[group_start:group_end]

    def _find_group_containing(self, title, timestamp):
        events = title.setdefault("events", [])
        for group in _title_session_pairs([title]):
            start = group[0].get("clockTimestamp")
            end = _group_end_clock(group)
            if (isinstance(start, int) and isinstance(end, int)
                    and start <= timestamp <= end):
                index = self._group_start_index(events, group)
                return index, index + len(group), group
        return None

    def _rebuild_group(self, data, title, group_start, group_end,
                       segments, complete):
        """Replace one event group from sorted logical play segments."""
        events = title.setdefault("events", [])
        group = events[group_start:group_end]
        start_ts = group[0].get("clockTimestamp")
        if not isinstance(start_ts, int):
            raise AdapterOperationError(
                "The session has no valid Launched timestamp.")
        segments = sorted(segments, key=lambda segment: segment[0])
        end_ts = _group_end_clock(group)
        if not isinstance(end_ts, int):
            end_ts = start_ts
        maximal_segment_end = max(
            (segment[1] for segment in segments), default=start_ts)
        if complete:
            # A complete session's Closed event is its fixed boundary. Do
            # not let a segment edit silently move that boundary.
            if maximal_segment_end > end_ts:
                raise AdapterOperationError(
                    "A play segment extends past the session boundary.")
            if not any(event.get("type") == "Closed" for event in group):
                # Repairing an incomplete session needs room for Account
                # Logout and Closed after the final focus interval.
                candidate_end = max(end_ts, maximal_segment_end) + 2
                if group_end < len(events):
                    next_start = events[group_end].get("clockTimestamp")
                    if isinstance(next_start, int) and candidate_end >= next_start:
                        candidate_end = next_start - 1
                if candidate_end < maximal_segment_end:
                    raise AdapterOperationError(
                        "There is no room to close this session before the "
                        "next session starts.")
                end_ts = candidate_end
        else:
            # Incomplete sessions have no authoritative Closed boundary yet;
            # allow a later segment and let the user repair the session.
            end_ts = max(end_ts, maximal_segment_end)
        launched_steady = group[0].get("steadyTimestamp")
        if isinstance(launched_steady, int):
            anchor = (launched_steady, start_ts)
        else:
            anchor = _anchor_for(
                _event_anchors(events) or _all_anchors(data), start_ts)
        replacement = _build_session_events(
            start_ts, end_ts, segments, complete, anchor,
            start_steady=launched_steady)
        events[group_start:group_end] = replacement
        events[:] = sort_events(events)
        self._recompute_title(data, title)

    def _segments_for_group(self, group):
        return [
            (start, end, duration)
            for start, end, duration in focus_spans(group)
            if isinstance(start, int) and isinstance(end, int)
        ]

    # ---------- game dictionary ----------
    def _game_entries(self, data):
        out = []
        for user_name, t in self._titles(data):
            game_id = "nx_" + str(t["id"])
            existing = next((g for g in out if g["game_id"] == game_id), None)
            if existing:
                existing["stored_duration"] = existing.get("stored_duration", 0) \
                    + t.get("summary", {}).get("playtime", 0)
                continue
            first, last = first_last_from_events(
                t.get("events", []),
                t.get("summary", {}).get("firstPlayed", 0),
                t.get("summary", {}).get("lastPlayed", 0))
            out.append({
                "game_id": game_id,
                "name": t.get("name", "Unknown"),
                "name_hex": str(t["id"]),
                "user": user_name,
                "stored_duration": t.get("summary", {}).get("playtime", 0),
                "last_session": last or first or 0,
            })
        return out

    def _find_game(self, games_list, game_id):
        return next((g for g in games_list if g["game_id"] == str(game_id)), None)

    # ------------------------------------------------------------------
    # The 18-method contract
    # ------------------------------------------------------------------
    def fetch_dashboard_data(self):
        data = self._load()
        games_list = self._game_entries(data)
        sessions, total = [], 0
        seen_groups = {}
        for user_name, t in self._titles(data):
            game_id = "nx_" + str(t["id"])
            cleared = False
            for group in _title_session_pairs([t]):
                duration = calculate_playtime(group)
                start_ts = group[0]["clockTimestamp"]
                end_ts = _group_end_clock(group) or start_ts
                incomplete = not any(
                    event.get("type") == "Closed" for event in group)
                group_key = (
                    user_name, str(t.get("id", "")), _group_fingerprint(group))
                duplicate_index = seen_groups.get(group_key)
                duplicate = duplicate_index is not None
                cleared = True
                sessions.append({
                    "game_id": game_id,
                    "name": t.get("name", "Unknown"),
                    "native_user_id": self._user_id_for(user_name),
                    "session_id": _session_id(user_name, t, group),
                    "date_time": format_timestamp(start_ts),
                    "end_time": format_timestamp(end_ts),
                    "active_segments": _group_active_segments(group),
                    "duration": duration,
                    "session_warning": _warning_text(incomplete, duplicate),
                    "repair_available": incomplete or duplicate,
                })
                current_index = len(sessions) - 1
                if duplicate_index is None:
                    seen_groups[group_key] = current_index
                else:
                    first = sessions[duplicate_index]
                    first["session_warning"] = _warning_text(
                        "Incomplete session:" in first["session_warning"],
                        True)
                    first["repair_available"] = True
                total += duration
            if not cleared:
                summary = t.get("summary", {})
                stored = summary.get("playtime", 0)
                first_ts = summary.get("firstPlayed", 0) or 0
                last_ts = summary.get("lastPlayed", 0) or first_ts
                if stored > 0:
                    last_ts = max(last_ts, first_ts + stored)
                    sessions.append({
                        "game_id": game_id,
                        "name": t.get("name", "Unknown"),
                        "native_user_id": self._user_id_for(user_name),
                        "date_time": format_timestamp(first_ts),
                        "end_time": format_timestamp(last_ts),
                        "active_segments": [{
                            "start_time": format_timestamp(first_ts),
                            "duration": stored,
                        }],
                        "duration": stored,
                    })
                    total += stored
        # sort chronologically
        # Titles whose event groups cannot be reconstructed still own stored
        # playtime (export summary); surface one summary session so it is not
        # silently hidden.
        seen_game = {s["game_id"] for s in sessions}
        for g in games_list:
            stored = g.get("stored_duration", 0)
            if g["game_id"] in seen_game or stored <= 0:
                continue
            first_ts = g.get("last_session") or 0
            sessions.append({
                "game_id": g["game_id"],
                "name": g.get("name", "Unknown"),
                "native_user_id": self.default_user_id,
                "date_time": format_timestamp(first_ts),
                "end_time": format_timestamp(first_ts + stored),
                "active_segments": [{
                    "start_time": format_timestamp(first_ts),
                    "duration": stored,
                }],
                "duration": stored,
            })
            total += stored
        sessions.sort(key=lambda s: s["date_time"])
        return games_list, sessions, total

    def get_all_games(self):
        return [{"game_id": g["game_id"], "name": g["name"]}
                for g in self._game_entries(self._load())]

    # --- Optional capability: launch counts ---
    # NXAL is the only source that knows how OFTEN a game was opened: its
    # export carries summary.launches per title, and the host's session list
    # cannot derive it because two Launched events can reconstruct into one
    # session. Steam has no launch column at all, so this stays opt-in.
    supports_game_stats = True

    def get_game_stats(self):
        out = {}
        for _, t in self._titles(self._load()):
            launches = t.get("summary", {}).get("launches")
            if not launches:
                # summary is only refreshed by recalculate_totals; count from
                # the live event stream so a fresh export still reports them.
                launches = count_launches(t.get("events", []))
            if not launches:
                continue
            # The same title for two users is one canonical game: add up.
            game_id = "nx_" + str(t["id"])
            out.setdefault(game_id, {"launches": 0})
            out[game_id]["launches"] += int(launches)
        return out

    def add_game(self, game_id, name):
        data = copy.deepcopy(self._load())
        if self._find_title(data, game_id) is None:
            # give the title an ID from the provided game_id (strip nx_)
            hex_id = str(game_id).removeprefix("nx_")
            title = {
                "id": hex_id,
                "name": name,
                "events": [],
                "summary": {
                    "firstPlayed": 0,
                    "lastPlayed": 0,
                    "playtime": 0,
                    "launches": 0,
                },
            }
            if data.get("users"):
                data["users"][0].setdefault("titles", []).append(title)
            else:
                data["users"] = [{"name": "Unknown", "titles": [title]}]
            self._save(data)
            self.report(f"Added game '{name}' to '{self.display_name}'")

    def delete_game(self, game_id):
        data = copy.deepcopy(self._load())
        hex_id = str(game_id).removeprefix("nx_").lower()
        for u in data.get("users", []):
            u["titles"] = [t for t in u.get("titles", [])
                           if str(t.get("id", "")).lower() != hex_id]
        data["users"] = [u for u in data.get("users", [])
                         if u.get("titles")]
        self._save(data)

    def get_game_merge_candidates(self):
        """Duplicate titles with the same name inside one NXAL user."""
        candidates = []
        data = self._load()
        for user in data.get("users", []):
            by_name = {}
            for title in user.get("titles", []):
                name = str(title.get("name") or "").strip()
                if name:
                    by_name.setdefault(name.casefold(), []).append(title)
            for titles in by_name.values():
                if len(titles) < 2:
                    continue
                instances = []
                for title in titles:
                    summary = title.get("summary", {})
                    events = title.get("events", [])
                    duration = summary.get("playtime")
                    if duration is None:
                        duration = calculate_playtime(events)
                    first, last = first_last_from_events(
                        events, summary.get("firstPlayed", 0),
                        summary.get("lastPlayed", 0))
                    instances.append({
                        "game_id": "nx_" + str(title.get("id", "")),
                        "actual_duration": duration,
                        "last_session": (format_timestamp(last)
                                         if last else None),
                    })
                instances.sort(
                    key=lambda row: row["actual_duration"], reverse=True)
                candidates.append({
                    "name": str(titles[0].get("name") or ""),
                    "instances": instances,
                })
        return candidates

    def apply_game_merges(self, primary_id, duplicate_ids):
        self._merge_titles(primary_id, duplicate_ids)

    def add_session(self, game_id, start_time, duration):
        data = copy.deepcopy(self._load())
        title = self._find_title(data, game_id)
        if title is None:
            self.add_game(game_id, str(game_id))
            data = self._load()
            title = self._find_title(data, game_id)
        if title is None:
            raise AdapterOperationError(
                f"Could not create or find title {game_id!r} in "
                f"'{self.display_name}'.")
        start_ts = _parse_dt(start_time)
        events = title.setdefault("events", [])
        duration = max(0, int(duration))
        # One anchor for the whole synthesized block: steady stays monotonic
        # inside it, matching a real Launched…Closed group.
        anchor = _anchor_for(_event_anchors(events) or _all_anchors(data), start_ts)
        segment_start = start_ts + 2
        segment_end = segment_start + duration
        events.extend(_build_session_events(
            start_ts,
            segment_end + 3,
            [(segment_start, segment_end, duration)],
            complete=True,
            anchor=anchor,
        ))
        # canonical (clock, steady) order — see nxal_codec.sort_events
        events[:] = sort_events(events)
        self._recompute_title(data, title)
        self._save(data)

    def _remove_session_from_title(self, events, start_ts):
        """Remove one Launched..Closed group from a title's events array."""
        group_start = None
        for index, event in enumerate(events):
            if (event.get("type") == "Launched"
                    and event.get("clockTimestamp") == start_ts):
                group_start = index
                break
        if group_start is None:
            return False
        end_i = group_start + 1
        while end_i < len(events) and events[end_i].get("type") != "Closed":
            end_i += 1
        if end_i < len(events):
            end_i += 1
        del events[group_start:end_i]
        return True

    def _resize_last_focus_pair(self, events, group_start, group_end,
                                desired_duration):
        """Adjust only the final focus pair, preserving the rest of the group."""
        pairs = []
        gained_index = None
        for index in range(group_start, group_end):
            event = events[index]
            if event.get("type") == "Gained Focus":
                gained_index = index
            elif (event.get("type") == "Lost Focus"
                  and gained_index is not None):
                pairs.append((gained_index, index))
                gained_index = None
        if not pairs:
            if desired_duration:
                raise AdapterOperationError(
                    "This session has no focus interval to resize.")
            return
        gained_index, lost_index = pairs[-1]
        gained = events[gained_index]
        lost = events[lost_index]
        gained_clock = gained.get("clockTimestamp")
        lost_clock = lost.get("clockTimestamp")
        if not isinstance(gained_clock, int) or not isinstance(lost_clock, int):
            raise AdapterOperationError(
                "The session focus interval has invalid timestamps.")
        gained_steady = gained.get("steadyTimestamp")
        lost_steady = lost.get("steadyTimestamp")
        if (isinstance(gained_steady, int)
                and isinstance(lost_steady, int)
                and lost_steady >= gained_steady):
            pair_duration = lost_steady - gained_steady
        else:
            pair_duration = lost_clock - gained_clock
        old_focus = calculate_playtime(events[group_start:group_end])
        target_focus = int(desired_duration) - (old_focus - pair_duration)
        if target_focus < 0:
            raise AdapterOperationError(
                "The edited duration is shorter than the session's other "
                "play segments.")
        # The target is the final pair's credited duration; other pairs are
        # unchanged. Shift the pair and every later event together so the
        # complete session stays ordered.
        if target_focus < 1 and desired_duration:
            raise AdapterOperationError(
                "The edited duration would remove the final focus interval.")
        shift = target_focus - pair_duration
        lost["clockTimestamp"] = lost_clock + shift
        if isinstance(lost_steady, int):
            lost["steadyTimestamp"] = lost_steady + shift
        for index in range(lost_index + 1, group_end):
            event = events[index]
            clock = event.get("clockTimestamp")
            if isinstance(clock, int):
                event["clockTimestamp"] = clock + shift
            if isinstance(event.get("steadyTimestamp"), int):
                event["steadyTimestamp"] += shift

    def _replace_session(self, data, game_id, old_start, new_start,
                         duration, session_id=None):
        """Move/resize one existing group without rebuilding its event list."""
        if session_id is not None:
            found = self._find_session_by_id(data, session_id)
            if found is None:
                raise AdapterOperationError(
                    f"Session {session_id!r} no longer exists in "
                    f"'{self.display_name}'.")
            _user_name, title, group = found
            events = title.setdefault("events", [])
            group_start = self._group_start_index(events, group)
            group_end = group_start + len(group)
            old_ts = group[0].get("clockTimestamp")
            if not isinstance(old_ts, int):
                raise AdapterOperationError(
                    "The selected session has no valid Launched timestamp.")
        else:
            title = self._find_title(data, game_id)
            if title is None:
                raise AdapterOperationError(
                    f"Could not find title {game_id!r} in "
                    f"'{self.display_name}'.")
            old_ts = _parse_dt(old_start)
            events = title.setdefault("events", [])
            group_start = None
            for index, event in enumerate(events):
                if (event.get("type") == "Launched"
                        and event.get("clockTimestamp") == old_ts):
                    group_start = index
                    break
            if group_start is None:
                raise AdapterOperationError(
                    f"No session starts at {old_start!r} in "
                    f"'{self.display_name}'.")
            group_end = group_start + 1
            while (group_end < len(events)
                   and events[group_end].get("type") not in
                   ("Launched", "Closed")):
                group_end += 1
            if (group_end < len(events)
                    and events[group_end].get("type") == "Closed"):
                group_end += 1
            group = events[group_start:group_end]
        new_ts = _parse_dt(new_start)
        delta = new_ts - old_ts
        for event in group:
            clock = event.get("clockTimestamp")
            if isinstance(clock, int):
                event["clockTimestamp"] = clock + delta
            if isinstance(event.get("steadyTimestamp"), int):
                event["steadyTimestamp"] += delta
        desired_duration = max(0, int(duration))
        if calculate_playtime(group) != desired_duration:
            self._resize_last_focus_pair(
                events, group_start, group_end, desired_duration)
        events[:] = sort_events(events)
        self._recompute_title(data, title)

    def update_session(self, game_id, duration, new_start, old_start,
                       session_id=None):
        data = copy.deepcopy(self._load())
        self._replace_session(
            data, game_id, old_start, new_start, duration,
            session_id=session_id)
        self._save(data)

    def delete_session(self, original_date_time, session_id=None):
        data = copy.deepcopy(self._load())
        if session_id is not None:
            if not self._remove_session_by_id(data, session_id):
                raise AdapterOperationError(
                    f"Session {session_id!r} no longer exists in "
                    f"'{self.display_name}'.")
            self._save(data)
            return
        target = _parse_dt(original_date_time)
        found = False
        for _user_name, t in self._titles(data):
            events = t.setdefault("events", [])
            # find the group starting at target
            group_start = None
            for i, ev in enumerate(events):
                if ev.get("type") == "Launched" and ev["clockTimestamp"] == target:
                    group_start = i
                    break
            if group_start is None:
                continue
            found = True
            end_i = group_start + 1
            while end_i < len(events) and events[end_i].get("type") != "Closed":
                end_i += 1
            if end_i < len(events):
                end_i += 1  # include the Closed event
            del events[group_start:end_i]
            self._recompute_title(data, t)
        if not found:
            raise AdapterOperationError(
                f"No session starts at {original_date_time!r} "
                f"in '{self.display_name}'.")
        self._save(data)

    # --- Optional capability: session PARTS (per-event detail surface) ---
    # NXAL CAN decompose a reconstructed session into its constituent real
    # events. Each event inside the group's Launched..Closed span is a part,
    # keyed by its own clockTimestamp. Breaks/pauses are NOT entities: a Lost
    # Focus part's `duration` IS the pause until the next Gained Focus (the
    # adapter never stores or edits a pause directly).
    supports_session_parts = True

    # NOTE: actual-vs-stored honesty for NXAL needs NO adapter persisted part
    # rows: a session "part" here simply IS the existing real event inside the
    # group's Launched..Closed span, keyed by its own clockTimestamp. The host
    # surfaces a part only after calling supports_session_parts (capability
    # flag), and every part edit re-writes that one event via _recompute_title
    # + _save — the SAME trio add/update/delete_session already end with, so
    # stored_duration and actual_duration both recompute from the same edited
    # event stream. No phantom, no drift: a pause is never an entity — it is
    # the duration a Lost Focus part owns until the next Gained Focus.

    def _remove_duplicate_groups(self, data, title, target_group):
        """Remove exact duplicate groups, keeping the target group."""
        fingerprint = _group_fingerprint(target_group)
        matches = [
            group for group in _title_session_pairs([title])
            if _group_fingerprint(group) == fingerprint
        ]
        if len(matches) < 2:
            return False
        events = title.setdefault("events", [])
        target_first = target_group[0]
        for group in matches:
            if group and group[0] is target_first:
                continue
            start = self._group_start_index(events, group)
            del events[start:start + len(group)]
        self._recompute_title(data, title)
        return True

    def repair_incomplete_session(self, game_id, date_time,
                                  session_id=None):
        """Close an incomplete group and add missing account attribution."""
        data = copy.deepcopy(self._load())
        if session_id is not None:
            found = self._find_session_by_id(data, session_id)
            if found is None:
                raise AdapterOperationError(
                    f"Session {session_id!r} no longer exists in "
                    f"'{self.display_name}'.")
            _user_name, title, group = found
            events = title.setdefault("events", [])
            group_start = self._group_start_index(events, group)
            group_end = group_start + len(group)
            target = group[0].get("clockTimestamp")
            if not isinstance(target, int):
                raise AdapterOperationError(
                    "The selected session has no valid Launched timestamp.")
        else:
            title = self._find_title(data, game_id)
            if title is None:
                raise AdapterOperationError(
                    f"Could not find title {game_id!r} in "
                    f"'{self.display_name}'.")
            target = _parse_dt(date_time)
            events = title.setdefault("events", [])
            group_start = None
            for index, event in enumerate(events):
                if (event.get("type") == "Launched"
                        and event.get("clockTimestamp") == target):
                    group_start = index
                    break
            if group_start is None:
                raise AdapterOperationError(
                    f"No session starts at {date_time!r} in "
                    f"'{self.display_name}'.")
            group_end = group_start + 1
            while (group_end < len(events)
                   and events[group_end].get("type") not in
                   ("Launched", "Closed")):
                group_end += 1
            group = events[group_start:group_end]
        duplicate_removed = self._remove_duplicate_groups(data, title, group)
        if any(event.get("type") == "Closed" for event in group):
            if duplicate_removed:
                self._save(data)
            return
        if duplicate_removed:
            # Removing an earlier duplicate shifts the target group.
            events = title.setdefault("events", [])
            group_start = self._group_start_index(events, group)
            group_end = group_start + len(group)
        segments = self._segments_for_group(group)
        self._rebuild_group(
            data, title, group_start, group_end, segments, complete=True)
        self._save(data)

    def get_session_parts(self, game_id, original_date_time):
        data = self._load()
        try:
            target = _parse_dt(original_date_time)
        except (TypeError, ValueError):
            return []
        hex_id = str(game_id)
        if hex_id.startswith("nx_"):
            hex_id = hex_id[len("nx_"):]
        title = self._find_title(data, "nx_" + hex_id)
        if title is None:
            return []
        found = self._find_group_by_start(title, target)
        if found is None:
            return []
        _group_start, _group_end, group = found
        parts = []
        previous_end = None
        for index, (start, end, duration) in enumerate(
                self._segments_for_group(group), start=1):
            parts.append({
                "part_id": _segment_part_id(
                    str(title.get("id", "")), target, index, start),
                "name": f"Play segment {index}",
                "type": "play",
                "start_time": format_timestamp(start),
                "end_time": format_timestamp(end),
                "duration": duration,
                "editable": True,
                "break_before_seconds": (
                    max(0, start - previous_end)
                    if previous_end is not None else 0),
                "original_date_time": format_timestamp(start),
            })
            previous_end = end
        return parts

    def update_session_part(self, part_id, duration, new_start, old_start):
        (title_id, session_start, segment_number,
         segment_start) = _decode_segment_part_id(part_id)
        data = copy.deepcopy(self._load())
        title = self._find_title(data, "nx_" + title_id)
        if title is None:
            raise AdapterOperationError(
                f"Could not find title {title_id!r} in "
                f"'{self.display_name}'.")
        found = self._find_group_by_start(title, session_start)
        if found is None:
            raise AdapterOperationError(
                f"Session {session_start!r} no longer exists in "
                f"'{self.display_name}'.")
        group_start, group_end, group = found
        segments = self._segments_for_group(group)
        if (segment_number < 1 or segment_number > len(segments)
                or segments[segment_number - 1][0] != segment_start):
            raise AdapterOperationError(
                f"Play segment {segment_number!r} no longer exists in "
                f"'{self.display_name}'.")
        segment_index = segment_number - 1
        old_start, old_end, old_duration = segments[segment_index]
        new_ts = _parse_dt(new_start)
        if duration is None:
            new_duration = old_duration
        else:
            try:
                new_duration = int(duration)
            except (TypeError, ValueError) as error:
                raise AdapterOperationError(
                    "Play segment duration must be an integer."
                ) from error
        if new_duration <= 0:
            raise AdapterOperationError(
                "Play segment duration must be positive.")
        new_end = (old_end if new_ts == old_start
                   and new_duration == old_duration
                   else new_ts + new_duration)
        segments[segment_index] = (new_ts, new_end, new_duration)
        segments.sort(key=lambda segment: segment[0])
        complete = any(event.get("type") == "Closed" for event in group)
        self._rebuild_group(
            data, title, group_start, group_end, segments, complete)
        self._save(data)

    def add_session_part(self, game_id, start_time, duration):
        data = copy.deepcopy(self._load())
        title = self._find_title(data, game_id)
        if title is None:
            raise AdapterOperationError(
                f"Could not find title {game_id!r} in "
                f"'{self.display_name}'.")
        base = _parse_dt(start_time)
        try:
            part_duration = int(duration)
        except (TypeError, ValueError) as error:
            raise AdapterOperationError(
                "Play segment duration must be an integer."
            ) from error
        if part_duration <= 0:
            raise AdapterOperationError(
                "Play segment duration must be positive.")
        found = self._find_group_containing(title, base)
        if found is None:
            raise AdapterOperationError(
                f"No session contains {start_time!r} in "
                f"'{self.display_name}'.")
        group_start, group_end, group = found
        segments = self._segments_for_group(group)
        segments.append((base, base + part_duration, part_duration))
        segments.sort(key=lambda segment: segment[0])
        complete = any(event.get("type") == "Closed" for event in group)
        self._rebuild_group(
            data, title, group_start, group_end, segments, complete)
        self._save(data)

    def delete_session_part(self, part_id):
        (title_id, session_start, segment_number,
         segment_start) = _decode_segment_part_id(part_id)
        data = copy.deepcopy(self._load())
        title = self._find_title(data, "nx_" + title_id)
        if title is None:
            raise AdapterOperationError(
                f"Could not find title {title_id!r} in "
                f"'{self.display_name}'.")
        found = self._find_group_by_start(title, session_start)
        if found is None:
            raise AdapterOperationError(
                f"Session {session_start!r} no longer exists in "
                f"'{self.display_name}'.")
        group_start, group_end, group = found
        segments = self._segments_for_group(group)
        if (segment_number < 1 or segment_number > len(segments)
                or segments[segment_number - 1][0] != segment_start):
            raise AdapterOperationError(
                f"Play segment {segment_number!r} no longer exists in "
                f"'{self.display_name}'.")
        del segments[segment_number - 1]
        complete = any(event.get("type") == "Closed" for event in group)
        self._rebuild_group(
            data, title, group_start, group_end, segments, complete)
        self._save(data)

    def recalculate_totals(self):
        data = self._load()
        for _user_name, t in self._titles(data):
            self._recompute_title(data, t)
        self._save(data)
        self.report(f"Totals recalculated for '{self.display_name}'")

    def _recompute_title(self, data, title):
        events = title.get("events", [])
        if not events:
            title.setdefault("summary", {}).update(
                {"firstPlayed": 0, "lastPlayed": 0, "playtime": 0, "launches": 0})
            return
        first, last = first_last_from_events(events, 0, 0)
        summary = title.setdefault("summary", {})
        summary["firstPlayed"] = first
        summary["lastPlayed"] = last
        summary["playtime"] = calculate_playtime(events)
        summary["launches"] = count_launches(events)

    def get_round_candidates(self):
        return []

    def apply_rounding(self, sessions):
        raise AdapterOperationError(
            "NXAL session durations are derived from focus events; "
            "there is no fractional duration to round.")

    def get_cleanup_candidates(self, threshold_seconds):
        cands = []
        data = self._load()
        for user_name, t in self._titles(data):
            for group in _title_session_pairs([t]):
                duration = calculate_playtime(group)
                if duration < threshold_seconds:
                    cands.append({
                        "date_time": format_timestamp(group[0]["clockTimestamp"]),
                        "duration": duration,
                        "game_id": "nx_" + str(t["id"]),
                        "name": t.get("name", "Unknown"),
                        "native_user_id": self._user_id_for(user_name),
                        "session_id": _session_id(user_name, t, group),
                    })
        return cands

    def apply_cleanup(self, sessions):
        for ref in sessions:
            self.delete_session(ref.date_time, session_id=ref.session_id)

    def get_sessions_chronological(self):
        return self.fetch_dashboard_data()[1]

    def apply_merges(self, merges):
        """Collapses adjacent sessions into one session, saving once."""
        if not merges:
            return
        data = copy.deepcopy(self._load())
        for merge in merges:
            primary_ref = merge.primary
            primary_dt = primary_ref.date_time
            new_duration = float(merge.duration)
            if primary_ref.session_id is not None:
                found = self._find_session_by_id(
                    data, primary_ref.session_id)
                if found is None:
                    raise AdapterOperationError(
                        f"Session {primary_ref.session_id!r} no longer "
                        f"exists in '{self.display_name}'.")
                game_id = "nx_" + str(found[1].get("id", ""))
            else:
                game_id = self._game_id_for_session(data, primary_dt)
            if game_id is None:
                raise AdapterOperationError(
                    f"No session starts at {primary_dt} in "
                    f"'{self.display_name}'.")
            self._replace_session(
                data, game_id, primary_dt, primary_dt, new_duration,
                session_id=primary_ref.session_id)
            title = self._find_title(data, game_id)
            if title is None:
                raise AdapterOperationError(
                    f"Could not find title {game_id!r} in "
                    f"'{self.display_name}'.")
            events = title.setdefault("events", [])
            for ref in merge.absorbed:
                if ref.session_id is not None:
                    removed = self._remove_session_by_id(
                        data, ref.session_id)
                else:
                    removed = self._remove_session_from_title(
                        events, _parse_dt(ref.date_time))
                if not removed:
                    raise AdapterOperationError(
                        f"No session starts at {ref.date_time!r} in "
                        f"'{self.display_name}'.")
            events[:] = sort_events(events)
            self._recompute_title(data, title)
        self._save(data)

    def _game_id_for_session(self, data, date_time):
        """Returns the 'nx_<hex>' id of the title owning a session start,
        or None when nothing starts there."""
        try:
            target = _parse_dt(date_time)
        except (TypeError, ValueError):
            return None
        for _, title in self._titles(data):
            for group in _title_session_pairs([title]):
                if group[0].get("clockTimestamp") == target:
                    return "nx_" + str(title.get("id", ""))
        return None

    def get_raw_export(self):
        raise AdapterOperationError(
            "NXAL does not support raw export: users and event detail "
            "cannot be reconstructed losslessly from the session view.")

    def import_raw_data(self, games, sessions):
        raise AdapterOperationError(
            "NXAL does not support raw import: sessions must be written as "
            "event groups, not reconstructed rows.")

    def backup_and_disable(self):
        raise AdapterOperationError(
            "NXAL does not support database merge backup/disable.")

    # ---------- helpers for advanced ops (called by plugin_tools) ----------
    def _merge_titles(self, primary_id, duplicate_ids):
        data = copy.deepcopy(self._load())
        primary = self._find_title(data, primary_id)
        if primary is None:
            raise AdapterOperationError(
                f"Could not find title {primary_id!r} in "
                f"'{self.display_name}'.")
        owner = next(
            (user for user in data.get("users", [])
             if any(title is primary for title in user.get("titles", []))),
            None)
        if owner is None:
            raise AdapterOperationError(
                f"Could not find the owner of title {primary_id!r}.")
        duplicate_keys = {
            str(value).removeprefix("nx_").lower()
            for value in duplicate_ids
        }
        owner_titles = owner.setdefault("titles", [])
        for duplicate in list(owner_titles):
            if str(duplicate.get("id", "")).lower() not in duplicate_keys:
                continue
            primary.setdefault("events", []).extend(
                duplicate.get("events", []))
            owner_titles.remove(duplicate)
        primary["events"] = sort_events(primary.get("events", []))
        self._recompute_title(data, primary)
        self._save(data)
