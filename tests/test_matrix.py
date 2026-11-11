"""NXAL adapter write matrix and capability contract."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from plugin_sdk import AdapterOperationError, SessionMerge, SessionRef
from plugins.nx_activity_log.adapter import NXALGameTrackerAdapter

START = 1767225600
NXAL_TITLE_ID = '0100DEADBEEF0000'
NXAL_USER_ID = 'A' * 32


def _nxal_fixture(path):
    payload = {
        "exportString": "test",
        "exportTimestamp": START,
        "exportVersion": "1.5.8",
        "users": [{
            "id": NXAL_USER_ID,
            "name": "Alice",
            "titles": [{
                "id": NXAL_TITLE_ID,
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
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)

class TestNxalCapabilities(unittest.TestCase):
    def test_capabilities_are_honest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'import.json')
            _nxal_fixture(path)
            adapter = NXALGameTrackerAdapter('nxal', 'NXAL', path, 1)
            self.assertTrue(adapter.supports_session_write)
            self.assertTrue(adapter.supports_session_ids)
            self.assertTrue(adapter.supports_session_repair)
            self.assertTrue(adapter.supports_game_write)
            self.assertTrue(adapter.supports_game_merge)
            self.assertFalse(adapter.supports_raw_transfer)
            with self.assertRaises(AdapterOperationError):
                adapter.get_raw_export()
            with self.assertRaises(AdapterOperationError):
                adapter.import_raw_data([], [])
            with self.assertRaises(AdapterOperationError):
                adapter.backup_and_disable()


class TestNxalWriteMatrix(unittest.TestCase):
    def test_read_write_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'import.json')
            _nxal_fixture(path)
            adapter = NXALGameTrackerAdapter(
                'nxal', 'NXAL', path, 1,
                config={'profile_mapping': '{"Alice": 1}'})

            _, sessions, _ = adapter.fetch_dashboard_data()
            original = sessions[0]
            self.assertEqual(original['native_user_id'], 1)

            adapter.add_session(
                original['game_id'], '2026-01-02T10:00:00', 120)
            _, sessions, _ = adapter.fetch_dashboard_data()
            self.assertEqual(len(sessions), 2)

            parts = adapter.get_session_parts(
                original['game_id'], original['date_time'])
            self.assertTrue(parts)
            editable = next(part for part in parts if part['editable'])
            self.assertEqual(editable['type'], 'play')
            self.assertEqual(editable['name'], 'Play segment 1')
            moved_start = (
                datetime.strptime(
                    editable['start_time'], '%Y-%m-%d %H:%M:%S')
                + timedelta(seconds=1)
            ).strftime('%Y-%m-%d %H:%M:%S')
            adapter.update_session_part(
                editable['part_id'], None,
                moved_start, editable['start_time'])
            parts_after = adapter.get_session_parts(
                original['game_id'], original['date_time'])
            self.assertTrue(any(
                part['start_time'] == moved_start
                for part in parts_after))
            _, sessions, _ = adapter.fetch_dashboard_data()
            original = next(
                s for s in sessions
                if s['date_time'] == original['date_time'])
            second = next(s for s in sessions
                          if s['date_time'] == '2026-01-02 10:00:00')
            merge = SessionMerge(
                primary=SessionRef(
                    'nxal', original['date_time'], original['session_id']),
                absorbed=(SessionRef(
                    'nxal', second['date_time'], second['session_id']),),
                duration=4000,
            )
            adapter.apply_merges([merge])
            _, sessions, _ = adapter.fetch_dashboard_data()
            self.assertEqual(len(sessions), 1)

            adapter.apply_cleanup([
                SessionRef('nxal', sessions[0]['date_time'],
                           sessions[0]['session_id'])])
            _, sessions, _ = adapter.fetch_dashboard_data()
            # The PDM-backed summary survives deletion of the only event
            # session; the adapter surfaces it as a synthetic summary row,
            # not as a native session.
            self.assertFalse(any(s.get('session_id') for s in sessions))

            with self.assertRaises(AdapterOperationError):
                adapter.get_raw_export()
            with self.assertRaises(AdapterOperationError):
                adapter.import_raw_data([], [])
            with self.assertRaises(AdapterOperationError):
                adapter.backup_and_disable()
