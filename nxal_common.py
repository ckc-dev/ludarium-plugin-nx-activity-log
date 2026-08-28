#!/usr/bin/env python3
"""
nxal_common.py — Shared utilities for NX-ActivityLog verification scripts.
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
