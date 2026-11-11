"""Unit coverage for the NXAL codec and validation helpers."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime

from plugins.nx_activity_log import nxal_codec, validate_import


class TestNxalCodec(unittest.TestCase):
    def test_hex_and_event_helpers(self):
        self.assertEqual(
            nxal_codec.hex_to_user_id_array('0' * 32), [0, 0])
        self.assertEqual(nxal_codec.hex_to_game_id('ff'), 255)
        self.assertTrue(nxal_codec.is_valid_hex('ff', 2))
        self.assertFalse(nxal_codec.is_valid_hex('ff', 3))
        self.assertFalse(nxal_codec.is_valid_hex(None, 2))
        events = [
            {'type': 'Launched', 'clockTimestamp': 1, 'steadyTimestamp': 1},
            {'type': 'Gained Focus', 'clockTimestamp': 2,
             'steadyTimestamp': 2},
            {'type': 'Lost Focus', 'clockTimestamp': 5,
             'steadyTimestamp': 5},
            {'type': 'Gained Focus', 'clockTimestamp': 6,
             'steadyTimestamp': 6},
        ]
        self.assertEqual(nxal_codec.count_launches(events), 1)
        self.assertEqual(nxal_codec.calculate_playtime(events), 3)
        self.assertEqual(nxal_codec.find_orphaned_lost_focus(events), [])
        repeated_out = [
            {'type': 'Launched', 'clockTimestamp': 1,
             'steadyTimestamp': 1},
            {'type': 'Gained Focus', 'clockTimestamp': 2,
             'steadyTimestamp': 2},
            {'type': 'Lost Focus', 'clockTimestamp': 5,
             'steadyTimestamp': 5},
            {'type': 'Lost Focus', 'clockTimestamp': 5,
             'steadyTimestamp': 5},
            {'type': 'Account Logout', 'clockTimestamp': 6,
             'steadyTimestamp': 6},
            {'type': 'Lost Focus', 'clockTimestamp': 6,
             'steadyTimestamp': 6},
        ]
        self.assertEqual(
            nxal_codec.find_orphaned_lost_focus(repeated_out), [5])
        self.assertTrue(nxal_codec.is_sorted(events))
        self.assertEqual(
            nxal_codec.event_key(events[0], ('type', 'clockTimestamp')),
            ('Launched', 1))

    def test_dedup_sort_and_summary_fallbacks(self):
        events = [
            {'type': 'Lost Focus', 'clockTimestamp': 30,
             'steadyTimestamp': 30},
            {'type': 'Account Login', 'clockTimestamp': 10,
             'steadyTimestamp': 10},
            {'type': 'Lost Focus', 'clockTimestamp': 30,
             'steadyTimestamp': 30},
        ]
        unique = nxal_codec.deduplicate_events(
            events, ('type', 'clockTimestamp', 'steadyTimestamp'))
        self.assertEqual(len(unique), 2)
        sorted_events = nxal_codec.sort_events(unique)
        self.assertEqual(sorted_events[0]['clockTimestamp'], 10)
        self.assertEqual(
            nxal_codec.first_last_from_events(sorted_events), (10, 30))
        self.assertEqual(nxal_codec.first_last_from_events(
            [], fallback_first=4, fallback_last=9), (4, 9))
        self.assertEqual(
            nxal_codec.first_last_from_events(
                [{'type': 'Launched', 'clockTimestamp': 8,
                  'steadyTimestamp': 8}]),
            (8, 8))

    def test_export_normalization_and_round_trip(self):
        payload = {
            'users': [{'name': 'Alice', 'id': 'A' * 32,
                       'titles': [{'name': 'Game', 'id': 'B' * 16,
                                   'events': [], 'summary': {}}]}],
            'exportString': 'x',
        }
        normalized = nxal_codec.normalize_export(payload)
        self.assertEqual(list(normalized), ['exportString', 'users'])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'export.json')
            nxal_codec.write_export_json(path, payload)
            loaded = nxal_codec.load_json(path)
            self.assertEqual(loaded, normalized)
            with open(path, encoding='utf-8') as handle:
                self.assertTrue(handle.read().endswith('\n'))

    def test_timestamp_and_anchor_helpers(self):
        self.assertEqual(
            nxal_codec.format_timestamp(0),
            datetime.fromtimestamp(0).strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(
            nxal_codec._parse_dt('2026-01-01 10:00:00'),
            int(datetime.strptime(
                '2026-01-01 10:00:00', '%Y-%m-%d %H:%M:%S').timestamp()))
        self.assertEqual(
            nxal_codec._steady_from((100, 50), 80), 130)
        self.assertEqual(nxal_codec._steady_from(None, 80), 0)
        self.assertEqual(
            nxal_codec._anchor_for([(10, 100), (20, 200)], 150), (10, 100))
        self.assertEqual(
            nxal_codec._anchor_for([(10, 100), (20, 200)], 250), (20, 200))
        self.assertIsNone(nxal_codec._anchor_for([], 1))


class TestNxalValidation(unittest.TestCase):
    def test_valid_export_passes_metadata_and_user_checks(self):
        payload = {
            'exportString': 'test',
            'exportTimestamp': 1767225600,
            'exportVersion': '1',
            'users': [{
                'id': 'A' * 32,
                'name': 'Alice',
                'titles': [{
                    'id': 'B' * 16,
                    'name': 'Game',
                    'summary': {
                        'firstPlayed': 1767225600,
                        'lastPlayed': 1767229200,
                        'launches': 1,
                        'playtime': 3600,
                    },
                    'events': [
                        {'type': 'Launched', 'clockTimestamp': 1767225600,
                         'steadyTimestamp': 1},
                        {'type': 'Gained Focus',
                         'clockTimestamp': 1767225602,
                         'steadyTimestamp': 3},
                        {'type': 'Lost Focus',
                         'clockTimestamp': 1767229200,
                         'steadyTimestamp': 3601},
                        {'type': 'Closed', 'clockTimestamp': 1767229200,
                         'steadyTimestamp': 3601},
                    ],
                }],
            }],
        }
        result = nxal_codec.VerificationResult()
        with redirect_stdout(io.StringIO()):
            validate_import.validate_metadata(payload, result)
            validate_import.validate_users(payload, result)
        self.assertTrue(result.passed, result.errors)


    def test_non_integer_first_last_is_an_error(self):
        payload = {
            'exportString': 'test',
            'exportTimestamp': 1767225600,
            'exportVersion': '1',
            'users': [{
                'id': 'A' * 32,
                'name': 'Alice',
                'titles': [{
                    'id': 'B' * 16,
                    'name': 'Game',
                    'summary': {
                        'firstPlayed': 'bad',
                        'lastPlayed': 1767225600,
                        'launches': 0,
                        'playtime': 0,
                    },
                    'events': [],
                }],
            }],
        }
        result = nxal_codec.VerificationResult()
        with redirect_stdout(io.StringIO()):
            validate_import.validate_metadata(payload, result)
            validate_import.validate_users(payload, result)
        self.assertFalse(result.passed)
        self.assertTrue(any('must be integers' in error
                            for error in result.errors))

    def test_malformed_summary_and_event_do_not_crash(self):
        payload = {
            'exportString': 'test',
            'exportTimestamp': 1767225600,
            'exportVersion': '1',
            'users': [{
                'id': 'A' * 32,
                'name': 'Alice',
                'titles': [{
                    'id': 'B' * 16,
                    'name': 'Game',
                    'summary': 'bad',
                    'events': [
                        {'type': 'Launched',
                         'clockTimestamp': 1767225600,
                         'steadyTimestamp': 1},
                        'bad-event',
                    ],
                }],
            }],
        }
        result = nxal_codec.VerificationResult()
        with redirect_stdout(io.StringIO()):
            validate_import.validate_metadata(payload, result)
            validate_import.validate_users(payload, result)
        self.assertFalse(result.passed)
        self.assertTrue(any('summary' in error for error in result.errors))
        self.assertTrue(any('event is not an object' in error
                            for error in result.errors))


    def test_invalid_export_reports_errors(self):
        result = nxal_codec.VerificationResult()
        with redirect_stdout(io.StringIO()):
            validate_import.validate_metadata({}, result)
            validate_import.validate_users(
                {'users': [{'id': 'bad', 'name': '', 'titles': []}]},
                result)
        self.assertFalse(result.passed)
        self.assertTrue(result.errors)


if __name__ == '__main__':
    unittest.main()


class TestNxalPluginSurfaces(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.ctx = SimpleNamespace(path='')

    def test_health_check_missing_and_valid(self):
        from plugins.nx_activity_log import plugin_health
        missing = plugin_health.check_path('/no/such/file', None, self.ctx)
        self.assertEqual(missing[0]['level'], 'error')
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'import.json')
            payload = {
                'users': [{'titles': [{'events': [
                    {'type': 'Launched', 'clockTimestamp': 1,
                     'steadyTimestamp': 1},
                    {'type': 'Gained Focus', 'clockTimestamp': 2,
                     'steadyTimestamp': 2},
                    {'type': 'Lost Focus', 'clockTimestamp': 4,
                     'steadyTimestamp': 4},
                ]}]}],
            }
            nxal_codec.write_export_json(path, payload)
            items = plugin_health.check_path(path, None, self.ctx)
            self.assertEqual(items[0]['level'], 'ok')
            self.assertIn('Users=1', items[0]['message'])

    def test_tool_paths_and_verify_export(self):
        from plugins.nx_activity_log import plugin_tools
        self.assertEqual(plugin_tools._path({}, self.ctx)[0], None)
        self.assertTrue(plugin_tools._path(
            {'path': '/no/such/file'}, self.ctx)[1].startswith('File not found'))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'import.json')
            nxal_codec.write_export_json(path, {'users': []})
            result = plugin_tools.verify_export({'path': path}, self.ctx)
            self.assertTrue(result['ok'])
            self.assertIn('Users: 0', result['lines'])
            result = plugin_tools.fix_import({'path': path}, self.ctx)
            self.assertTrue(result['ok'])
