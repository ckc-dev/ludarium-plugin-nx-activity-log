"""Environment-scoped NXAL tool functions."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from plugins.nx_activity_log import nxal_codec, plugin_tools

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
                {'path': path}, SimpleNamespace(path=path))
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
