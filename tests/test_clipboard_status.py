"""No real clipboard/window/disk access: native dependencies are injected or mocked."""

from contextlib import contextmanager
import ctypes
import io
import logging
from unittest.mock import Mock, patch
import unittest

from fastmcp import Client
import windows_gui_mcp
from windows_gui import clipboard as cb
from windows_gui import system_status as ss


class FakeClipboard:
    def __init__(self, text='fixture'):
        self.text = text
        self.sessions = []
        self.writes = []

    @contextmanager
    def session(self, *, write):
        self.sessions.append(write)
        yield

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.writes.append(text)


class ClipboardTests(unittest.TestCase):
    def test_explicit_read_returns_text_and_truncation(self):
        fake = FakeClipboard('hello\n\u4e16\u754c')
        result = cb.exchange({'operation': 'read', 'max_chars': 5}, backend=fake)
        self.assertEqual('hello', result['text'])
        self.assertTrue(result['truncated'])
        self.assertEqual([False], fake.sessions)
        self.assertEqual([], fake.writes)

    def test_write_returns_counts_not_content(self):
        fake = FakeClipboard()
        result = cb.exchange({'operation': 'write', 'text': 'SECRET'}, backend=fake)
        self.assertEqual(['SECRET'], fake.writes)
        self.assertEqual([True], fake.sessions)
        self.assertEqual(6, result['characters'])
        self.assertTrue(result['history_excluded'])
        self.assertTrue(result['cloud_sync_excluded'])
        self.assertNotIn('SECRET', str(result))

    def test_invalid_fields_and_types_before_access(self):
        for request in [
            {'operation': 'read', 'text': 'SECRET'}, {'operation': 'write'},
            {'operation': 'read', 'max_chars': 0}, {'operation': 'read', 'max_chars': 64001},
            {'operation': 'write', 'text': 42}, {'operation': 'clear'},
            {'operation': 'write', 'text': 'x' * 64001},
        ]:
            fake = FakeClipboard()
            self.assertEqual({'status': 'error', 'code': 'invalid_request'}, cb.exchange(request, backend=fake))
            self.assertEqual([], fake.sessions)

    def test_nul_surrogate_and_size_rejected_before_access(self):
        for text, code in [('a\0b', 'invalid_text'), ('\ud800', 'invalid_request')]:
            fake = FakeClipboard()
            self.assertEqual(code, cb.exchange({'operation': 'write', 'text': text}, backend=fake)['code'])
            self.assertEqual([], fake.sessions)
        with patch.object(cb, 'MAX_BYTES', 4):
            self.assertEqual('clipboard_too_large', cb.exchange({'operation': 'write', 'text': 'abc'}, backend=FakeClipboard())['code'])

    def test_empty_write_is_explicitly_supported(self):
        fake = FakeClipboard()
        self.assertEqual('written', cb.exchange({'operation': 'write', 'text': ''}, backend=fake)['code'])
        self.assertEqual([''], fake.writes)

    def test_failure_and_success_do_not_log_content(self):
        output = io.StringIO(); handler = logging.StreamHandler(output)
        logger = logging.getLogger(); logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        fake = FakeClipboard()
        fake.write_text = Mock(side_effect=RuntimeError('SECRET payload'))
        result = cb.exchange({'operation': 'write', 'text': 'SECRET'}, backend=fake)
        self.assertEqual({'status': 'error', 'code': 'clipboard_unavailable'}, result)
        self.assertEqual('', output.getvalue())

    def test_native_read_checks_size_before_memory_copy(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.available = Mock(return_value=True); native.get = Mock(return_value=1)
        native.size = Mock(return_value=cb.MAX_BYTES + 1); native.lock = Mock()
        with self.assertRaises(cb.ClipboardError) as raised:
            native.read_text()
        self.assertEqual('clipboard_too_large', raised.exception.code)
        native.lock.assert_not_called()

    def test_native_unicode_read_and_invalid_termination(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.available = Mock(return_value=True); native.get = Mock(return_value=1)
        native.unlock = Mock()
        for raw, expected in [('hello\U0001f4da'.encode('utf-16-le') + b'\0\0', 'hello\U0001f4da'),
                              (b'a\0b\0', None), (b'\x00\xd8\0\0', None)]:
            memory = ctypes.create_string_buffer(raw)
            native.size = Mock(return_value=len(raw)); native.lock = Mock(return_value=ctypes.addressof(memory))
            if expected is None:
                with self.assertRaises(cb.ClipboardError):
                    native.read_text()
            else:
                self.assertEqual(expected, native.read_text())

    def test_native_unsupported_format(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.available = Mock(return_value=False)
        with self.assertRaises(cb.ClipboardError) as raised:
            native.read_text()
        self.assertEqual('text_not_available', raised.exception.code)

    def native_writer(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        memory = []
        def allocate(flags, size):
            memory.append(ctypes.create_string_buffer(size))
            return len(memory)
        native.register = Mock(side_effect=[101, 102, 103])
        native.allocate = Mock(side_effect=allocate)
        native.lock = Mock(side_effect=lambda handle: ctypes.addressof(memory[handle - 1]))
        native.unlock = Mock(); native.free = Mock(); native.empty = Mock(return_value=True)
        native.set = Mock(side_effect=lambda fmt, handle: handle)
        return native, memory

    def test_native_history_exclusions_precede_text_and_transfer_ownership(self):
        native, memory = self.native_writer()
        native.write_text('secret')
        self.assertEqual(list(cb.EXCLUSION_FORMATS), [x.args[0] for x in native.register.call_args_list])
        self.assertEqual([101, 102, 103, 13], [x.args[0] for x in native.set.call_args_list])
        for value in memory[:3]:
            self.assertEqual(b'\0' * 4, value.raw)
        native.free.assert_not_called()

    def test_privacy_marker_failure_never_publishes_text(self):
        native, _ = self.native_writer()
        native.set.side_effect = lambda fmt, handle: 0 if fmt == 102 else handle
        with self.assertRaises(cb.ClipboardError):
            native.write_text('SECRET')
        self.assertNotIn(13, [x.args[0] for x in native.set.call_args_list])
        self.assertEqual([2, 3, 4], [x.args[0] for x in native.free.call_args_list])

    def test_allocation_failure_does_not_clear_existing_clipboard(self):
        native, _ = self.native_writer(); native.allocate.side_effect = [0]
        with self.assertRaises(cb.ClipboardError):
            native.write_text('SECRET')
        native.empty.assert_not_called(); native.set.assert_not_called()

    def test_registration_failure_does_not_clear_existing_clipboard(self):
        native, _ = self.native_writer(); native.register.side_effect = [0, 102, 103]
        with self.assertRaises(cb.ClipboardError):
            native.write_text('SECRET')
        native.empty.assert_not_called()

    def test_busy_clipboard_retries_bounded_and_destroys_hidden_owner(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.open = Mock(return_value=False); native.close = Mock()
        with patch.object(cb.win32gui, 'CreateWindowEx', return_value=7), \
                patch.object(cb.win32gui, 'DestroyWindow') as destroy, \
                patch.object(cb.time, 'sleep') as sleep:
            with self.assertRaises(cb.ClipboardError) as raised:
                with native.session(write=True):
                    self.fail('must not open')
        self.assertEqual('clipboard_busy', raised.exception.code)
        self.assertEqual(4, native.open.call_count); self.assertEqual(3, sleep.call_count)
        native.close.assert_not_called(); destroy.assert_called_once_with(7)

    def test_read_never_creates_window_and_closes_on_error(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.open = Mock(return_value=True); native.close = Mock()
        with patch.object(cb.win32gui, 'CreateWindowEx') as create:
            with self.assertRaises(RuntimeError):
                with native.session(write=False):
                    raise RuntimeError()
        create.assert_not_called(); native.open.assert_called_once_with(0); native.close.assert_called_once()

    def test_close_failure_is_not_success_and_owner_is_destroyed(self):
        native = cb.NativeClipboard.__new__(cb.NativeClipboard)
        native.open = Mock(return_value=True); native.close = Mock(return_value=False)
        with patch.object(cb.win32gui, 'CreateWindowEx', return_value=7), \
                patch.object(cb.win32gui, 'DestroyWindow') as destroy:
            with self.assertRaises(cb.ClipboardError) as raised:
                with native.session(write=True):
                    pass
        self.assertEqual('clipboard_close_failed', raised.exception.code)
        destroy.assert_called_once_with(7)


class StatusTests(unittest.TestCase):
    def test_battery_absent_unknown_and_charging(self):
        self.assertEqual('not_present', ss.power_result(1, 128, 255)['state'])
        self.assertIsNone(ss.power_result(1, 128, 255)['percent'])
        self.assertEqual('unknown', ss.power_result(255, 255, 255)['status'])
        self.assertIsNone(ss.power_result(255, 0, 255)['percent'])
        self.assertTrue(ss.power_result(1, 8, 40)['charging'])
        self.assertEqual(40, ss.power_result(1, 8, 40)['percent'])

    def test_partial_results_preserve_success_without_raw_errors(self):
        fake = Mock()
        fake.foreground.return_value = {'status': 'ok', 'title': 'fixture'}
        fake.battery.side_effect = OSError('PRIVATE')
        fake.disks.return_value = fake.screen.return_value = fake.mouse.return_value = {'status': 'ok'}
        result = ss.collect_status(backend=fake)
        self.assertEqual('partial', result['status'])
        self.assertEqual({'status': 'unknown', 'code': 'query_failed'}, result['battery'])
        self.assertEqual('fixture', result['foreground_window']['title'])
        self.assertNotIn('PRIVATE', str(result))

    def test_foreground_title_is_bounded_no_focus(self):
        with patch.object(ss.win32gui, 'GetForegroundWindow', return_value=1), \
                patch.object(ss.win32gui, 'GetWindowText', return_value='x' * 300), \
                patch.object(ss.win32gui, 'SetForegroundWindow') as focus:
            result = ss.NativeStatus().foreground()
        self.assertEqual(256, len(result['title'])); self.assertTrue(result['title_truncated'])
        focus.assert_not_called()

    def test_no_foreground_returns_unknown(self):
        with patch.object(ss.win32gui, 'GetForegroundWindow', return_value=0):
            self.assertEqual('unknown', ss.NativeStatus().foreground()['status'])

    def test_disks_query_only_fixed_local_drives_and_keep_partial_results(self):
        with patch.object(ss.win32api, 'GetLogicalDriveStrings', return_value='C:\\\0D:\\\0Z:\\\0'), \
                patch.object(ss.win32file, 'GetDriveType', side_effect=[3, 3, 4]), \
                patch.object(ss.win32api, 'GetDiskFreeSpaceEx', side_effect=[(10, 100, 20), OSError('PRIVATE')]) as free:
            result = ss.NativeStatus().disks()
        self.assertEqual(2, free.call_count)
        self.assertEqual('C:', result['volumes'][0]['drive'])
        self.assertEqual(10, result['volumes'][0]['free_bytes'])
        self.assertEqual('unknown', result['volumes'][1]['status'])
        self.assertNotIn('PRIVATE', str(result))

    def test_screen_and_mouse_are_read_only(self):
        values = {0:1920, 1:1080, 76:-1920, 77:0, 78:3840, 79:1080}
        with patch.object(ss.win32api, 'GetSystemMetrics', side_effect=values.get), \
                patch.object(ss.win32api, 'GetCursorPos', return_value=(-20, 30)):
            self.assertEqual(1920, ss.NativeStatus().screen()['primary']['width'])
            self.assertEqual(-1920, ss.NativeStatus().screen()['virtual']['left'])
            self.assertEqual({'status':'ok', 'x':-20, 'y':30}, ss.NativeStatus().mouse())


class McpStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_tools_real_schema_and_fixed_error_without_access(self):
        async with Client(windows_gui_mcp.mcp) as client:
            tools = await client.list_tools()
            self.assertEqual(42, len(tools))
            with patch.object(cb, 'NativeClipboard') as native:
                result = await client.call_tool('clipboard', {'request': {'operation':'read','text':'SECRET'}})
                self.assertEqual({'status':'error','code':'invalid_request'}, result.data)
                native.assert_not_called()
                self.assertNotIn('SECRET', str(result.content))
            with patch.object(ss, 'collect_status', return_value={'status':'ok','code':'ok'}) as collect:
                result = await client.call_tool('get_system_status', {})
                self.assertEqual('ok', result.data['status'])
                collect.assert_called_once_with()
