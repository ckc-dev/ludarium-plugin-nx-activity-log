"""Environment-scoped NXAL tool functions."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from plugins.nx_activity_log import (
    deduplicate_exports,
    merge_exports,
    nxal_codec,
    nxal_repair,
    plugin_tools,
    verify_import,
)
from plugins.nx_activity_log import fix_import as fix_import_cli

TITLE_ID = '0100DEADBEEF0000'
USER_ID = 'A' * 32
START = 1767225600


def _events(start):
    return [
        {'type': 'Launched', 'clockTimestamp': start,
         'steadyTimestamp': 100},
        {'type': 'Gained Focus', 'clockTimestamp': start + 2,
         'steadyTimestamp': 102},
        {'type': 'Account Login', 'clockTimestamp': start + 3,
         'steadyTimestamp': 103},
        {'type': 'Lost Focus', 'clockTimestamp': start + 32,
         'steadyTimestamp': 132},
        {'type': 'Account Logout', 'clockTimestamp': start + 33,
         'steadyTimestamp': 133},
        {'type': 'Closed', 'clockTimestamp': start + 35,
         'steadyTimestamp': 135},
    ]


def _payload(start=START, user='Alice', title='Game', title_id=TITLE_ID):
    return {
        'exportString': 'test',
        'exportTimestamp': start,
        'exportVersion': '1.6.0',
        'users': [{
            'id': USER_ID,
            'name': user,
            'titles': [{
                'id': title_id,
                'name': title,
                'summary': {
                    'firstPlayed': start + 3,
                    'lastPlayed': start + 32,
                    'playtime': 30,
                    'launches': 1,
                },
                'events': _events(start),
            }],
        }],
    }


class TestEnvironmentTools(unittest.TestCase):
    def test_all_cli_tools_are_registered_with_environment_inputs(self):
        tools = {tool['key']: tool for tool in plugin_tools.PLUGIN_TOOLS}
        self.assertEqual(
            set(tools),
            {'validate_export', 'verify_import', 'fix_import',
             'merge_exports', 'deduplicate_exports', 'verify_export'})
        for tool in tools.values():
            self.assertTrue(tool['needs_environment'])
            self.assertFalse(any(
                field.get('type') == 'path' for field in tool['fields']))
        sources = tools['merge_exports']['fields'][0]
        self.assertEqual(sources['name'], 'sources')
        self.assertEqual(sources['type'], 'environment')
        self.assertTrue(sources['multiple'])

    def _write(self, directory, name, data):
        path = os.path.join(directory, name)
        nxal_codec.write_export_json(path, data)
        return path

    def test_validate_export_reports_full_validator_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, 'import.json', _payload())
            result = plugin_tools.validate_export(
                {'path': path}, SimpleNamespace(path=path))
            self.assertTrue(result['ok'])
            self.assertTrue(any('RESULT:' in line
                                for line in result['lines']))

    def test_verify_imported_data_reads_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            import_data = _payload()
            import_path = self._write(directory, 'import.json', import_data)
            imported = {
                'aWarning': 'generated',
                'importString': 'test',
                'importTimestamp': START,
                'importVersion': '1.6.0',
                'users': [{
                    'id': [1, 2],
                    'titles': [{
                        'id': int(TITLE_ID, 16),
                        'name': 'Game',
                        'summary': import_data['users'][0]['titles'][0][
                            'summary'],
                        'events': [
                            {
                                'clockTimestamp': event['clockTimestamp'],
                                'steadyTimestamp': event['steadyTimestamp'],
                                'type': nxal_codec.EVENT_TYPE_MAP[event['type']],
                            }
                            for event in import_data['users'][0]['titles'][0][
                                'events']
                        ],
                    }],
                }],
            }
            self._write(directory, 'importedData.json', imported)
            result = plugin_tools.verify_imported_data(
                {'path': import_path}, SimpleNamespace(path=import_path))
            self.assertTrue(result['ok'], result['lines'])


    def test_verify_import_reports_ambiguous_user_matches(self):
        import_data = {'users': [
            {'id': 'A' * 32, 'name': 'Alice', 'titles': [{'name': 'Game'}]},
            {'id': 'B' * 32, 'name': 'Bob', 'titles': [{'name': 'Game'}]},
        ]}
        imported_data = {'users': [
            {'id': [1, 2], 'titles': [{'name': 'Game'}]},
            {'id': [3, 4], 'titles': [{'name': 'Game'}]},
        ]}
        result = nxal_codec.VerificationResult()
        verify_import.verify_users(import_data, imported_data, result)
        self.assertFalse(result.passed)
        self.assertTrue(any('unambiguously' in error
                            for error in result.errors))



    def test_deduplicate_normalizes_input_before_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            data['users'][0]['titles'][0]['events'] = list(reversed(
                data['users'][0]['titles'][0]['events']))
            one = self._write(directory, 'one.json', data)
            two = self._write(directory, 'two.json', data)
            results = deduplicate_exports.deduplicate_paths(
                [one, two], backup=False)
            self.assertEqual(
                sum(result['events_after'] for result in results), 6)
            events = [
                title['events']
                for path in (one, two)
                for user in nxal_codec.load_json(path)['users']
                for title in user.get('titles', [])
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(len(events[0]), 6)

    def test_merge_and_dedup_reject_malformed_events(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            data['users'][0]['titles'][0]['events'].append('bad-event')
            one = self._write(directory, 'one.json', data)
            two = self._write(directory, 'two.json', data)
            with self.assertRaises(ValueError):
                merge_exports.merge_paths(one, [one, two], backup=False)
            with self.assertRaises(ValueError):
                deduplicate_exports.deduplicate_paths(
                    [one, two], backup=False)


    def test_merge_export_uses_target_in_place_and_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self._write(directory, 'target.json', _payload(
                start=START))
            source = self._write(
                directory, 'source.json',
                _payload(start=START + 1000, user='Bob', title='Other',
                         title_id='B' * 16))
            result = plugin_tools.merge_export(
                {'sources': [source]},
                SimpleNamespace(path=target))
            self.assertTrue(result['ok'], result['lines'])
            merged = nxal_codec.load_json(target)
            self.assertEqual(len(merged['users']), 2)
            self.assertTrue(os.path.exists(target + '.bak'))

    def test_deduplicate_export_rewrites_all_selected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            one = self._write(directory, 'one.json', _payload(start=START))
            two = self._write(directory, 'two.json', _payload(start=START))
            result = plugin_tools.deduplicate_export(
                {'sources': [two], 'match_by': 'name'},
                SimpleNamespace(path=one))
            self.assertTrue(result['ok'], result['lines'])
            self.assertTrue(os.path.exists(one + '.bak'))
            self.assertTrue(os.path.exists(two + '.bak'))
            remaining = []
            for path in (one, two):
                remaining.extend(
                    title['events']
                    for user in nxal_codec.load_json(path)['users']
                    for title in user.get('titles', []))
            self.assertEqual(len(remaining), 1)


    def test_fix_export_preserves_summary_only_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            title = data['users'][0]['titles'][0]
            title['events'] = []
            title['summary'] = {
                'firstPlayed': 1000, 'lastPlayed': 2000,
                'playtime': 500, 'launches': 3,
            }
            path = self._write(directory, 'import.json', data)
            result = plugin_tools.fix_export(
                {'path': path}, SimpleNamespace(path=path))
            self.assertTrue(result['ok'], result['lines'])
            self.assertEqual(
                nxal_codec.load_json(path)['users'][0]['titles'][0][
                    'summary'],
                title['summary'])

    def test_fix_export_can_reset_summary_only_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            title = data['users'][0]['titles'][0]
            title['events'] = []
            title['summary'] = {
                'firstPlayed': 1000, 'lastPlayed': 2000,
                'playtime': 500, 'launches': 3,
            }
            path = self._write(directory, 'import.json', data)
            result = plugin_tools.fix_export(
                {'path': path, 'preserve_summary_only': ''},
                SimpleNamespace(path=path))
            self.assertTrue(result['ok'], result['lines'])
            self.assertEqual(
                nxal_codec.load_json(path)['users'][0]['titles'][0][
                    'summary'],
                {'firstPlayed': 0, 'lastPlayed': 0,
                 'playtime': 0, 'launches': 0})

    def test_merge_preserves_summary_only_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = {
                'firstPlayed': 1000, 'lastPlayed': 2000,
                'playtime': 500, 'launches': 3,
            }
            target_data = _payload()
            target_title = target_data['users'][0]['titles'][0]
            target_title['events'] = []
            target_title['summary'] = dict(summary)
            target = self._write(directory, 'target.json', target_data)
            source_data = _payload()
            source_title = source_data['users'][0]['titles'][0]
            source_title['events'] = []
            source_title['summary'] = {
                'firstPlayed': 1200, 'lastPlayed': 3000,
                'playtime': 700, 'launches': 4,
            }
            source = self._write(directory, 'source.json', source_data)
            result = plugin_tools.merge_export(
                {'sources': [source]},
                SimpleNamespace(path=target))
            self.assertTrue(result['ok'], result['lines'])
            merged = nxal_codec.load_json(target)['users'][0]['titles'][0]
            self.assertEqual(merged['summary'], {
                'firstPlayed': 1000, 'lastPlayed': 3000,
                'playtime': 700, 'launches': 4,
            })

    def test_dedup_preserves_summary_only_title_once(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = {
                'firstPlayed': 1000, 'lastPlayed': 2000,
                'playtime': 500, 'launches': 3,
            }
            data = _payload()
            data['users'][0]['titles'][0]['events'] = []
            data['users'][0]['titles'][0]['summary'] = dict(summary)
            one = self._write(directory, 'one.json', data)
            two = self._write(directory, 'two.json', data)
            result = plugin_tools.deduplicate_export(
                {'sources': [two], 'match_by': 'name'},
                SimpleNamespace(path=one))
            self.assertTrue(result['ok'], result['lines'])
            titles = [
                title
                for path in (one, two)
                for user in nxal_codec.load_json(path)['users']
                for title in user.get('titles', [])
            ]
            self.assertEqual(len(titles), 1)
            self.assertEqual(titles[0]['summary'], summary)


    def test_dedup_prefers_accountful_copy_of_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            accountful = _payload()
            accountless = _payload()
            accountless['users'][0]['titles'][0]['events'] = [
                event for event in accountless['users'][0]['titles'][0][
                    'events']
                if event['type'] not in ('Account Login', 'Account Logout')
            ]
            # Make the accountless file the older owner; the accountful copy
            # must still win.
            accountless['exportTimestamp'] = START - 10
            one = self._write(directory, 'accountless.json', accountless)
            two = self._write(directory, 'accountful.json', accountful)
            deduplicate_exports.deduplicate_paths([one, two], backup=False)
            titles = [
                title
                for path in (one, two)
                for user in nxal_codec.load_json(path)['users']
                for title in user.get('titles', [])
            ]
            self.assertEqual(len(titles), 1)
            self.assertTrue(any(
                event['type'] == 'Account Login'
                for event in titles[0]['events']))

    def test_merge_combines_accountless_and_accountful_session(self):
        with tempfile.TemporaryDirectory() as directory:
            accountful = _payload()
            accountless = _payload()
            accountless['exportTimestamp'] = START - 10
            accountless['users'][0]['titles'][0]['events'] = [
                event for event in accountless['users'][0]['titles'][0][
                    'events']
                if event['type'] not in ('Account Login', 'Account Logout')
            ]
            target = self._write(directory, 'target.json', accountless)
            source = self._write(directory, 'source.json', accountful)
            merge_exports.merge_paths(target, [target, source], backup=False)
            title = nxal_codec.load_json(target)['users'][0]['titles'][0]
            self.assertTrue(any(
                event['type'] == 'Account Login'
                for event in title['events']))
            self.assertEqual(
                nxal_repair.summary_for_events(title['events'])['launches'],
                1)



    def test_cli_recalculate_first_last_does_not_require_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            data['users'][0]['titles'][0]['summary'] = {
                'firstPlayed': 1, 'lastPlayed': 2,
                'playtime': 30, 'launches': 1,
            }
            path = self._write(directory, 'import.json', data)
            with mock.patch.object(fix_import_cli, 'FIX_WARNINGS', False), \
                    mock.patch.object(fix_import_cli, 'FIX_FIRST_LAST', True), \
                    mock.patch.object(
                        fix_import_cli.sys, 'argv',
                        ['fix_import.py', path]), \
                    mock.patch('builtins.input', return_value='y'):
                fix_import_cli.main()
            summary = nxal_codec.load_json(path)['users'][0]['titles'][0][
                'summary']
            self.assertEqual(summary['firstPlayed'], START + 3)
            self.assertEqual(summary['lastPlayed'], START + 32)


    def test_cli_fix_first_last_matches_tool_behavior(self):
        summary = {
            'firstPlayed': 1, 'lastPlayed': 2,
            'playtime': 30, 'launches': 1,
        }
        with mock.patch('builtins.input', return_value='y'):
            fixed, changed = fix_import_cli.fix_first_last(
                summary, _events(START))
        self.assertTrue(changed)
        self.assertEqual(fixed['firstPlayed'], START + 3)
        self.assertEqual(fixed['lastPlayed'], START + 32)


    def test_fix_export_preserves_pdm_first_last_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            title = data['users'][0]['titles'][0]
            title['summary'] = {
                'firstPlayed': 111, 'lastPlayed': 222,
                'playtime': 999, 'launches': 0,
            }
            path = self._write(directory, 'import.json', data)
            result = plugin_tools.fix_export(
                {'path': path}, SimpleNamespace(path=path))
            self.assertTrue(result['ok'], result['lines'])
            summary = nxal_codec.load_json(path)['users'][0]['titles'][0][
                'summary']
            self.assertEqual(summary['firstPlayed'], 111)
            self.assertEqual(summary['lastPlayed'], 222)
            self.assertEqual(summary['playtime'], 30)
            self.assertEqual(summary['launches'], 1)


    def test_fix_export_sorts_and_repairs_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            data = _payload()
            title = data['users'][0]['titles'][0]
            title['events'] = list(reversed(title['events']))
            title['summary'] = {
                'firstPlayed': 1, 'lastPlayed': 2,
                'playtime': 999, 'launches': 0,
            }
            path = self._write(directory, 'import.json', data)
            result = plugin_tools.fix_export(
                {'path': path, 'recalculate_first_last': 'on'},
                SimpleNamespace(path=path))
            self.assertTrue(result['ok'], result['lines'])
            fixed = nxal_codec.load_json(path)
            events = fixed['users'][0]['titles'][0]['events']
            self.assertEqual(
                events, nxal_codec.sort_events(events))
            summary = fixed['users'][0]['titles'][0]['summary']
            self.assertEqual(summary['firstPlayed'], START + 3)
            self.assertEqual(summary['lastPlayed'], START + 32)
            self.assertEqual(summary['playtime'], 30)
            self.assertEqual(summary['launches'], 1)
            self.assertTrue(os.path.exists(path + '.bak'))


if __name__ == '__main__':
    unittest.main()
