"""Shared NXAL repair primitives used by the CLI and host tools."""

import unittest
from unittest import mock

from plugins.nx_activity_log import fix_import, nxal_repair

START = 1767225600


def _events(start=START):
    return [
        {"type": "Launched", "clockTimestamp": start,
         "steadyTimestamp": 100},
        {"type": "Gained Focus", "clockTimestamp": start + 2,
         "steadyTimestamp": 102},
        {"type": "Account Login", "clockTimestamp": start + 3,
         "steadyTimestamp": 103},
        {"type": "Lost Focus", "clockTimestamp": start + 32,
         "steadyTimestamp": 132},
        {"type": "Account Logout", "clockTimestamp": start + 33,
         "steadyTimestamp": 133},
        {"type": "Closed", "clockTimestamp": start + 35,
         "steadyTimestamp": 135},
    ]


class TestRepairEvents(unittest.TestCase):
    def test_invalid_events_are_reported_and_removed(self):
        events = _events() + [
            {"type": "Unknown", "clockTimestamp": 1, "steadyTimestamp": 1},
            {"type": "Closed", "clockTimestamp": 2},
            {"type": "Closed", "clockTimestamp": "3",
             "steadyTimestamp": 3},
            "not-an-event",
        ]
        invalid = nxal_repair.find_invalid_event_fields(events)
        self.assertEqual(
            invalid,
            [(6, "type"), (7, "steadyTimestamp"),
             (8, "clockTimestamp"), (9, "event")])
        cleaned, removed = nxal_repair.remove_invalid_events(events)
        self.assertEqual(removed, 4)
        self.assertEqual(cleaned, _events())

    def test_strict_duplicates_are_removed_but_reboot_pairs_are_kept(self):
        event = {
            "type": "Lost Focus",
            "clockTimestamp": START,
            "steadyTimestamp": 10,
        }
        reboot_pair = {
            "type": "Lost Focus",
            "clockTimestamp": START,
            "steadyTimestamp": 20,
        }
        cleaned, removed = nxal_repair.remove_duplicate_events(
            [event, event, reboot_pair])
        self.assertEqual(removed, 1)
        self.assertEqual(cleaned, [event, reboot_pair])

    def test_repair_sorts_before_checking_orphan_focus(self):
        events = list(reversed(_events()))
        repaired, stats = nxal_repair.repair_events(events)
        self.assertEqual(stats, {
            "invalid": 0,
            "duplicates": 0,
            "orphans": 0,
            "events_removed": 0,
        })
        self.assertEqual(repaired, _events())

    def test_repair_removes_account_separated_orphan_focus(self):
        events = [
            {"type": "Launched", "clockTimestamp": START,
             "steadyTimestamp": 1},
            {"type": "Lost Focus", "clockTimestamp": START + 1,
             "steadyTimestamp": 2},
            {"type": "Account Login", "clockTimestamp": START + 2,
             "steadyTimestamp": 3},
            {"type": "Closed", "clockTimestamp": START + 3,
             "steadyTimestamp": 4},
        ]
        repaired, stats = nxal_repair.repair_events(events)
        self.assertEqual(stats["orphans"], 1)
        self.assertNotIn("Lost Focus", [event["type"] for event in repaired])
        self.assertEqual(len(repaired), 3)

    def test_summary_for_events_uses_nxal_first_last_semantics(self):
        summary = nxal_repair.summary_for_events(_events())
        self.assertEqual(summary, {
            "firstPlayed": START + 3,
            "lastPlayed": START + 32,
            "launches": 1,
            "playtime": 30,
        })
        self.assertEqual(
            nxal_repair.summary_for_events(
                [], fallback_first=10, fallback_last=20),
            {
                "firstPlayed": 10,
                "lastPlayed": 20,
                "launches": 0,
                "playtime": 0,
            })

    def test_repair_export_updates_all_titles(self):
        data = {
            "users": [{
                "titles": [
                    {
                        "events": list(reversed(_events())),
                        "summary": {
                            "firstPlayed": 1,
                            "lastPlayed": 2,
                            "launches": 9,
                            "playtime": 9,
                        },
                    },
                    {
                        "events": [{
                            "type": "Unknown",
                            "clockTimestamp": 1,
                            "steadyTimestamp": 1,
                        }],
                        "summary": {"launches": 9},
                    },
                ],
            }],
        }
        totals = nxal_repair.repair_export(data)
        self.assertEqual(totals["titles"], 2)
        self.assertEqual(totals["invalid"], 1)
        self.assertEqual(totals["events_removed"], 1)
        first = data["users"][0]["titles"][0]
        self.assertEqual(first["summary"], {
            "firstPlayed": START + 3,
            "lastPlayed": START + 32,
            "launches": 1,
            "playtime": 30,
        })
        second = data["users"][0]["titles"][1]
        self.assertEqual(second["events"], [])
        self.assertEqual(second["summary"], {
            "firstPlayed": 0,
            "lastPlayed": 0,
            "launches": 0,
            "playtime": 0,
        })


class TestInteractiveCliWrappers(unittest.TestCase):
    def test_sort_wrapper_uses_shared_ordering(self):
        with mock.patch("builtins.input", return_value="y"):
            events, fixed = fix_import.fix_out_of_order(
                list(reversed(_events())))
        self.assertTrue(fixed)
        self.assertEqual(events, _events())

    def test_invalid_field_wrapper_removes_unknown_type(self):
        events = _events() + [{
            "type": "Unknown",
            "clockTimestamp": START,
            "steadyTimestamp": 999,
        }]
        with mock.patch("builtins.input", return_value="y"):
            fixed, changed = fix_import.fix_null_fields(events)
        self.assertTrue(changed)
        self.assertEqual(fixed, _events())

    def test_focus_wrapper_removes_orphan(self):
        events = [
            {"type": "Launched", "clockTimestamp": START,
             "steadyTimestamp": 1},
            {"type": "Lost Focus", "clockTimestamp": START + 1,
             "steadyTimestamp": 2},
            {"type": "Account Login", "clockTimestamp": START + 2,
             "steadyTimestamp": 3},
            {"type": "Closed", "clockTimestamp": START + 3,
             "steadyTimestamp": 4},
        ]
        with mock.patch("builtins.input", return_value="y"):
            fixed, changed = fix_import.fix_focus_errors(events, "Game")
        self.assertTrue(changed)
        self.assertEqual(len(fixed), 3)
        self.assertNotIn("Lost Focus", [event["type"] for event in fixed])

    def test_duplicate_wrapper_uses_strict_identity(self):
        event = {
            "type": "Lost Focus",
            "clockTimestamp": START,
            "steadyTimestamp": 10,
        }
        events = [event, dict(event), {
            "type": "Lost Focus",
            "clockTimestamp": START,
            "steadyTimestamp": 20,
        }]
        with mock.patch("builtins.input", return_value="y"):
            fixed, changed = fix_import.fix_duplicate_events(events)
        self.assertTrue(changed)
        self.assertEqual(fixed, [event, events[2]])


if __name__ == "__main__":
    unittest.main()
