#!/usr/bin/env python3
"""
nxal_common.py — Shared utilities for NX-ActivityLog verification scripts.
"""

import json

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
