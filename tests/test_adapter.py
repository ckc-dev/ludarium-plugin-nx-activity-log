"""NXAL profile mapping and per-environment config behaviour."""

import json
import os
import tempfile
import unittest

from plugins.nx_activity_log.adapter import NXALGameTrackerAdapter

os.environ.setdefault(
    "LUDARIUM_TEST_PLUGIN_DIRS",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import support  # noqa: E402,F401  (isolates metadata + plugins)
from trackers.aggregator import TrackerAggregator

START = 1767225600
TITLE_ID = '0100DEADBEEF0000'
USER_ID = 'A' * 32


def _export():
    return {
        "exportString": "test",
        "exportTimestamp": START,
        "exportVersion": "1.5.8",
        "users": [{
            "id": USER_ID,
            "name": "Alice",
            "titles": [{
                "id": TITLE_ID,
                "name": "Mapped Game",
                "summary": {
                    "firstPlayed": START,
                    "lastPlayed": START + 3608,
                    "launches": 1,
                    "playtime": 3600,
                },
                "events": [
                    {"type": "Launched", "clockTimestamp": START,
                     "steadyTimestamp": 100},
                    {"type": "Gained Focus", "clockTimestamp": START + 2,
                     "steadyTimestamp": 102},
                    {"type": "Lost Focus", "clockTimestamp": START + 3602,
                     "steadyTimestamp": 3602},
                    {"type": "Closed", "clockTimestamp": START + 3608,
                     "steadyTimestamp": 3608},
                ],
            }],
        }],
    }


class TestNxalUserMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'import.json')
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(_export(), handle)

    def tearDown(self):
        self.tmp.cleanup()

    def _adapter(self, config=None):
        return NXALGameTrackerAdapter(
            'source-nxal', 'NXAL', self.path,
            default_user_id=7, config=config)

    def test_profile_mapping_sets_native_user_id(self):
        adapter = self._adapter({'profile_mapping': '{"Alice": 3}'})
        _, sessions, _ = adapter.fetch_dashboard_data()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['native_user_id'], 3)

    def test_unmapped_profile_uses_environment_default(self):
        adapter = self._adapter({'profile_mapping': '{"Someone Else": 3}'})
        _, sessions, _ = adapter.fetch_dashboard_data()
        self.assertEqual(sessions[0]['native_user_id'], 7)

    def test_legacy_default_user_config_overrides_environment_default(self):
        adapter = self._adapter({'default_user_id': 4})
        _, sessions, _ = adapter.fetch_dashboard_data()
        self.assertEqual(adapter.default_user_id, 4)
        self.assertEqual(sessions[0]['native_user_id'], 4)

    def test_invalid_mapping_is_reported_and_ignored(self):
        adapter = self._adapter({'profile_mapping': '{not json'})
        _, sessions, _ = adapter.fetch_dashboard_data()
        self.assertEqual(sessions[0]['native_user_id'], 7)
        self.assertTrue(any('profile_mapping' in message
                            for message in adapter.drain_messages()))

    def test_cleanup_candidates_carry_the_mapped_user(self):
        adapter = self._adapter({'profile_mapping': '{"alice": 3}'})
        candidates = adapter.get_cleanup_candidates(999999)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]['native_user_id'], 3)

    def test_core_honors_native_user_id(self):
        adapter = self._adapter({'profile_mapping': '{"Alice": 1}'})
        aggregator = TrackerAggregator({'source-nxal': adapter})
        _, sessions, _, _ = aggregator.fetch_dashboard_data(
            {'show_zero': True})
        self.assertTrue(sessions)
        self.assertEqual(sessions[0]['user']['id'], 1)


if __name__ == '__main__':
    unittest.main()
