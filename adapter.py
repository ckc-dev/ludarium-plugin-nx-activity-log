"""NX-ActivityLog live adapter — read/write over a single NXAL import.json.

The GameTime host treats an environment of type `nx_activity_log` as a LIVE
source backed by one \`import.json\` export (the adapters' env path). All reads
are derived from the JSON on each call; all mutations write back through
\nxal_common.write_export_json\` so the file stays byte-compatible with a
pristine NXAL export (sorted keys, UTF-8 literal, trailing newline).

Session model (mirrors NXAL / PlayData.cpp):
  - A "session" is a Launched…Closed group inside a title's events array.
  - start       = the Launched event's clockTimestamp
  - duration    = focus-pair arithmetic over steadyTimestamp between the
                  group's Gained Focus / Lost Focus events (calculate_playtime)
  - game        = the title; game_id = 'nx_' + title.id (hex), name = title.name
  - firstPlayed/lastPlayed/lastPlayed derived via nxal_common (first Login /
    last Lost Focus semantics, falling back to event extremes).

The 18-method surface below is the GameTrackerAdapter contract. The heavy
advanced operations (merge/dedup/verify/fix) live in plugin_tools.py /
plugin_health.py and reuse the in-repo CLI logic (nxal_common + merge_exports /
deduplicate_exports / fix_import / validate_import).
"""

import os
import sys
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nxal_common import (
    calculate_playtime,
    count_launches,
    first_last_from_events,
    format_timestamp,
    load_json,
    write_export_json,
)

MIN_SESSION_DURATION = 300  # 5 minutes; NXAL ignores shorter focus pairs


def _title_session_pairs(titles):
    """Yield (user_name, title, event_group) for every Launched…Closed group.

    A group opens at a "Launched" event and closes at the next "Closed"
    event. If a title ends without a "Closed" (crash), the group is still
    emitted using the remaining events (duration may be partial).
    """
    for t in titles:
        group = []
        in_group = False
        for ev in t.get("events", []):
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


class NXALGameTrackerAdapter:
    """GameTrackerAdapter contract implemented over live NXAL import.json."""

    def __init__(self, source_id, display_name, path, default_user_id=1,
                 config=None):
        self.source_id = source_id
        self.display_name = display_name
        self.path = path
        self.default_user_id = default_user_id
        self.type = "nx_activity_log"
        self._messages = []
        self.config = config or {}

    # ---------- host integration ----------
    def report(self, message):
        if not hasattr(self, "_messages"):
            self._messages = []
        self._messages.append(message)

    def drain_messages(self):
        msgs = getattr(self, "_messages", [])
        self._messages = []
        return msgs

    # ---------- storage ----------
    def _load(self):
        return load_json(self.path)

    def _save(self, data):
        write_export_json(self.path, data)

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
        stored_by_game = {g["game_id"]: g.get("stored_duration", 0) for g in games_list}
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
                    "user": user_name,
                    "date_time": format_timestamp(start_ts),
                    "start_dt": datetime.fromtimestamp(start_ts),
                    "end_dt": datetime.fromtimestamp(start_ts + duration),
                    "duration": duration,
                })
                total += duration
            if not cleared:
                stored = g.get("stored_duration", 0)
                first_ts = t.get("summary", {}).get("firstPlayed", 0) or 0
                if stored > 0:
                    sessions.append({
                        "game_id": game_id,
                        "name": t.get("name", "Unknown"),
                        "user": self.default_user_id,
                        "date_time": format_timestamp(first_ts),
                        "start_dt": datetime.fromtimestamp(first_ts),
                        "end_dt": datetime.fromtimestamp(first_ts + stored),
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
                "user": self.default_user_id,
                "date_time": format_timestamp(first_ts),
                "start_dt": datetime.fromtimestamp(first_ts),
                "end_dt": datetime.fromtimestamp(first_ts + stored),
                "duration": stored,
            })
            total += stored
        sessions.sort(key=lambda s: s["date_time"])
        return games_list, sessions, total

    def get_all_games(self):
        return [{"game_id": g["game_id"], "name": g["name"]}
                for g in self._game_entries(self._load())]

    def add_game(self, game_id, name):
        data = self._load()
        if self._find_title(data, game_id) is None:
            # give the title an ID from the provided game_id (strip nx_)
            hex_id = str(game_id).lstrip("nx_")
            title = {"id": hex_id, "name": name, "events": []}
            if data.get("users"):
                data["users"][0].setdefault("titles", []).append(title)
            else:
                data["users"] = [{"name": "Unknown", "titles": [title]}]
            self._save(data)
            self.report(f"Added game '{name}' to '{self.display_name}'")

    def delete_game(self, game_id):
        data = self._load()
        hex_id = str(game_id).lstrip("nx_").lower()
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
            return
        start_ts = _parse_dt(start_time)
        events = title.setdefault("events", [])
        events.extend([
            {"type": "Launched", "clockTimestamp": start_ts},
            {"type": "Gained Focus", "clockTimestamp": start_ts + 2},
            {"type": "Lost Focus", "clockTimestamp": start_ts + 2 + int(duration)},
            {"type": "Closed", "clockTimestamp": start_ts + 6 + int(duration)},
        ])
        # keep events sorted by clockTimestamp
        events.sort(key=lambda e: e["clockTimestamp"])
        self._recompute_title(data, title)
        self._save(data)

    def update_session(self, game_id, duration, new_start, old_start):
        self.delete_session(old_start)
        self.add_session(game_id, new_start, duration)

    def delete_session(self, original_date_time):
        data = self._load()
        target = _parse_dt(original_date_time)
        for user_name, t in self._titles(data):
            events = t.get("events", [])
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
            if end_i < len(events):
                end_i += 1  # include the Closed event
            del events[group_start:end_i]
            self._recompute_title(data, t)
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
        target = _parse_dt(original_date_time)
        parts = []
        hex_id = str(game_id)
        if hex_id.startswith("nx_"):
            hex_id = hex_id[len("nx_"):]
        for user_name, t in self._titles(data):
            if str(t.get("id", "")).lower() != hex_id.lower():
                continue
            events = t.get("events", [])
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
            events = t.get("events", [])
            for ev in events:
                if ev.get("clockTimestamp") == target_ts:
                    # re-point this one event; guard: Launched/Closed are the
                    # group boundaries — both stay put (never drift the group).
                    if ev.get("type") in ("Launched", "Closed"):
                        continue
                    ev["clockTimestamp"] = new_ts
                    events.sort(key=lambda e: e["clockTimestamp"])
                    self._recompute_title(data, t)
                    self._save(data)
                    return
        self._save(data)

    def add_session_part(self, game_id, start_time, duration):
        data = self._load()
        hex_id = str(game_id)
        if hex_id.startswith("nx_"):
            hex_id = hex_id[len("nx_"):]
        for user_name, t in self._titles(data):
            if str(t.get("id", "")).lower() != hex_id.lower():
                continue
            events = t.get("events", [])
            base = _parse_dt(start_time)
            # insert a synthetic Gained/Lost Focus pair at start_time -> this
            # SPLITS the current focus pair in two, inserting a pause between.
            events.append({"type": "Gained Focus", "clockTimestamp": base})
            events.append({"type": "Lost Focus", "clockTimestamp": base + int(duration)})
            events.sort(key=lambda e: e["clockTimestamp"])
            self._recompute_title(data, t)
            self._save(data)
            return
        self._save(data)

    def delete_session_part(self, part_id):
        ts = _parse_dt(part_id)
        data = self._load()
        for user_name, t in self._titles(data):
            events = t.get("events", [])
            for i, ev in enumerate(events):
                if ev.get("clockTimestamp") == ts:
                    # guard: Launched/Closed are the group boundaries — both stay
                    if ev.get("type") in ("Launched", "Closed"):
                        continue
                    del events[i]
                    self._recompute_title(data, t)
                    self._save(data)
                    return
        self._save(data)

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

    def apply_rounding(self, dates_list):
        for d in dates_list:
            try:
                target, new_dur = d["date_time"], d["duration"]
            except (KeyError, TypeError):
                continue
            self.update_session(d.get("game_id", None),
                                round(float(new_dur)),
                                target, target)

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
                    })
        return cands

    def apply_cleanup(self, dates_list):
        for original_date_time in dates_list:
            self.delete_session(original_date_time)

    def get_sessions_chronological(self):
        return self.fetch_dashboard_data()[1]

    def apply_merges(self, merges_list):
        for primary_dt, new_dur, doomed in merges_list:
            self.update_session(None, new_dur, primary_dt, primary_dt)
            for d in doomed:
                self.delete_session(d)

    def get_raw_export(self):
        return self._load()

    def import_raw_data(self, games, sessions):
        data = self._load()
        for g in games:
            self.add_game(g["game_id"], g["name"])
        for s in sessions:
            self.add_session(s["game_id"], s["date_time"], round(s["duration"]))
        self._save(self._load())

    def backup_and_disable(self):
        import time
        data = self._load()
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = f"{self.path}.backup_{stamp}"
        shutil.copy2(self.path, backup)
        # disable by replacing with minimal empty export
        write_export_json(self.path, {
            "users": [], "version": 1,
            "generatedAt": data.get("generatedAt", 0),
        })
        self.report(f"Backed up to {backup} and disabled '{self.display_name}'")

    # ---------- helpers for advanced ops (called by plugin_tools) ----------
    def _merge_titles(self, primary_id, duplicate_ids):
        data = self._load()
        primary = self._find_title(data, primary_id)
        if primary is None:
            return
        primary_hex = str(primary_id).lstrip("nx_").lower()
        for d in duplicate_ids:
            dup = self._find_title(data, d)
            if dup is None:
                continue
            primary.setdefault("events", []).extend(dup.get("events", []))
            primary["events"].sort(key=lambda e: e["clockTimestamp"])
            for u in data.get("users", []):
                u["titles"] = [t for t in u.get("titles", [])
                               if str(t.get("id", "")).lower() != str(
                                   str(d).lstrip("nx_")).lower()]
        self._recompute_title(data, primary)
        self._save(data)


def _parse_dt(value):
    """Parse 'YYYY-MM-DD HH:MM:SS' or ISO 'T' into epoch seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
