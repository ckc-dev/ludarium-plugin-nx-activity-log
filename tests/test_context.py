"""Explicit plugin page/tool context and template namespaces."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from plugin_system import (
    PluginContext,
    current_plugin_context,
    registry,
)

os.environ.setdefault(
    "LUDARIUM_TEST_PLUGIN_DIRS",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import support  # noqa: E402,F401  (isolates metadata + plugins)


class TestPluginContext(unittest.TestCase):
    def test_template_namespace_uses_type_not_directory(self):
        manifest = registry.get('nx_activity_log')
        ctx = PluginContext(None, None, manifest)
        self.assertEqual(ctx.template_namespace, 'nx_activity_log')
        self.assertEqual(
            ctx.qualified('index.html'), 'nx_activity_log/index.html')
        self.assertEqual(
            ctx.qualified('nx_activity_log/index.html'),
            'nx_activity_log/index.html')

    def test_clone_with_environment_exposes_path_and_config(self):
        manifest = registry.get('nx_activity_log')
        ctx = PluginContext(None, None, manifest)
        environment = {
            'id': 1,
            'uuid': 'env-uuid',
            'name': 'NXAL',
            'type': 'nx_activity_log',
            'path': '/tmp/import.json',
            'config': {'profile_mapping': '{"Alice": 1}'},
        }
        clone = ctx.clone_with_environment(environment)
        self.assertEqual(clone.path, '/tmp/import.json')
        self.assertEqual(clone.source_id, 'env-uuid')
        self.assertEqual(clone.config['profile_mapping'], '{"Alice": 1}')
        self.assertEqual(clone.environment['name'], 'NXAL')
        self.assertIsNone(ctx.path)

    def test_current_plugin_context_reads_the_request_local_context(self):
        from flask import g

        with support.get_app().test_request_context('/'):
            g.plugin_context = 'request-context'
            self.assertEqual(current_plugin_context(), 'request-context')

    def test_tool_uses_the_selected_environment_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'import.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'users': []}, handle)
            config = {
                'id': 99,
                'uuid': 'environment-uuid',
                'name': 'NXAL Test',
                'type': 'nx_activity_log',
                'path': path,
                'config': {'profile_mapping': '{}'},
                'default_user_id': 1,
            }
            client = support.get_app().test_client()
            with patch('app.accounts.get_system_configs',
                       return_value=[config]):
                response = client.get(
                    '/tools/nx_activity_log/verify_export')
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'_environment_id', response.data)

                response = client.post(
                    '/tools/nx_activity_log/verify_export',
                    data={'_environment_id': '99', 'path': ''})
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Users: 0', response.data)


if __name__ == '__main__':
    unittest.main()
