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

    def test_parts_are_logical_play_segments_not_raw_events(self):
        path, adapter = self._adapter()
        game_id = 'nx_' + TITLE_ID
        start = adapter.fetch_dashboard_data()[1][0]['date_time']
        parts = adapter.get_session_parts(game_id, start)
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part['type'], 'play')
        self.assertEqual(part['name'], 'Play segment 1')
        self.assertEqual(part['duration'], 100)
        self.assertTrue(part['editable'])
        self.assertEqual(
            part['start_time'],
            (datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
             + timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(
            part['end_time'],
            (datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
             + timedelta(seconds=102)).strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(
            [item['type'] for item in parts], ['play'])

    def test_zero_duration_segments_have_distinct_part_ids(self):
        start = int(datetime(2026, 1, 8, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 104},
            {'type': 'Lost Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 104},
            {'type': 'Account Logout', 'clockTimestamp': start + 3,
             'steadyTimestamp': 105},
            {'type': 'Closed', 'clockTimestamp': start + 5,
             'steadyTimestamp': 107},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        parts = adapter.get_session_parts('nx_' + TITLE_ID,
                                          row['date_time'])
        self.assertEqual(len(parts), 2)
        self.assertEqual([part['duration'] for part in parts], [0, 0])
        self.assertEqual(len({part['part_id'] for part in parts}), 2)
        adapter.delete_session_part(parts[1]['part_id'])
        remaining = adapter.get_session_parts('nx_' + TITLE_ID,
                                              row['date_time'])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['duration'], 0)

    def test_add_edit_delete_play_segments_rebuilds_lifecycle(self):
        path, adapter = self._adapter(_payload([]))
        game_id = 'nx_' + TITLE_ID
        base = datetime(2026, 1, 2, 10, 0, 0)
        base_text = base.strftime('%Y-%m-%d %H:%M:%S')
        adapter.add_session(game_id, base_text, 100)
        events = adapter._load()['users'][0]['titles'][0]['events']
        self.assertEqual(
            [event['type'] for event in events],
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Account Logout', 'Closed'])
        base_ts = int(base.timestamp())
        self.assertEqual(
            [event['clockTimestamp'] for event in events],
            [base_ts, base_ts + 2, base_ts + 3, base_ts + 102,
             base_ts + 103, base_ts + 105])
        self.assertEqual(
            adapter.fetch_dashboard_data()[1][0]['duration'], 100)

        parts = adapter.get_session_parts(game_id, base_text)
        self.assertEqual(len(parts), 1)
        adapter.update_session_part(
            parts[0]['part_id'], 80,
            parts[0]['start_time'], parts[0]['start_time'])
        edited = adapter.get_session_parts(game_id, base_text)
        self.assertEqual(edited[0]['duration'], 80)
        self.assertEqual(
            [event['type'] for event in
             adapter._load()['users'][0]['titles'][0]['events']],
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Account Logout', 'Closed'])

        adapter.add_session_part(
            game_id,
            (base + timedelta(seconds=83)).strftime('%Y-%m-%d %H:%M:%S'),
            10)
        parts = adapter.get_session_parts(game_id, base_text)
        self.assertEqual([part['duration'] for part in parts], [80, 10])
        self.assertEqual(parts[1]['break_before_seconds'], 1)
        self.assertEqual(
            [event['type'] for event in
             adapter._load()['users'][0]['titles'][0]['events']],
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Gained Focus', 'Lost Focus', 'Account Logout', 'Closed'])
        self.assertEqual(
            adapter.fetch_dashboard_data()[1][0]['duration'], 90)

        adapter.delete_session_part(parts[1]['part_id'])
        remaining = adapter.get_session_parts(game_id, base_text)
        self.assertEqual([part['duration'] for part in remaining], [80])
        self.assertEqual(
            [event['type'] for event in
             adapter._load()['users'][0]['titles'][0]['events']],
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Account Logout', 'Closed'])

    def test_parts_are_scoped_to_the_exact_native_session(self):
        same_start = int(datetime(2026, 1, 9, 10, 0, 0).timestamp())
        events = _complete_events(start=same_start, duration=100)
        data = _payload()
        data['users'] = [
            {
                'id': 'A' * 32,
                'name': 'Alice',
                'titles': [{
                    'id': TITLE_ID,
                    'name': 'Game',
                    'summary': {},
                    'events': json.loads(json.dumps(events)),
                }],
            },
            {
                'id': 'B' * 32,
                'name': 'Bob',
                'titles': [{
                    'id': TITLE_ID,
                    'name': 'Game',
                    'summary': {},
                    'events': json.loads(json.dumps(events)),
                }],
            },
        ]
        path, adapter = self._adapter(data)
        rows = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['session_id'] for row in rows}), 2)
        target = rows[1]
        parts = adapter.get_session_parts(
            target['game_id'], target['date_time'],
            session_id=target['session_id'])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['duration'], 100)
        # Date-only lookup must not guess between two native profiles.
        self.assertEqual(adapter.get_session_parts(
            target['game_id'], target['date_time']), [])
        adapter.update_session_part(
            parts[0]['part_id'], 80, parts[0]['start_time'],
            parts[0]['start_time'])
        with self.assertRaises(AdapterOperationError):
            adapter.add_session_part(
                target['game_id'],
                (datetime.fromtimestamp(same_start)
                 + timedelta(seconds=83)).strftime('%Y-%m-%d %H:%M:%S'),
                1)
        adapter.add_session_part(
            target['game_id'],
            (datetime.fromtimestamp(same_start)
             + timedelta(seconds=83)).strftime('%Y-%m-%d %H:%M:%S'),
            1, session_id=target['session_id'])
        refreshed = adapter.fetch_dashboard_data()[1]
        by_id = {row['session_id']: row for row in refreshed}
        self.assertEqual(by_id[target['session_id']]['duration'], 81)
        untargeted = next(
            row for row in refreshed if row['session_id'] != target['session_id'])
        self.assertEqual(untargeted['duration'], 100)

    def test_session_id_survives_profile_rename(self):
        path, adapter = self._adapter()
        before = adapter.fetch_dashboard_data()[1][0]
        data = adapter._load()
        data['users'][0]['name'] = 'Alice Renamed'
        adapter._save(data)
        after = adapter.fetch_dashboard_data()[1][0]
        self.assertEqual(after['session_id'], before['session_id'])

    def test_incomplete_segment_edit_cannot_overlap_the_next_session(self):
        start = int(datetime(2026, 1, 10, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Lost Focus', 'clockTimestamp': start + 12,
             'steadyTimestamp': 112},
            {'type': 'Launched', 'clockTimestamp': start + 30,
             'steadyTimestamp': 130},
            {'type': 'Gained Focus', 'clockTimestamp': start + 32,
             'steadyTimestamp': 132},
            {'type': 'Account Login', 'clockTimestamp': start + 33,
             'steadyTimestamp': 133},
            {'type': 'Lost Focus', 'clockTimestamp': start + 82,
             'steadyTimestamp': 182},
            {'type': 'Account Logout', 'clockTimestamp': start + 83,
             'steadyTimestamp': 183},
            {'type': 'Closed', 'clockTimestamp': start + 85,
             'steadyTimestamp': 185},
        ]
        path, adapter = self._adapter(_payload(events))
        row = next(r for r in adapter.fetch_dashboard_data()[1]
                   if r['date_time'] == datetime.fromtimestamp(
                       start).strftime('%Y-%m-%d %H:%M:%S'))
        parts = adapter.get_session_parts(
            'nx_' + TITLE_ID, row['date_time'],
            session_id=row['session_id'])
        with open(path, 'rb') as handle:
            before = handle.read()
        with self.assertRaises(AdapterOperationError):
            adapter.update_session_part(
                parts[0]['part_id'], 100, parts[0]['start_time'],
                parts[0]['start_time'])
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), before)

    def test_legacy_date_delete_stops_before_the_next_session(self):
        start = int(datetime(2026, 1, 11, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Lost Focus', 'clockTimestamp': start + 12,
             'steadyTimestamp': 112},
            {'type': 'Launched', 'clockTimestamp': start + 30,
             'steadyTimestamp': 130},
            {'type': 'Gained Focus', 'clockTimestamp': start + 32,
             'steadyTimestamp': 132},
            {'type': 'Account Login', 'clockTimestamp': start + 33,
             'steadyTimestamp': 133},
            {'type': 'Lost Focus', 'clockTimestamp': start + 82,
             'steadyTimestamp': 182},
            {'type': 'Account Logout', 'clockTimestamp': start + 83,
             'steadyTimestamp': 183},
            {'type': 'Closed', 'clockTimestamp': start + 85,
             'steadyTimestamp': 185},
        ]
        path, adapter = self._adapter(_payload(events))
        adapter.delete_session(datetime.fromtimestamp(
            start).strftime('%Y-%m-%d %H:%M:%S'))
        rows = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['duration'], 50)
        title = adapter._load()['users'][0]['titles'][0]
        self.assertEqual(
            [event['type'] for event in title['events']],
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Account Logout', 'Closed'])

    def test_account_events_are_preserved_when_editing_segments(self):
        start = int(datetime(2026, 1, 12, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Lost Focus', 'clockTimestamp': start + 22,
             'steadyTimestamp': 122},
            {'type': 'Gained Focus', 'clockTimestamp': start + 30,
             'steadyTimestamp': 150},
            {'type': 'Account Login', 'clockTimestamp': start + 31,
             'steadyTimestamp': 151},
            {'type': 'Lost Focus', 'clockTimestamp': start + 60,
             'steadyTimestamp': 180},
            {'type': 'Account Logout', 'clockTimestamp': start + 61,
             'steadyTimestamp': 181},
            {'type': 'Closed', 'clockTimestamp': start + 62,
             'steadyTimestamp': 182},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        parts = adapter.get_session_parts(
            'nx_' + TITLE_ID, row['date_time'],
            session_id=row['session_id'])
        before = [
            event for event in adapter._load()['users'][0]['titles'][0]['events']
            if event['type'] in ('Account Login', 'Account Logout')]
        adapter.update_session_part(
            parts[0]['part_id'], 25, parts[0]['start_time'],
            parts[0]['start_time'])
        after = [
            event for event in adapter._load()['users'][0]['titles'][0]['events']
            if event['type'] in ('Account Login', 'Account Logout')]
        self.assertEqual(after, before)

    def test_summary_first_last_are_preserved_on_edit(self):
        path, adapter = self._adapter()
        data = adapter._load()
        title = data['users'][0]['titles'][0]
        title['summary']['firstPlayed'] = 1234
        title['summary']['lastPlayed'] = 5678
        adapter._save(data)
        row = adapter.fetch_dashboard_data()[1][0]
        parts = adapter.get_session_parts(
            'nx_' + TITLE_ID, row['date_time'],
            session_id=row['session_id'])
        adapter.update_session_part(
            parts[0]['part_id'], 80, parts[0]['start_time'],
            parts[0]['start_time'])
        summary = adapter._load()['users'][0]['titles'][0]['summary']
        self.assertEqual(summary['firstPlayed'], 1234)
        self.assertEqual(summary['lastPlayed'], 5678)
        self.assertEqual(summary['playtime'], 80)

    def test_orphan_lost_focus_warns_and_repairs(self):
        start = int(datetime(2026, 1, 13, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 12,
             'steadyTimestamp': 112},
            {'type': 'Account Logout', 'clockTimestamp': start + 13,
             'steadyTimestamp': 113},
            {'type': 'Lost Focus', 'clockTimestamp': start + 13,
             'steadyTimestamp': 113},
            {'type': 'Closed', 'clockTimestamp': start + 15,
             'steadyTimestamp': 115},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        self.assertTrue(row['repair_available'])
        self.assertIn('Malformed focus sequence', row['session_warning'])
        adapter.repair_incomplete_session(
            'nx_' + TITLE_ID, row['date_time'], session_id=row['session_id'])
        repaired = adapter.fetch_dashboard_data()[1][0]
        self.assertFalse(repaired['repair_available'])
        self.assertEqual(repaired['session_warning'], '')
        types = [event['type'] for event in
                 adapter._load()['users'][0]['titles'][0]['events']]
        self.assertEqual(types.count('Lost Focus'), 1)

    def test_invalid_account_event_warns_and_repairs(self):
        start = int(datetime(2026, 1, 14, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3},
            {'type': 'Lost Focus', 'clockTimestamp': start + 12,
             'steadyTimestamp': 112},
            {'type': 'Account Logout', 'clockTimestamp': start + 13,
             'steadyTimestamp': 113},
            {'type': 'Closed', 'clockTimestamp': start + 15,
             'steadyTimestamp': 115},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        self.assertIn('Malformed account event', row['session_warning'])
        adapter.repair_incomplete_session(
            'nx_' + TITLE_ID, row['date_time'], session_id=row['session_id'])
        repaired = adapter.fetch_dashboard_data()[1][0]
        self.assertFalse(repaired['repair_available'])
        self.assertNotIn(
            'Malformed account event', repaired['session_warning'])
        types = [event['type'] for event in
                 adapter._load()['users'][0]['titles'][0]['events']]
        self.assertNotIn('Account Login', types)

    def test_add_game_requires_a_console_user_profile(self):
        path, adapter = self._adapter(_payload([]))
        data = adapter._load()
        data['users'] = []
        adapter._save(data)
        with self.assertRaises(AdapterOperationError):
            adapter.add_game('nx_' + TITLE_ID, 'Game')
        with self.assertRaises(AdapterOperationError):
            adapter.add_session('nx_' + TITLE_ID, '2026-01-14T10:00:00', 60)

    def test_segment_edits_reject_overlap_and_boundary_escape(self):
        path, adapter = self._adapter(_payload([]))
        game_id = 'nx_' + TITLE_ID
        base = datetime(2026, 1, 2, 10, 0, 0)
        base_text = base.strftime('%Y-%m-%d %H:%M:%S')
        adapter.add_session(game_id, base_text, 100)
        first = adapter.get_session_parts(game_id, base_text)[0]

        with self.assertRaises(AdapterOperationError):
            adapter.update_session_part(
                first['part_id'], 120, first['start_time'],
                first['start_time'])

        with open(path, 'rb') as handle:
            before = handle.read()
        with self.assertRaises(AdapterOperationError):
            adapter.add_session_part(
                game_id,
                (base + timedelta(seconds=3)).strftime('%Y-%m-%d %H:%M:%S'),
                1)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), before)

        adapter.add_session_part(
            game_id,
            (base + timedelta(seconds=103)).strftime('%Y-%m-%d %H:%M:%S'),
            1)
        parts = adapter.get_session_parts(game_id, base_text)
        self.assertEqual(len(parts), 2)
        with open(path, 'rb') as handle:
            before = handle.read()
        with self.assertRaises(AdapterOperationError):
            adapter.update_session_part(
                first['part_id'], 102, first['start_time'],
                first['start_time'])
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), before)
        with self.assertRaises(AdapterOperationError):
            adapter.update_session_part(
                parts[1]['part_id'], 1,
                (base + timedelta(seconds=101)).strftime(
                    '%Y-%m-%d %H:%M:%S'),
                parts[1]['start_time'])
        with self.assertRaises(AdapterOperationError):
            adapter.add_session_part(
                game_id,
                (base + timedelta(seconds=104)).strftime(
                    '%Y-%m-%d %H:%M:%S'),
                5)

    def test_segment_edit_preserves_steady_playtime_and_session_id(self):
        start = int(datetime(2026, 1, 6, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 22,
             'steadyTimestamp': 122},
            {'type': 'Gained Focus', 'clockTimestamp': start + 30,
             'steadyTimestamp': 150},
            {'type': 'Lost Focus', 'clockTimestamp': start + 60,
             'steadyTimestamp': 180},
            {'type': 'Account Logout', 'clockTimestamp': start + 61,
             'steadyTimestamp': 181},
            {'type': 'Closed', 'clockTimestamp': start + 62,
             'steadyTimestamp': 182},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        session_id = row['session_id']
        parts = adapter.get_session_parts('nx_' + TITLE_ID,
                                          row['date_time'])
        self.assertEqual([part['duration'] for part in parts], [20, 30])
        adapter.update_session_part(
            parts[0]['part_id'], 25, parts[0]['start_time'],
            parts[0]['start_time'])
        refreshed = adapter.fetch_dashboard_data()[1][0]
        self.assertEqual(refreshed['session_id'], session_id)
        self.assertEqual(refreshed['duration'], 55)
        rebuilt = adapter.get_session_parts('nx_' + TITLE_ID,
                                            refreshed['date_time'])
        self.assertEqual([part['duration'] for part in rebuilt], [25, 30])
        title = adapter._load()['users'][0]['titles'][0]
        self.assertEqual(nxal_codec.calculate_playtime(title['events']), 55)

    def test_update_session_rejects_resize_below_other_play_segments(self):
        start = int(datetime(2026, 1, 7, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 60,
             'steadyTimestamp': 162},
            {'type': 'Gained Focus', 'clockTimestamp': start + 100,
             'steadyTimestamp': 200},
            {'type': 'Lost Focus', 'clockTimestamp': start + 300,
             'steadyTimestamp': 210},
            {'type': 'Account Logout', 'clockTimestamp': start + 301,
             'steadyTimestamp': 211},
            {'type': 'Closed', 'clockTimestamp': start + 303,
             'steadyTimestamp': 213},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        self.assertEqual(row['duration'], 70)
        with self.assertRaises(AdapterOperationError):
            adapter.update_session(
                'nx_' + TITLE_ID, 50, row['date_time'], row['date_time'],
                session_id=row['session_id'])
        self.assertEqual(adapter.fetch_dashboard_data()[1][0]['duration'], 70)

    def test_cross_midnight_segment_edit_keeps_the_session_atomic(self):
        start = int(datetime(2026, 1, 1, 23, 59, 0).timestamp())
        path, adapter = self._adapter(_payload(
            _complete_events(start=start, duration=120)))
        before = adapter.fetch_dashboard_data()[1][0]
        parts = adapter.get_session_parts('nx_' + TITLE_ID,
                                          before['date_time'])
        adapter.update_session_part(
            parts[0]['part_id'], 60, parts[0]['start_time'],
            parts[0]['start_time'])
        rows = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date_time'], before['date_time'])
        self.assertEqual(rows[0]['duration'], 60)
        self.assertIn('2026-01-02', rows[0]['end_time'])
        after = adapter.get_session_parts('nx_' + TITLE_ID,
                                          rows[0]['date_time'])
        self.assertEqual(after[0]['duration'], 60)

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

    def test_fix_export_removes_orphan_focus_and_fixes_summary_pair(self):
        start = int(datetime(2026, 1, 15, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 12,
             'steadyTimestamp': 112},
            {'type': 'Account Logout', 'clockTimestamp': start + 13,
             'steadyTimestamp': 113},
            {'type': 'Lost Focus', 'clockTimestamp': start + 13,
             'steadyTimestamp': 113},
            {'type': 'Closed', 'clockTimestamp': start + 15,
             'steadyTimestamp': 115},
        ]
        data = _payload(events)
        data['users'][0]['titles'][0]['summary'] = {
            'firstPlayed': 111, 'lastPlayed': 222,
            'playtime': 999, 'launches': 1,
        }
        path, _adapter = self._adapter(data)
        result = plugin_tools.fix_import(
            {'path': path}, type('Ctx', (), {'path': path})())
        self.assertTrue(result['ok'])
        title = nxal_codec.load_json(path)['users'][0]['titles'][0]
        self.assertEqual(
            nxal_codec.find_orphaned_lost_focus(title['events']), [])
        # Tool repairs use event-derived first/last semantics.
        self.assertEqual(title['summary']['firstPlayed'], start + 3)
        self.assertEqual(title['summary']['lastPlayed'], start + 12)
        self.assertEqual(title['summary']['playtime'], 10)
        self.assertTrue(os.path.exists(path + '.bak'))

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
        merged = adapter._load()['users'][0]['titles']
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['events']), 6)
        sessions = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['duration'], 100)

    def test_add_game_writes_a_durable_zero_summary(self):
        path, adapter = self._adapter()
        adapter.add_game('nx_' + 'D' * 16, 'New Game')
        title = next(
            item for item in adapter._load()['users'][0]['titles']
            if item['id'] == 'D' * 16)
        self.assertEqual(title['summary'], {
            'firstPlayed': 0, 'lastPlayed': 0, 'playtime': 0, 'launches': 0,
        })

    def test_incomplete_session_warns_and_repairs_with_one_call(self):
        start = int(datetime(2026, 1, 3, 12, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 500},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 502},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 503},
            {'type': 'Lost Focus', 'clockTimestamp': start + 62,
             'steadyTimestamp': 562},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        self.assertTrue(row['repair_available'])
        self.assertIn('Incomplete', row['session_warning'])
        self.assertEqual(
            len(adapter.get_session_parts('nx_' + TITLE_ID,
                                          row['date_time'])), 1)
        adapter.repair_incomplete_session(
            'nx_' + TITLE_ID, row['date_time'])
        repaired = adapter.fetch_dashboard_data()[1][0]
        self.assertFalse(repaired['repair_available'])
        self.assertEqual(repaired['session_warning'], '')
        types = [event['type'] for event in
                 adapter._load()['users'][0]['titles'][0]['events']]
        self.assertEqual(
            types,
            ['Launched', 'Gained Focus', 'Account Login', 'Lost Focus',
             'Account Logout', 'Closed'])

    def test_update_session_preserves_existing_focus_and_account_events(self):
        start = int(datetime(2026, 1, 4, 10, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 100},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 102},
            {'type': 'Account Login', 'clockTimestamp': start + 3,
             'steadyTimestamp': 103},
            {'type': 'Lost Focus', 'clockTimestamp': start + 52,
             'steadyTimestamp': 152},
            {'type': 'Gained Focus', 'clockTimestamp': start + 55,
             'steadyTimestamp': 155},
            {'type': 'Lost Focus', 'clockTimestamp': start + 75,
             'steadyTimestamp': 175},
            {'type': 'Account Logout', 'clockTimestamp': start + 76,
             'steadyTimestamp': 176},
            {'type': 'Closed', 'clockTimestamp': start + 78,
             'steadyTimestamp': 178},
        ]
        path, adapter = self._adapter(_payload(events))
        before = adapter._load()['users'][0]['titles'][0]['events']
        row = adapter.fetch_dashboard_data()[1][0]
        adapter.update_session(
            'nx_' + TITLE_ID, 150, datetime.fromtimestamp(
                start).strftime('%Y-%m-%d %H:%M:%S'),
            datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M:%S'),
            session_id=row['session_id'])
        after = adapter._load()['users'][0]['titles'][0]['events']
        self.assertEqual([event['type'] for event in after],
                         [event['type'] for event in before])
        self.assertEqual(adapter.fetch_dashboard_data()[1][0]['duration'], 150)
        self.assertEqual(
            sum(1 for event in after if event['type'] == 'Gained Focus'), 2)
        self.assertIn('Account Logout', [event['type'] for event in after])


    def test_session_ids_disambiguate_identical_start_times(self):
        data = _payload(_complete_events())
        other = json.loads(json.dumps(data['users'][0]['titles'][0]))
        other['id'] = 'C' * 16
        other['name'] = 'Other Game'
        data['users'][0]['titles'].append(other)
        path, adapter = self._adapter(data)
        rows = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['session_id'] for row in rows}), 2)
        adapter.delete_session(
            rows[0]['date_time'], session_id=rows[0]['session_id'])
        remaining = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(
            remaining[0]['session_id'], rows[0]['session_id'])


    def test_delete_session_by_date_uses_the_legacy_fallback(self):
        path, adapter = self._adapter()
        row = adapter.fetch_dashboard_data()[1][0]
        adapter.delete_session(row['date_time'])
        self.assertEqual(adapter.fetch_dashboard_data()[1], [])

    def test_repair_by_session_id_uses_the_exact_group(self):
        start = int(datetime(2026, 1, 5, 12, 0, 0).timestamp())
        events = [
            {'type': 'Launched', 'clockTimestamp': start,
             'steadyTimestamp': 500},
            {'type': 'Gained Focus', 'clockTimestamp': start + 2,
             'steadyTimestamp': 502},
            {'type': 'Lost Focus', 'clockTimestamp': start + 62,
             'steadyTimestamp': 562},
        ]
        path, adapter = self._adapter(_payload(events))
        row = adapter.fetch_dashboard_data()[1][0]
        adapter.repair_incomplete_session(
            'nx_' + TITLE_ID, row['date_time'], session_id=row['session_id'])
        repaired = adapter.fetch_dashboard_data()[1][0]
        self.assertFalse(repaired['repair_available'])
        self.assertEqual(repaired['session_id'], row['session_id'])
        event_types = [event['type'] for event in
                       adapter._load()['users'][0]['titles'][0]['events']]
        self.assertIn('Account Login', event_types)
        self.assertEqual(event_types[-2:], ['Account Logout', 'Closed'])


    def test_exact_duplicate_sessions_warn_and_repair(self):
        path, adapter = self._adapter(
            _payload(_complete_events() + _complete_events()))
        rows = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['repair_available'] for row in rows))
        self.assertTrue(all('Duplicate session' in row['session_warning']
                            for row in rows))
        self.assertEqual(rows[0]['session_id'], rows[1]['session_id'])
        adapter.repair_incomplete_session(
            rows[0]['game_id'], rows[0]['date_time'],
            session_id=rows[0]['session_id'])
        repaired = adapter.fetch_dashboard_data()[1]
        self.assertEqual(len(repaired), 1)
        self.assertFalse(repaired[0]['repair_available'])
        self.assertEqual(repaired[0]['session_warning'], '')



if __name__ == '__main__':
    unittest.main()
