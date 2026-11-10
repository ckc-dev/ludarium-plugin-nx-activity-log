"""NX-ActivityLog live adapter — read/write over a single NXAL import.json.

The GameTime host treats an environment of type `nx_activity_log` as a LIVE
source backed by one `import.json` export (the adapters' env path). All reads
are derived from the JSON on each call; all mutations write back through
`nxal_codec.write_export_json` so the file stays byte-compatible with a
pristine NXAL export (sorted keys, UTF-8 literal, trailing newline).

Session model (mirrors NXAL / PlayData.cpp):
  - A "session" is a Launched…Closed group inside a title's events array.
  - start       = the Launched event's clockTimestamp
  - duration    = focus-pair arithmetic over steadyTimestamp between the
                  group's Gained Focus / Lost Focus events (calculate_playtime)
  - game        = the title; game_id = 'nx_' + title.id (hex), name = title.name
  - firstPlayed/lastPlayed/lastPlayed derived via nxal_codec (first Login /
    last Lost Focus semantics, falling back to event extremes).

The 18-method surface below is the GameTrackerAdapter contract. The heavy
advanced operations (merge/dedup/verify/fix) live in plugin_tools.py /
plugin_health.py and reuse the in-repo CLI logic (nxal_codec + merge_exports /
deduplicate_exports / fix_import / validate_import).
"""

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
    format_timestamp,
    load_json,
    sort_events,
    write_export_json,
)
from plugin_sdk import AdapterOperationError

MIN_SESSION_DURATION = 300  # 5 minutes; NXAL ignores shorter focus pairs


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
        """Map an NXAL profile name to a GameTime user id.

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
        for user_name, t in self._titles(data):
            game_id = "nx_" + str(t["id"])
            cleared = False
            for group in _title_session_pairs([t]):
                duration = calculate_playtime(group)
                start_ts = group[0]["clockTimestamp"]
                cleared = True
                sessions.append({
                    "game_id": game_id,
                    "name": t.get("name", "Unknown"),
                    "native_user_id": self._user_id_for(user_name),
                    "date_time": format_timestamp(start_ts),
                    "duration": duration,
                })
                total += duration
            if not cleared:
                stored = t.get("summary", {}).get("playtime", 0)
                first_ts = t.get("summary", {}).get("firstPlayed", 0) or 0
                if stored > 0:
                    sessions.append({
                        "game_id": game_id,
                        "name": t.get("name", "Unknown"),
                        "native_user_id": self.default_user_id,
                        "date_time": format_timestamp(first_ts),
                        "duration": stored,
                    })
                    total += stored
        # sort chronologically
        # %s
        # Titles whose event groups never clear MIN_SESSION_DURATION still own
        # stored playtime (export summary) - surface them as a single summary
        # session attributed to default_user_id so they are not silently hidden.
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
    # cannot derive it because two Launched events inside one
    # MIN_SESSION_DURATION span reconstruct into a SINGLE session. Steam has
    # no launch column at all, so this stays an opt-in capability flag.
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
        data = self._load()
        if self._find_title(data, game_id) is None:
            # give the title an ID from the provided game_id (strip nx_)
            hex_id = str(game_id).removeprefix("nx_")
            title = {"id": hex_id, "name": name, "events": []}
            if data.get("users"):
                data["users"][0].setdefault("titles", []).append(title)
            else:
                data["users"] = [{"name": "Unknown", "titles": [title]}]
            self._save(data)
            self.report(f"Added game '{name}' to '{self.display_name}'")

    def delete_game(self, game_id):
        data = self._load()
        hex_id = str(game_id).removeprefix("nx_").lower()
        for u in data.get("users", []):
            u["titles"] = [t for t in u.get("titles", [])
                           if str(t.get("id", "")).lower() != hex_id]
        data["users"] = [u for u in data.get("users", [])
                         if u.get("titles")]
        self._save(data)

    def get_game_merge_candidates(self):
        return []

    def apply_game_merges(self, primary_id, duplicate_ids):
        self._merge_titles(primary_id, duplicate_ids)

    def add_session(self, game_id, start_time, duration):
        data = self._load()
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
        duration = int(duration)
        # One anchor for the whole synthesized block: steady stays monotonic
        # inside it, matching what a real Launched…Closed group looks like.
        anchor = _anchor_for(_event_anchors(events) or _all_anchors(data), start_ts)
        events.extend([
            {"type": "Launched", "clockTimestamp": start_ts,
             "steadyTimestamp": _steady_from(anchor, start_ts)},
            {"type": "Gained Focus", "clockTimestamp": start_ts + 2,
             "steadyTimestamp": _steady_from(anchor, start_ts + 2)},
            {"type": "Lost Focus", "clockTimestamp": start_ts + 2 + duration,
             "steadyTimestamp": _steady_from(anchor, start_ts + 2 + duration)},
            {"type": "Closed", "clockTimestamp": start_ts + 6 + duration,
             "steadyTimestamp": _steady_from(anchor, start_ts + 6 + duration)},
        ])
        # canonical (clock, steady) order — see nxal_codec.sort_events
        events[:] = sort_events(events)
        self._recompute_title(data, title)
        self._save(data)

    def update_session(self, game_id, duration, new_start, old_start):
        self.delete_session(old_start)
        self.add_session(game_id, new_start, duration)

    def delete_session(self, original_date_time):
        data = self._load()
        target = _parse_dt(original_date_time)
        found = False
        for user_name, t in self._titles(data):
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

    def get_session_parts(self, game_id, original_date_time):
        data = self._load()
        try:
            target = _parse_dt(original_date_time)
        except (TypeError, ValueError):
            return []
        parts = []
        hex_id = str(game_id)
        if hex_id.startswith("nx_"):
            hex_id = hex_id[len("nx_"):]
        for user_name, t in self._titles(data):
            if str(t.get("id", "")).lower() != hex_id.lower():
                continue
            events = t.setdefault("events", [])
            # find the group starting at target
            group_start = None
            for i, ev in enumerate(events):
                if ev.get("type") == "Launched" and ev["clockTimestamp"] == target:
                    group_start = i
                    break
            if group_start is None:
                continue
            end_i = group_start + 1
            while end_i < len(events) and events[end_i].get("type") != "Closed":
                end_i += 1
            if end_i >= len(events):
                continue
            span_events = events[group_start:end_i + 1]

            # emit one part per real event.
            # Real exports store clockTimestamp as epoch ints; keep prev in int
            # space so format_timestamp gets ints and gaps are plain seconds.
            # Each event's duration = seconds until the NEXT event of ANY type
            # in this span (the plan's "event->next event span"), so the part
            # durations tile the Launched..Closed span exactly; the last event
            # (Closed) has no successor -> None. Breaks have no rows of their
            # own: a Lost Focus part's duration IS the pause until the next
            # Gained Focus.
            span_timestamps = [ev.get("clockTimestamp") for ev in span_events]
            prev_ts = None
            for i, ev in enumerate(span_events):
                ts = ev.get("clockTimestamp")
                gap_before = 0
                if prev_ts is not None and ts is not None:
                    gap_before = ts - prev_ts
                next_ts = (span_timestamps[i + 1]
                           if i + 1 < len(span_timestamps) else None)
                duration = (next_ts - ts) if (next_ts is not None and ts is not None) else None
                parts.append({
                    "part_id": format_timestamp(ts) if ts is not None else None,
                    "name": ev.get("type"),
                    "type": ev.get("type"),
                    "start_time": format_timestamp(ts) if ts is not None else None,
                    "duration": duration,
                    "clockTimestamp": ts,
                    # Launched/Closed are the group boundaries: they anchor the
                    # span and must never drift, so they render read-only.
                    "editable": ev.get("type") not in ("Launched", "Closed"),
                    "break_before_seconds": gap_before,
                })
                if ts is not None:
                    prev_ts = ts
            # Only the FIRST title copy matching id takes part in the UI's
            # session chain. Duplicate copies (same id, same events, owned by
            # multiple users) must not be concatenated; edits on the parts pane
            # hit this first copy and make the copies diverge thereafter.
            return parts
        return parts

    def update_session_part(self, part_id, duration, new_start, old_start):
        target_ts = _parse_dt(part_id)
        new_ts = _parse_dt(new_start)
        data = self._load()
        for user_name, t in self._titles(data):
            events = t.setdefault("events", [])
            for ev in events:
                if ev.get("clockTimestamp") == target_ts:
                    # re-point this one event; guard: Launched/Closed are the
                    # group boundaries — both stay put (never drift the group).
                    if ev.get("type") in ("Launched", "Closed"):
                        raise AdapterOperationError(
                            "Launched and Closed parts anchor the session and "
                            "cannot be moved.")
                    ev["clockTimestamp"] = new_ts
                    events[:] = sort_events(events)
                    self._recompute_title(data, t)
                    self._save(data)
                    return
        raise AdapterOperationError(
            f"No session part starts at {part_id!r} in '{self.display_name}'.")

    def add_session_part(self, game_id, start_time, duration):
        data = self._load()
        hex_id = str(game_id)
        if hex_id.startswith("nx_"):
            hex_id = hex_id[len("nx_"):]
        for user_name, t in self._titles(data):
            if str(t.get("id", "")).lower() != hex_id.lower():
                continue
            events = t.setdefault("events", [])
            base = _parse_dt(start_time)
            # insert a synthetic Gained/Lost Focus pair at start_time -> this
            # SPLITS the current focus pair in two, inserting a pause between.
            gained = base
            lost = base + int(duration)
            anchor = _anchor_for(_event_anchors(events) or _all_anchors(data), gained)
            events.append({"type": "Gained Focus", "clockTimestamp": gained,
                           "steadyTimestamp": _steady_from(anchor, gained)})
            events.append({"type": "Lost Focus", "clockTimestamp": lost,
                           "steadyTimestamp": _steady_from(anchor, lost)})
            events[:] = sort_events(events)
            self._recompute_title(data, t)
            self._save(data)
            return
        raise AdapterOperationError(
            f"Could not find title {game_id!r} in '{self.display_name}'.")

    def delete_session_part(self, part_id):
        ts = _parse_dt(part_id)
        data = self._load()
        for user_name, t in self._titles(data):
            events = t.setdefault("events", [])
            for i, ev in enumerate(events):
                if ev.get("clockTimestamp") == ts:
                    # guard: Launched/Closed are the group boundaries — both stay
                    if ev.get("type") in ("Launched", "Closed"):
                        raise AdapterOperationError(
                            "Launched and Closed parts anchor the session and "
                            "cannot be deleted.")
                    del events[i]
                    self._recompute_title(data, t)
                    self._save(data)
                    return
        raise AdapterOperationError(
            f"No session part starts at {part_id!r} in '{self.display_name}'.")

    def recalculate_totals(self):
        data = self._load()
        for user_name, t in self._titles(data):
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
                    })
        return cands

    def apply_cleanup(self, sessions):
        for ref in sessions:
            self.delete_session(ref.date_time)

    def get_sessions_chronological(self):
        return self.fetch_dashboard_data()[1]

    def apply_merges(self, merges):
        """Collapses adjacent sessions into one spanning session.

        The host passes typed ``SessionMerge`` requests. The primary's game
        must be resolved before anything is mutated — add_session(game_id,
        ...) with game_id None used to fabricate a title literally named
        'None' in the export.
        """
        for merge in merges:
            primary_dt = merge.primary.date_time
            new_duration = float(merge.duration)
            data = self._load()
            game_id = self._game_id_for_session(data, primary_dt)
            if game_id is None:
                raise AdapterOperationError(
                    f"No session starts at {primary_dt} in "
                    f"'{self.display_name}'.")

            # Set the surviving span first, then drop the sessions it absorbed.
            self.update_session(
                game_id, new_duration, primary_dt, primary_dt)
            for ref in merge.absorbed:
                self.delete_session(ref.date_time)

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
        data = self._load()
        primary = self._find_title(data, primary_id)
        if primary is None:
            raise AdapterOperationError(
                f"Could not find title {primary_id!r} in "
                f"'{self.display_name}'.")
        for d in duplicate_ids:
            dup = self._find_title(data, d)
            if dup is None:
                continue
            primary.setdefault("events", []).extend(dup.get("events", []))
            primary["events"].sort(key=lambda e: e["clockTimestamp"])
            for u in data.get("users", []):
                u["titles"] = [t for t in u.get("titles", [])
                               if str(t.get("id", "")).lower() != str(
                                   str(d).removeprefix("nx_")).lower()]
        self._recompute_title(data, primary)
        self._save(data)
