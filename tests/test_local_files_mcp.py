"""Exercise real MCP serialization and validation with isolated files and fake launches."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastmcp import Client
import windows_gui_mcp
from windows_gui import local_paths
from windows_gui.system_health import check_mcp_component


class LocalMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_invalid_requests_return_fixed_errors(self):
        async with Client(windows_gui_mcp.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            health = check_mcp_component('fixture-time', list_tools=lambda: list(tools.values()))
            self.assertEqual('PASS', health['status'])
            self.assertEqual(42, health['details']['stable_tool_count'])
            operations = tools['inspect_path'].inputSchema['properties']['request']['anyOf']
            self.assertEqual({'stat', 'list', 'search', 'read_text'},
                             {x['properties']['operation']['const'] for x in operations})
            for name, arguments, code in [
                ('inspect_path', {'request': {'operation': 'delete', 'path': 'SECRET'}}, 'invalid_request'),
                ('manage_path', {'request': {'operation': 'mkdir', 'path': 'SECRET', 'overwrite': True}}, 'invalid_request'),
                ('open_app', {'app': ['SECRET']}, 'unknown_app'),
                ('open_path', {'path': ['SECRET']}, 'invalid_path'),
            ]:
                result = await client.call_tool(name, arguments)
                self.assertEqual({'status': 'error', 'code': code}, result.data)
                self.assertNotIn('SECRET', str(result.content))

    async def test_files_round_trip_through_mcp(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'fixture.txt').write_text('safe fixture', encoding='utf-8')
            policy = local_paths.PathPolicy({'Downloads': root, 'Documents': root})
            with patch.object(local_paths, 'PathPolicy', return_value=policy):
                async with Client(windows_gui_mcp.mcp) as client:
                    result = await client.call_tool('inspect_path', {'request': {
                        'operation': 'read_text', 'path': 'Downloads/fixture.txt'}})
                    self.assertEqual('safe fixture', result.data['text'])
                    result = await client.call_tool('manage_path', {'request': {
                        'operation': 'mkdir', 'path': 'Documents/HCI'}})
                    self.assertEqual('created', result.data['code'])
                    self.assertTrue((root / 'HCI').is_dir())
