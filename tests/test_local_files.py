"""Native Windows filesystem tests use only disposable owned directories, never GUI."""

import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import win32file
import pywintypes

from windows_gui import applications as apps
from windows_gui import files
from windows_gui import local_paths as paths


def junction(link: Path, target: Path):
    """Windows junction fixture, no shell and no symlink privilege prerequisite."""
    link.mkdir(exist_ok=True)
    substitute = ('\\??\\' + str(target)).encode('utf-16-le')
    display = str(target).encode('utf-16-le')
    data = struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(display))
    data += substitute + b'\0\0' + display + b'\0\0'
    buffer = struct.pack('<IHH', 0xA0000003, len(data), 0) + data
    handle = win32file.CreateFile(str(link), paths.GENERIC_WRITE, 7, None, 3, paths.OPEN_FLAGS, None)
    try:
        win32file.DeviceIoControl(handle, 0x900A4, buffer, None)
    finally:
        handle.Close()


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.downloads = self.base / 'Downloads'
        self.documents = self.base / 'Documents'
        self.downloads.mkdir()
        self.documents.mkdir()
        self.policy = paths.PathPolicy({'Downloads': self.downloads, 'Documents': self.documents})

    def file(self, name='example.txt', data=b'fixture'):
        target = self.downloads / name
        target.write_bytes(data)
        return target

    def inspect(self, operation='stat', path='Downloads', **kw):
        return files.inspect(dict(operation=operation, path=path, **kw), policy=self.policy)

    def manage(self, operation, **kw):
        return files.manage(dict(operation=operation, **kw), policy=self.policy)

    def assertCode(self, result, code):
        self.assertEqual(code, result['code'], result)


class PathPolicyTests(LocalFixture):
    def test_alias_and_absolute_root_and_sanitized_output(self):
        target = self.file()
        self.assertEqual(self.policy.target(str(target)), self.policy.target('Downloads/example.txt'))
        result = self.inspect(path=str(target))
        self.assertCode(result, 'ok')
        self.assertEqual({'status', 'code', 'path', 'type', 'size', 'modified_time'}, set(result))
        self.assertNotIn(str(self.base), json.dumps(result))

    def test_sibling_prefix_is_outside(self):
        sibling = self.base / 'Downloads-escape'
        sibling.mkdir()
        self.assertCode(self.inspect(path=str(sibling)), 'outside_allowed_roots')

    def test_traversal_ads_unc_devices_nt_relative_and_expansion(self):
        for value in ['Downloads/../Documents', 'Downloads/./x', 'Downloads/a:secret',
                      '\\\\server\\share\\x', '\\\\?\\C:\\x', '\\\\.\\C:\\x', '\\??\\C:\\x',
                      'C:relative', '/absolute', 'relative', '%USERPROFILE%/x',
                      'Downloads/$HOME', 'Downloads/~user', 'Downloads/*.txt',
                      'Downloads/a?', 'Downloads/NUL.txt', 'Downloads/COM1',
                      'Downloads/x.', 'Downloads/x ', 'Downloads/a\x00b', 'Downloads//x']:
            with self.subTest(value=value):
                self.assertCode(self.inspect(path=value), 'invalid_path')

    def test_network_drive_rejected_before_open(self):
        with patch.object(win32file, 'GetDriveType', return_value=4), patch.object(paths, '_open') as opened:
            self.assertCode(self.inspect(), 'local_fixed_drive_required')
            opened.assert_not_called()

    def test_missing_path_is_fixed(self):
        self.assertEqual({'status': 'error', 'code': 'not_found'}, self.inspect(path='Downloads/missing.txt'))

    def test_reparse_junction_source_and_parent_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('private')
        link = self.downloads / 'link'
        junction(link, outside)
        for value in ['Downloads/link', 'Downloads/link/secret.txt']:
            self.assertCode(self.inspect(path=value), 'reparse_point_not_supported')
        self.assertCode(self.manage('mkdir', path='Downloads/link/child'), 'reparse_point_not_supported')
        self.assertFalse((outside / 'child').exists())

    def test_mocked_symlink_file_attribute_rejected(self):
        source = self.file()
        original = win32file.GetFileInformationByHandle
        def tagged(handle):
            info = original(handle)
            if not info[0] & paths.DIRECTORY:
                return (info[0] | paths.REPARSE, *info[1:])
            return info
        with patch.object(win32file, 'GetFileInformationByHandle', side_effect=tagged):
            self.assertCode(self.inspect(path=str(source)), 'reparse_point_not_supported')

    def test_hardlink_is_rejected(self):
        source = self.file()
        os.link(source, self.base / 'outside-hardlink.txt')
        self.assertCode(self.inspect(path=str(source)), 'hardlink_not_supported')

    def test_final_handle_path_mismatch_is_rejected(self):
        with patch.object(win32file, 'GetFinalPathNameByHandle', return_value='\\\\?\\C:\\other'):
            self.assertCode(self.inspect(), 'path_changed')

    def test_handles_close_after_rejection(self):
        source = self.file()
        with patch.object(paths, '_checked', side_effect=paths.PathError('path_changed')):
            self.assertCode(self.inspect(path=str(source)), 'path_changed')
        source.rename(source.with_name('after.txt'))


class InspectTests(LocalFixture):
    def test_crlf_is_preserved_and_unicode_path_is_literal(self):
        target = self.file('\u8bfe\u7a0b-\U0001f4da.txt', b'line1\r\nline2\r\n')
        result = self.inspect('read_text', str(target))
        self.assertCode(result, 'ok')
        self.assertEqual('line1\r\nline2\r\n', result['text'])

    def test_read_utf8_and_truncation_without_cache(self):
        target = self.file(data='hello \u4e16\u754c'.encode('utf-8'))
        result = self.inspect('read_text', str(target), max_chars=5)
        self.assertEqual('hello', result['text'])
        self.assertTrue(result['truncated'])
        target.write_bytes(b'new')
        result = self.inspect('read_text', str(target))
        self.assertEqual('new', result['text'])
        self.assertFalse(result['truncated'])

    def test_explicit_encoding_and_bom_policy(self):
        text = '\u4f60\u597d'
        for encoding in ['utf-8-sig', 'utf-16', 'gb18030']:
            self.file(data=text.encode(encoding))
            result = self.inspect('read_text', 'Downloads/example.txt',
                                  encoding='utf-8' if encoding == 'utf-8-sig' else encoding)
            self.assertEqual(text, result['text'])
        self.file(data=b'ab')
        self.assertCode(self.inspect('read_text', 'Downloads/example.txt', encoding='utf-16'), 'invalid_encoding')

    def test_binary_and_invalid_encoding(self):
        self.file(data=b'plain\x00binary')
        self.assertCode(self.inspect('read_text', 'Downloads/example.txt'), 'binary_not_supported')
        self.file(data=b'\xff\xfe\xff')
        self.assertCode(self.inspect('read_text', 'Downloads/example.txt'), 'invalid_encoding')

    def test_binary_tail_after_truncation_is_rejected(self):
        self.file(data=b'hello\x00')
        self.assertCode(self.inspect('read_text', 'Downloads/example.txt', max_chars=2), 'binary_not_supported')

    def test_oversize_text_rejected_before_read(self):
        self.file(data=b'a' * 20)
        with patch.object(files, 'MAX_TEXT_FILE', 10), patch.object(win32file, 'ReadFile') as read:
            self.assertCode(self.inspect('read_text', 'Downloads/example.txt'), 'text_too_large')
            read.assert_not_called()

    def test_directory_cannot_be_read_as_file(self):
        self.assertCode(self.inspect('read_text'), 'regular_file_required')

    def test_invalid_operations_and_irrelevant_fields(self):
        for request in [dict(operation='delete', path='Downloads'),
                        dict(operation='stat', path='Downloads', max_depth=2),
                        dict(operation='list', path='Downloads', limit=0),
                        dict(operation='search', path='Downloads', max_depth=6),
                        dict(operation='read_text', path='Downloads', encoding='auto')]:
            self.assertCode(files.inspect(request, policy=self.policy), 'invalid_request')

    def test_single_directory_list_sorted_and_filtered(self):
        self.file('b.pdf'); self.file('a.pdf'); self.file('c.txt')
        child = self.downloads / 'child'; child.mkdir(); (child / 'hidden.pdf').write_bytes(b'x')
        result = self.inspect('list', extension='.PDF')
        self.assertEqual(['Downloads/a.pdf', 'Downloads/b.pdf'], [x['path'] for x in result['entries']])
        self.assertFalse(result['partial'])

    def test_latest_is_selected_after_complete_scan_not_result_limit(self):
        first = self.file('a.pdf'); latest = self.file('z.pdf'); other = self.file('m.pdf')
        os.utime(first, (1000, 1000)); os.utime(other, (2000, 2000)); os.utime(latest, (3000, 3000))
        result = self.inspect('search', extension='.pdf', sort='modified_desc', limit=1)
        self.assertEqual('Downloads/z.pdf', result['entries'][0]['path'])
        self.assertTrue(result['results_truncated'])
        self.assertTrue(result['latest_in_scope_verified'])
        self.assertEqual(3, result['matched_entries'])

    def test_depth_is_explicit_scope(self):
        child = self.downloads / 'child'; child.mkdir(); (child / 'inside.pdf').write_bytes(b'x')
        self.assertEqual([], self.inspect('search', extension='.pdf', max_depth=0)['entries'])
        self.assertEqual(1, len(self.inspect('search', extension='.pdf', max_depth=1)['entries']))

    def test_empty_complete_scan_does_not_claim_a_latest_file(self):
        result = self.inspect('search', extension='.pdf', sort='modified_desc')
        self.assertTrue(result['scan_complete'])
        self.assertFalse(result['latest_in_scope_verified'])
        self.assertEqual([], result['entries'])

    def test_entry_bound_marks_partial_not_latest(self):
        self.file('a.pdf'); self.file('b.pdf')
        with patch.object(files, 'MAX_SCAN_ENTRIES', 1):
            result = self.inspect('search', extension='.pdf', sort='modified_desc')
        self.assertEqual('partial', result['status'])
        self.assertFalse(result['scan_complete'])
        self.assertFalse(result['latest_in_scope_verified'])

    def test_execution_budget_marks_partial(self):
        self.file()
        with patch.object(files, 'SCAN_SECONDS', -1):
            result = self.inspect('search')
        self.assertTrue(result['partial'])
        self.assertEqual(0, result['scanned_entries'])

    def test_reparse_region_is_not_followed_and_reported_partial(self):
        outside = self.base / 'outside'; outside.mkdir(); (outside / 'secret.pdf').write_bytes(b'x')
        junction(self.downloads / 'link', outside)
        result = self.inspect('search', extension='.pdf')
        self.assertEqual([], result['entries'])
        self.assertTrue(result['partial'])
        self.assertEqual(1, result['skipped_entries'])

    def test_filter_rejects_glob_and_injection(self):
        for extension in ['*.pdf', '.pdf/..', '.pdf;cmd', 'pdf']:
            self.assertCode(self.inspect('list', extension=extension), 'invalid_filter')

    def test_errors_never_echo_content_or_path(self):
        with patch.object(paths, 'pinned', side_effect=RuntimeError('SECRET /private/path')):
            self.assertEqual({'status': 'error', 'code': 'operation_failed'}, self.inspect())


class ManageTests(LocalFixture):
    def test_mkdir_single_level_and_existing_status(self):
        self.assertCode(self.manage('mkdir', path='Documents/HCI'), 'created')
        self.assertCode(self.manage('mkdir', path='Documents/HCI'), 'destination_exists')
        self.assertCode(self.manage('mkdir', path='Documents/missing/child'), 'not_found')
        self.assertCode(self.manage('mkdir', path='Documents'), 'destination_exists')

    def test_copy_move_rename_preserve_content(self):
        source = self.file(data=b'original')
        self.assertCode(self.manage('copy', source='Downloads/example.txt', destination='Documents/copy.txt'), 'copied')
        self.assertEqual(b'original', source.read_bytes())
        self.assertCode(self.manage('move', source='Documents/copy.txt', destination='Downloads/moved.txt'), 'moved')
        self.assertFalse((self.documents / 'copy.txt').exists())
        self.assertCode(self.manage('rename', source='Downloads/moved.txt', new_name='renamed.txt'), 'renamed')
        self.assertEqual(b'original', (self.downloads / 'renamed.txt').read_bytes())

    def test_destination_exists_never_overwritten(self):
        self.file(data=b'source'); target = self.documents / 'target.txt'; target.write_bytes(b'keep')
        for operation in ['copy', 'move']:
            self.assertCode(self.manage(operation, source='Downloads/example.txt', destination='Documents/target.txt'), 'destination_exists')
            self.assertEqual(b'keep', target.read_bytes())
        self.file('other.txt', b'keep')
        self.assertCode(self.manage('rename', source='Downloads/example.txt', new_name='other.txt'), 'destination_exists')

    def test_same_source_and_destination_is_not_a_replace(self):
        self.file()
        self.assertCode(self.manage('move', source='Downloads/example.txt', destination='Downloads/example.txt'), 'destination_exists')
        self.assertCode(self.manage('rename', source='Downloads/example.txt', new_name='example.txt'), 'destination_exists')

    def test_concurrent_destination_creation_at_publish(self):
        for operation in ['copy', 'move']:
            with self.subTest(operation=operation):
                source = self.file(operation + '.txt', b'source')
                destination = self.documents / (operation + '.txt')
                original = paths.rename_handle
                def race(handle, parent, name):
                    target = parent.path / name
                    target.write_bytes(b'concurrent')
                    original(handle, parent, name)
                with patch.object(paths, 'rename_handle', side_effect=race):
                    result = self.manage(operation, source=str(source), destination=str(destination))
                self.assertCode(result, 'destination_exists')
                self.assertEqual(b'concurrent', destination.read_bytes())
                self.assertEqual(b'source', source.read_bytes())
                self.assertEqual([], list(self.documents.glob('.ai-work-*.tmp')))

    def test_cross_volume_rejected_without_copy_fallback(self):
        self.file()
        original = paths._checked
        def different_volume(path, handle, **kw):
            lease = original(path, handle, **kw)
            if path == self.documents:
                values = list(lease.info); values[4] += 1
                return replace(lease, info=tuple(values))
            return lease
        with patch.object(paths, '_checked', side_effect=different_volume), patch.object(files, '_copy') as copy:
            result = self.manage('move', source='Downloads/example.txt', destination='Documents/new.txt')
            self.assertCode(result, 'cross_volume_not_supported')
            copy.assert_not_called()
        self.assertTrue((self.downloads / 'example.txt').exists())

    def test_locked_source_fails_closed(self):
        source = self.file()
        handle = win32file.CreateFile(str(source), paths.GENERIC_WRITE, 7, None, 3, 0, None)
        try:
            self.assertCode(self.manage('copy', source=str(source), destination='Documents/new.txt'), 'file_busy')
        finally:
            handle.Close()

    def test_permission_error_is_fixed_and_no_destination(self):
        self.file()
        with patch.object(paths, 'rename_handle', side_effect=OSError(5, 'sensitive text')):
            result = self.manage('move', source='Downloads/example.txt', destination='Documents/new.txt')
        self.assertEqual({'status': 'error', 'code': 'permission_denied'}, result)
        self.assertFalse((self.documents / 'new.txt').exists())

    def test_copy_failure_cleans_only_its_temp(self):
        self.file(); unrelated = self.documents / '.ai-work-unrelated.tmp'; unrelated.write_bytes(b'keep')
        with patch.object(win32file, 'WriteFile', side_effect=OSError(112, 'sensitive')):
            self.assertCode(self.manage('copy', source='Downloads/example.txt', destination='Documents/new.txt'), 'disk_full')
        self.assertEqual([unrelated], list(self.documents.iterdir()))

    def test_copy_size_and_time_bounds(self):
        self.file(data=b'123456')
        with patch.object(files, 'MAX_COPY_BYTES', 5):
            self.assertCode(self.manage('copy', source='Downloads/example.txt', destination='Documents/new.txt'), 'copy_too_large')
        with patch.object(files, 'COPY_SECONDS', -1):
            self.assertCode(self.manage('copy', source='Downloads/example.txt', destination='Documents/new.txt'), 'operation_timeout')
        self.assertEqual([], list(self.documents.iterdir()))

    def test_rename_cannot_change_parent(self):
        self.file()
        for name in ['../escape.txt', 'child/new.txt', 'child\\new.txt', 'C:\\escape.txt', '.','..', 'x:stream']:
            self.assertCode(self.manage('rename', source='Downloads/example.txt', new_name=name), 'invalid_path')

    def test_no_delete_overwrite_recursive_or_extra_args(self):
        for request in [dict(operation='delete', source='Downloads/x'),
                        dict(operation='copy', source='Downloads/x', destination='Documents/y', overwrite=True),
                        dict(operation='mkdir', path='Documents/HCI', parents=True),
                        dict(operation='rename', source='Downloads/x', new_name='y', destination='Documents')]:
            self.assertCode(files.manage(request, policy=self.policy), 'invalid_request')

    def test_directories_cannot_copy_move_or_rename(self):
        for operation in ['copy', 'move']:
            self.assertCode(self.manage(operation, source='Downloads', destination='Documents/dir'), 'regular_file_required')
        self.assertCode(self.manage('rename', source='Downloads', new_name='renamed'), 'outside_allowed_roots')

    def test_source_replacement_after_open_is_blocked(self):
        source = self.file(); original = paths._checked; attempts = []
        def race(path, handle, **kw):
            result = original(path, handle, **kw)
            if path == source:
                with self.assertRaises(OSError):
                    source.rename(source.with_name('stolen.txt'))
                attempts.append(True)
            return result
        with patch.object(paths, '_checked', side_effect=race):
            self.assertCode(self.manage('move', source=str(source), destination='Documents/new.txt'), 'moved')
        self.assertEqual([True], attempts)
        self.assertFalse((self.downloads / 'stolen.txt').exists())

    def test_destination_parent_replacement_after_check_is_blocked(self):
        source = self.file(); original = files._absent
        attempts = []
        def race(parent, name):
            with self.assertRaises(OSError):
                self.documents.rename(self.base / 'stolen-parent')
            attempts.append(True)
            return original(parent, name)
        with patch.object(files, '_absent', side_effect=race):
            self.assertCode(self.manage('move', source=str(source), destination='Documents/new.txt'), 'moved')
        self.assertEqual([True], attempts)

    def test_parent_becomes_junction_during_operation_fails_closed(self):
        self.file(); outside = self.base / 'outside'; outside.mkdir()
        original = files._absent; attempts = []
        def race(parent, name):
            junction(self.documents, outside)
            attempts.append(True)
            original(parent, name)
        with patch.object(files, '_absent', side_effect=race):
            self.assertCode(self.manage('copy', source='Downloads/example.txt', destination='Documents/new.txt'), 'reparse_point_not_supported')
        self.assertEqual([True], attempts)
        self.assertEqual([], list(outside.iterdir()))

    def test_reparse_destination_parent_is_rejected(self):
        self.file(); outside = self.base / 'outside'; outside.mkdir()
        junction(self.documents / 'link', outside)
        for operation in ['copy', 'move']:
            self.assertCode(self.manage(operation, source='Downloads/example.txt', destination='Documents/link/new.txt'), 'reparse_point_not_supported')
        self.assertEqual([], list(outside.iterdir()))

    def test_junction_change_immediately_before_native_rename_cannot_redirect(self):
        source = self.file()
        outside = self.base / 'outside'; outside.mkdir()
        original = paths.rename_handle
        def race(handle, parent, name):
            junction(parent.path, outside)
            original(handle, parent, name)
        with patch.object(paths, 'rename_handle', side_effect=race):
            self.assertCode(self.manage('move', source=str(source), destination='Documents/new.txt'), 'reparse_point_not_supported')
        self.assertEqual(b'fixture', source.read_bytes())
        self.assertEqual([], list(outside.iterdir()))

    def test_mkdir_parent_changed_to_junction_cannot_redirect(self):
        outside = self.base / 'outside'; outside.mkdir()
        original = files._absent
        def race(parent, name):
            original(parent, name)
            junction(parent.path, outside)
        with patch.object(files, '_absent', side_effect=race):
            self.assertCode(self.manage('mkdir', path='Documents/HCI'), 'reparse_point_not_supported')
        self.assertEqual([], list(outside.iterdir()))


class OpenTests(LocalFixture):
    def test_document_dispatch_injected_and_bounded_result(self):
        for name in ['safe.pdf', 'safe.txt', 'safe.md', 'safe.PNG']:
            target = self.file(name)
            launcher = Mock()
            result = apps.open_local(str(target), policy=self.policy, document_launcher=launcher)
            self.assertCode(result, 'open_requested')
            launcher.assert_called_once_with(target)
            self.assertNotIn(str(self.base), json.dumps(result))

    def test_directory_dispatch_only_explorer_with_one_literal_argument(self):
        executable = self.file('fixture.exe')
        launcher, resolver = Mock(), Mock(return_value=executable)
        result = apps.open_local('Documents', policy=self.policy, app_launcher=launcher, resolver=resolver)
        self.assertCode(result, 'open_requested')
        resolver.assert_called_once_with('explorer')
        launcher.assert_called_once_with(executable, (str(self.documents),))

    def test_executable_script_shortcut_and_unknown_formats_rejected(self):
        for ext in ['exe','com','bat','cmd','ps1','vbs','js','msi','scr','lnk','url','hta','dll','docm','docx']:
            target = self.file('blocked.' + ext)
            launcher = Mock()
            self.assertCode(apps.open_local(str(target), policy=self.policy, document_launcher=launcher), 'document_type_not_allowed')
            launcher.assert_not_called()

    def test_missing_document_never_launches(self):
        launcher = Mock()
        self.assertCode(apps.open_local('Downloads/missing.pdf', policy=self.policy, document_launcher=launcher), 'not_found')
        launcher.assert_not_called()

    def test_known_app_alias_and_injected_resolver_launcher(self):
        executable = self.file('fixture.exe')
        for alias in apps.APP_ALIASES:
            launcher, resolver = Mock(), Mock(return_value=executable)
            self.assertCode(apps.launch_app(alias, resolver=resolver, launcher=launcher), 'launch_requested')
            resolver.assert_called_once_with(alias)
            launcher.assert_called_once_with(executable)

    def test_unknown_app_paths_interpreters_and_injection_rejected(self):
        for alias in ['cmd','powershell','pwsh','wscript','cscript','C:\\app.exe',
                      'notepad file.txt','notepad;cmd','notepad.exe','https://example.com', 'unknown', '']:
            resolver, launcher = Mock(), Mock()
            self.assertCode(apps.launch_app(alias, resolver=resolver, launcher=launcher), 'unknown_app')
            resolver.assert_not_called(); launcher.assert_not_called()

    def test_missing_install_fixed_result(self):
        resolver = Mock(side_effect=paths.PathError('app_not_installed'))
        self.assertCode(apps.launch_app('vscode', resolver=resolver, launcher=Mock()), 'app_not_installed')

    def test_resolver_uses_only_fixed_install_locations(self):
        installations = {
            'notepad': self.base / 'notepad.exe',
            'calculator': self.base / 'calc.exe',
            'explorer': self.base / 'explorer.exe',
            'edge': self.base / 'Microsoft/Edge/Application/msedge.exe',
            'vscode': self.base / 'Programs/Microsoft VS Code/Code.exe',
        }
        for target in installations.values():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'not executable test data')
        with patch.object(apps.win32api, 'GetSystemDirectory', return_value=str(self.base)), \
                patch.object(apps.win32api, 'GetWindowsDirectory', return_value=str(self.base)), \
                patch.object(paths, 'known_folder', return_value=self.base):
            for alias, target in installations.items():
                self.assertEqual(target, apps.resolve_app(alias))

    def test_resolver_missing_install_does_not_search_path(self):
        with patch.object(paths, 'known_folder', return_value=self.base):
            with self.assertRaises(paths.PathError) as raised:
                apps.resolve_app('vscode')
        self.assertEqual('app_not_installed', raised.exception.code)

    def test_launcher_receives_argv_without_shell(self):
        with patch.object(apps.subprocess, 'Popen') as process:
            apps._launch(Path('C:/trusted/app.exe'))
        self.assertEqual(['C:\\trusted\\app.exe'], process.call_args.args[0])
        self.assertIs(False, process.call_args.kwargs['shell'])
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(apps.subprocess.DEVNULL, process.call_args.kwargs[stream])

    def test_association_api_not_command_string(self):
        with patch.object(apps.shell, 'ShellExecuteEx') as execute:
            apps._open_document(Path('C:/safe/a.txt'))
        self.assertEqual('open', execute.call_args.kwargs['lpVerb'])
        self.assertEqual('C:\\safe\\a.txt', execute.call_args.kwargs['lpFile'])
        self.assertNotIn('lpParameters', execute.call_args.kwargs)

    def test_all_four_public_wrappers_use_default_policy_without_injected_public_roots(self):
        target = self.file('safe.txt')
        with patch.object(paths, 'PathPolicy', return_value=self.policy):
            self.assertCode(files.inspect_path(dict(operation='stat', path='Downloads/safe.txt')), 'ok')
            self.assertCode(files.manage_path(dict(operation='mkdir', path='Documents/HCI')), 'created')
        with patch.object(apps, 'open_local', return_value={'status':'ok'}) as open_local:
            self.assertEqual({'status':'ok'}, apps.open_path(str(target)))
            open_local.assert_called_once_with(str(target))
        with patch.object(apps, 'launch_app', return_value={'status':'ok'}) as launch:
            self.assertEqual({'status':'ok'}, apps.open_app('vscode'))
            launch.assert_called_once_with('vscode')


if __name__ == '__main__':
    unittest.main()
