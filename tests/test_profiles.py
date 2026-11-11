"""Native NXAL profile discovery for the host environment editor."""

import os
import tempfile
import unittest

from plugins.nx_activity_log.profiles import list_profiles

START = 1767225600


def _write(directory, data):
    import json
    path = os.path.join(directory, 'profiles.json')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle)
    return path


class TestProfileDiscovery(unittest.TestCase):
    def test_export_profiles_use_native_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(directory, {'users': [
                {'id': 'A' * 32, 'name': 'Alice', 'titles': []},
                {'id': 'B' * 32, 'name': 'Bob', 'titles': []},
            ]})
            self.assertEqual(list_profiles(path), [
                {'key': 'A' * 32, 'name': 'Alice'},
                {'key': 'B' * 32, 'name': 'Bob'},
            ])

    def test_missing_native_id_falls_back_to_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(directory, {'users': [
                {'name': 'Alice', 'titles': []},
            ]})
            self.assertEqual(list_profiles(path), [
                {'key': 'name:alice', 'name': 'Alice'},
            ])

    def test_duplicate_names_or_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(directory, {'users': [
                {'id': 'A' * 32, 'name': 'Alice', 'titles': []},
                {'id': 'B' * 32, 'name': 'alice', 'titles': []},
            ]})
            with self.assertRaises(ValueError):
                list_profiles(path)
            path = _write(directory, {'users': [
                {'id': 'A' * 32, 'name': 'Alice', 'titles': []},
                {'id': 'A' * 32, 'name': 'Bob', 'titles': []},
            ]})
            with self.assertRaises(ValueError):
                list_profiles(path)

    def test_imported_data_without_names_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(directory, {'users': [
                {'id': [1, 2], 'titles': []},
            ]})
            with self.assertRaises(ValueError):
                list_profiles(path)


if __name__ == '__main__':
    unittest.main()
