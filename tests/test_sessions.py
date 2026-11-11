"""NXAL session span, midnight and write-safety behaviour."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from plugin_sdk import AdapterOperationError
from plugins.nx_activity_log import nxal_codec, plugin_tools
from plugins.nx_activity_log.adapter import NXALGameTrackerAdapter

START = 1767225600
TITLE_ID = 'B' * 16
USER_ID = 'A' * 32


def _payload(events=None, title_id=TITLE_ID, name='Game'):
    return {
        'exportString': 'test',
        'exportTimestamp': START,
        'exportVersion': '1.6.0',
        'users': [{
            'id': USER_ID,
            'name': 'Alice',
            'titles': [{
                'id': title_id,
                'name': name,
                'summary': {},
                'events': events or [],
            }],
        }],
    }


def _complete_events(start=START, duration=100):
    return [
        {'type': 'Launched', 'clockTimestamp': start,
         'steadyTimestamp': 100},
        {'type': 'Gained Focus', 'clockTimestamp': start + 2,
         'steadyTimestamp': 102},
        {'type': 'Account Login', 'clockTimestamp': start + 3,
         'steadyTimestamp': 103},
        {'type': 'Lost Focus', 'clockTimestamp': start + 2 + duration,
         'steadyTimestamp': 102 + duration},
        {'type': 'Account Logout', 'clockTimestamp': start + 3 + duration,
         'steadyTimestamp': 103 + duration},
        {'type': 'Closed', 'clockTimestamp': start + 5 + duration,
         'steadyTimestamp': 105 + duration},
    ]


class NxalSessionTest(unittest.TestCase):
    def _adapter(self, payload=None):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, 'import.json')
        nxal_codec.write_export_json(path, payload or _payload(
            _complete_events()))
        self.addCleanup(tmp.cleanup)
        return path, NXALGameTrackerAdapter('nxal', 'NXAL', path, 1)

    def test_playtime_uses_steady_time_when_clock_jumps(self):
        events = [
            {'type': 'Gained Focus', 'clockTimestamp': 2,
             'steadyTimestamp': 10},
            {'type': 'Lost Focus', 'clockTimestamp': 40,
             'steadyTimestamp': 70},
        ]
        self.assertEqual(nxal_codec.calculate_playtime(events), 60)
        self.assertEqual(nxal_codec.focus_intervals(events), [(2, 60)])

    def test_cross_midnight_session_exposes_end_and_active_segments(self):
        start = int(datetime(2026, 1, 1, 23, 59, 0).timestamp())
        path, adapter = self._adapter(_payload(
            _complete_events(start=start, duration=120)))
        row = adapter.fetch_dashboard_data()[1][0]
        self.assertEqual(row['duration'], 120)
        self.assertIn('end_time', row)
        self.assertEqual(row['active_segments'][0]['duration'], 120)
        start = datetime.strptime(row['date_time'], '%Y-%m-%d %H:%M:%S')
        end = datetime.strptime(row['end_time'], '%Y-%m-%d %H:%M:%S')
        self.assertGreater(end, start)
        self.assertNotEqual(end, start + timedelta(seconds=120))

    def test_add_session_writes_account_events_and_exact_playtime(self):
        path, adapter = self._adapter()
        adapter.add_session('nx_' + TITLE_ID, '2026-01-02T10:00:00', 120)
        rows = adapter.fetch_dashboard_data()[1]
        added = next(row for row in rows
                     if row['date_time'] == '2026-01-02 10:00:00')
        self.assertEqual(added['duration'], 120)
        events = adapter._load()['users'][0]['titles'][0]['events']
        types = [event['type'] for event in events]
        self.assertIn('Account Login', types)
        self.assertIn('Account Logout', types)

    def test_update_session_leaves_the_file_untouched_on_save_failure(self):
        path, adapter = self._adapter()
        with open(path, 'rb') as handle:
            before = handle.read()
        with mock.patch.object(
                adapter, '_save', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                adapter.update_session(
                    'nx_' + TITLE_ID, 50, '2026-01-02T10:00:00',
                    adapter.fetch_dashboard_data()[1][0]['date_time'])
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), before)

    def test_add_part_rejects_overlap_and_adds_only_in_a_gap(self):
        path, adapter = self._adapter()
        game_id = 'nx_' + TITLE_ID
        start = adapter.fetch_dashboard_data()[1][0]['date_time']
        start_dt = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
        adapter.add_session_part(game_id, start, 1)
        self.assertEqual(
            adapter.fetch_dashboard_data()[1][0]['duration'], 101)
        with self.assertRaises(AdapterOperationError):
            adapter.add_session_part(
                game_id,
                (start_dt + timedelta(seconds=2)).strftime(
                    '%Y-%m-%d %H:%M:%S'),
                2)

    def test_part_updates_move_steady_time_and_protect_required_events(self):
        path, adapter = self._adapter()
        game_id = 'nx_' + TITLE_ID
        start = adapter.fetch_dashboard_data()[1][0]['date_time']
        parts = adapter.get_session_parts(game_id, start)
        gained = next(part for part in parts
                      if part['type'] == 'Gained Focus')
        moved = (datetime.strptime(
            gained['start_time'], '%Y-%m-%d %H:%M:%S')
            + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        adapter.update_session_part(
            gained['part_id'], None, moved, gained['start_time'])
        events = adapter._load()['users'][0]['titles'][0]['events']
        moved_event = next(event for event in events
                           if event['type'] == 'Gained Focus')
        self.assertEqual(moved_event['steadyTimestamp'], 103)

        account = next(part for part in parts
                       if part['type'] == 'Account Login')
        with self.assertRaises(AdapterOperationError):
            adapter.update_session_part(
                account['part_id'], None, moved, account['start_time'])
        with self.assertRaises(AdapterOperationError):
            adapter.delete_session_part(account['part_id'])

    def test_fix_import_preserves_distinct_events_and_recomputes_summary(self):
        path, _adapter = self._adapter()
        result = plugin_tools.fix_import(
            {'path': path}, type('Ctx', (), {'path': path})())
        self.assertTrue(result['ok'])
        data = nxal_codec.load_json(path)
        title = data['users'][0]['titles'][0]
        self.assertEqual(len(title['events']), 6)
        self.assertEqual(title['summary']['playtime'], 100)
        self.assertEqual(title['summary']['launches'], 1)

    def test_game_merge_candidates_are_scoped_to_one_user(self):
        path, adapter = self._adapter()
        data = adapter._load()
        title = data['users'][0]['titles'][0]
        copy = json.loads(json.dumps(title))
        copy['id'] = 'C' * 16
        data['users'][0]['titles'].append(copy)
        adapter._save(data)
        candidates = adapter.get_game_merge_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]['instances']), 2)
        adapter.apply_game_merges(
            candidates[0]['instances'][0]['game_id'],
            [candidates[0]['instances'][1]['game_id']])
        self.assertEqual(len(adapter._load()['users'][0]['titles']), 1)


if __name__ == '__main__':
    unittest.main()
