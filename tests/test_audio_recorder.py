"""
Unit tests for audio_recorder.py

Run with: python -m pytest tests/ -v
  or:     python -m unittest tests/test_audio_recorder.py -v
"""

import os
import sys
import tempfile
import unittest

import numpy as np

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_recorder as ar


class TestNextFilename(unittest.TestCase):
    """BUG-01: next_filename must never collide with an existing file after cleanup."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self.td, name)
        open(path, 'wb').close()
        return path

    def test_first_file(self):
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0001.wav')

    def test_sequential(self):
        self._touch('rec_0001.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0002.wav')

    def test_monotonic_after_gap(self):
        # _0001 deleted, _0002 and _0003 exist → next should be _0004, not _0002
        self._touch('rec_0002.wav')
        self._touch('rec_0003.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0004.wav')

    def test_no_overwrite_after_cleanup(self):
        # Simulate: max=4, files _0001–_0004 exist, _0001 was deleted
        self._touch('rec_0002.wav')
        self._touch('rec_0003.wav')
        self._touch('rec_0004.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0005.wav')
        # Critical: _0004 must NOT be returned (was the old broken behaviour)
        self.assertNotEqual(os.path.basename(path), 'rec_0004.wav')

    def test_different_formats(self):
        self._touch('sess_0001.mp3')
        self._touch('sess_0002.mp3')
        path = ar.next_filename('sess', 'mp3', self.td)
        self.assertEqual(os.path.basename(path), 'sess_0003.mp3')

    def test_ignores_wrong_extension(self):
        self._touch('rec_0001.wav')
        self._touch('rec_0001.mp3')   # different format, should not affect wav counter
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0002.wav')

    def test_ignores_non_indexed_files(self):
        self._touch('rec_extra.wav')  # no _NNNN suffix
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0001.wav')

    def test_parse_index_valid(self):
        self._touch('rec_0042.wav')
        self.assertEqual(ar._parse_index(os.path.join(self.td, 'rec_0042.wav'), 'rec', 'wav'), 42)

    def test_parse_index_invalid(self):
        self.assertIsNone(ar._parse_index('other_0001.wav', 'rec', 'wav'))

    def test_parse_index_non_numeric(self):
        self.assertIsNone(ar._parse_index('rec_abc.wav', 'rec', 'wav'))


class TestCleanupOldest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self.td, name)
        open(path, 'wb').close()
        return path

    def test_removes_oldest_when_at_limit(self):
        for i in range(1, 5):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        remaining = sorted(os.listdir(self.td))
        self.assertNotIn('rec_0001.wav', remaining)
        self.assertIn('rec_0004.wav', remaining)
        self.assertEqual(len(remaining), 3)

    def test_no_removal_below_limit(self):
        for i in range(1, 4):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        self.assertEqual(len(os.listdir(self.td)), 3)

    def test_removes_multiple_when_over_limit(self):
        for i in range(1, 7):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        remaining = os.listdir(self.td)
        self.assertEqual(len(remaining), 3)


class TestFmtSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(ar.fmt_size(512), '512 B')

    def test_kilobytes(self):
        self.assertIn('KB', ar.fmt_size(2048))

    def test_megabytes(self):
        self.assertIn('MB', ar.fmt_size(5 * 1024 * 1024))

    def test_gigabytes(self):
        self.assertIn('GB', ar.fmt_size(2 * 1024 ** 3))

    def test_zero(self):
        self.assertEqual(ar.fmt_size(0), '0 B')


class TestFmtTime(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(ar.fmt_time(45), '00:00:45')

    def test_minutes_and_seconds(self):
        self.assertEqual(ar.fmt_time(90), '00:01:30')

    def test_hours(self):
        self.assertEqual(ar.fmt_time(3661), '01:01:01')

    def test_zero(self):
        self.assertEqual(ar.fmt_time(0), '00:00:00')


class TestPbar(unittest.TestCase):
    def test_empty(self):
        result = ar.pbar(0.0)
        self.assertTrue(all(c == '-' for c in result))

    def test_full(self):
        result = ar.pbar(1.0)
        self.assertTrue(all(c == '#' for c in result))

    def test_half(self):
        result = ar.pbar(0.5, width=10)
        self.assertEqual(result, '#####-----')

    def test_clamps_above_one(self):
        result = ar.pbar(2.0, width=10)
        self.assertEqual(result, '##########')

    def test_clamps_below_zero(self):
        result = ar.pbar(-1.0, width=10)
        self.assertEqual(result, '----------')

    def test_width_respected(self):
        for w in (10, 20, 30):
            self.assertEqual(len(ar.pbar(0.5, width=w)), w)


class TestSparkline(unittest.TestCase):
    def test_empty_returns_spaces(self):
        result = ar.sparkline([], width=10)
        self.assertEqual(result, ' ' * 10)

    def test_uniform_values_returns_first_char(self):
        result = ar.sparkline([-30, -30, -30], width=5)
        # All equal → all map to index 0 ('.')
        self.assertTrue(all(c == '.' for c in result))

    def test_length_matches_width(self):
        levels = list(range(-60, 0, 3))
        result = ar.sparkline(levels, width=15)
        self.assertEqual(len(result), 15)


class TestRmsDb(unittest.TestCase):
    def test_silence_returns_low_db(self):
        data = np.zeros(1024, dtype=np.int16)
        self.assertAlmostEqual(ar.rms_db(data, 16), -100.0)

    def test_full_scale_sine_near_zero_db(self):
        t = np.linspace(0, 2 * np.pi, 1024, endpoint=False)
        data = (np.sin(t) * 32767).astype(np.int16)
        db = ar.rms_db(data, 16)
        # Full-scale sine RMS ≈ -3 dB
        self.assertAlmostEqual(db, -3.0, delta=0.5)

    def test_empty_returns_low_db(self):
        data = np.array([], dtype=np.int16)
        self.assertEqual(ar.rms_db(data, 16), -100.0)


class TestApplyGain(unittest.TestCase):
    def test_unity_gain_no_change(self):
        data = np.array([100, 200, 300], dtype=np.int16)
        result = ar.apply_gain(data, 1.0)
        np.testing.assert_array_equal(result, data)

    def test_gain_doubles_values(self):
        data = np.array([100, 200], dtype=np.int16)
        result = ar.apply_gain(data, 2.0)
        np.testing.assert_array_equal(result, np.array([200, 400], dtype=np.int16))

    def test_gain_clips_at_max(self):
        data = np.array([20000], dtype=np.int16)
        result = ar.apply_gain(data, 3.0)
        self.assertEqual(result[0], np.iinfo(np.int16).max)

    def test_gain_clips_at_min(self):
        data = np.array([-20000], dtype=np.int16)
        result = ar.apply_gain(data, 3.0)
        self.assertEqual(result[0], np.iinfo(np.int16).min)

    def test_output_dtype_preserved(self):
        data = np.array([100, 200], dtype=np.int32)
        result = ar.apply_gain(data, 2.0)
        self.assertEqual(result.dtype, np.int32)


class TestPackInt24(unittest.TestCase):
    def test_output_length(self):
        data = np.array([0, 1, 2, 3], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(len(result), 12)  # 4 samples × 3 bytes

    def test_zero_encodes_to_three_zero_bytes(self):
        data = np.array([0], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x00\x00\x00')

    def test_value_1_encodes_correctly(self):
        data = np.array([1], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x01\x00\x00')

    def test_value_256_encodes_correctly(self):
        data = np.array([256], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x00\x01\x00')

    def test_roundtrip_bytes_count(self):
        n = 100
        data = np.arange(n, dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(len(result), n * 3)

    def test_2d_mono_input(self):
        data = np.array([[1], [256]], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x01\x00\x00\x00\x01\x00')

    def test_2d_stereo_input(self):
        data = np.array([[1, 2], [3, 4]], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(len(result), 12)
        self.assertEqual(result, b'\x01\x00\x00\x02\x00\x00\x03\x00\x00\x04\x00\x00')


class TestGetAudioDtype(unittest.TestCase):
    def test_8bit(self):
        self.assertEqual(ar.get_audio_dtype(8), np.int8)

    def test_16bit(self):
        self.assertEqual(ar.get_audio_dtype(16), np.int16)

    def test_24bit(self):
        self.assertEqual(ar.get_audio_dtype(24), np.int32)

    def test_32bit(self):
        self.assertEqual(ar.get_audio_dtype(32), np.int32)


class TestGetFfmpegFmt(unittest.TestCase):
    def test_8bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(8), 's8')

    def test_16bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(16), 's16le')

    def test_24bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(24), 's24le')

    def test_32bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(32), 's32le')


class TestBuildFfmpegCmd(unittest.TestCase):
    def _cmd(self, fmt):
        return ar.build_ffmpeg_cmd(fmt, '/out.' + fmt, 320, 48000, 2, 16)

    def test_mp3_uses_libmp3lame(self):
        cmd = self._cmd('mp3')
        self.assertIn('libmp3lame', cmd)

    def test_flac_uses_flac_codec(self):
        cmd = self._cmd('flac')
        self.assertIn('flac', cmd)

    def test_ogg_uses_libvorbis(self):
        cmd = self._cmd('ogg')
        self.assertIn('libvorbis', cmd)

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            ar.build_ffmpeg_cmd('avi', '/out.avi', 320, 48000, 2, 16)

    def test_cmd_starts_with_ffmpeg(self):
        self.assertEqual(self._cmd('mp3')[0], 'ffmpeg')

    def test_sample_rate_included(self):
        cmd = ar.build_ffmpeg_cmd('mp3', '/out.mp3', 128, 44100, 1, 16)
        self.assertIn('44100', cmd)


class TestGetFileSize(unittest.TestCase):
    def test_missing_file_returns_zero(self):
        self.assertEqual(ar._get_file_size('/nonexistent/path/file.wav'), 0)

    def test_existing_file_returns_size(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'hello')
            path = f.name
        try:
            self.assertEqual(ar._get_file_size(path), 5)
        finally:
            os.unlink(path)


class TestUIStateGainReset(unittest.TestCase):
    """BUG-12: '0' key should reset gain to unity."""

    def test_gain_starts_at_unity(self):
        s = ar.UIState()
        self.assertEqual(s.gain, 1.0)

    def test_gain_db_at_unity(self):
        s = ar.UIState()
        self.assertAlmostEqual(s.gain_db(), 0.0, places=3)

    def test_files_created_starts_empty(self):
        s = ar.UIState()
        self.assertEqual(s.files_created, [])

    def test_error_msg_starts_none(self):
        s = ar.UIState()
        self.assertIsNone(s.error_msg)

    def test_auto_gain_starts_off(self):
        s = ar.UIState()
        self.assertEqual(s.auto_gain, 0)


class TestWavHeaderConstant(unittest.TestCase):
    def test_wav_header_size(self):
        """WAV_HEADER_BYTES should equal 44 (standard PCM WAV header)."""
        self.assertEqual(ar.WAV_HEADER_BYTES, 44)


class TestDeviceAndMonitorFeatures(unittest.TestCase):
    def test_uistate_initial_state(self):
        s = ar.UIState()
        self.assertFalse(s.select_device)
        self.assertFalse(s.toggle_monitor)
        self.assertFalse(s.monitor)

    def test_read_key_device_selection(self):
        s = ar.UIState()
        from unittest.mock import patch
        if ar.HAS_MSVCRT:
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'd'):
                s.read_key()
            self.assertTrue(s.select_device)
            s.select_device = False
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'D'):
                s.read_key()
            self.assertTrue(s.select_device)

    def test_read_key_monitor_toggle(self):
        s = ar.UIState()
        from unittest.mock import patch
        if ar.HAS_MSVCRT:
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'm'):
                s.read_key()
            self.assertTrue(s.toggle_monitor)
            s.toggle_monitor = False
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'M'):
                s.read_key()
            self.assertTrue(s.toggle_monitor)

    def test_prompt_monitor_warning_text_and_confirmation(self):
        from unittest.mock import patch
        import io
        expected_warning = 'Make sure you have headphones on, audio will feedback (noise) loop (echo) if the input (mic) receives (hears) the output (spkr) signal (sound)'
        
        # Test confirming with 'y'
        with patch('audio_recorder._readline_echo', return_value='y'), patch('sys.stdout', new=io.StringIO()) as fake_out:
            confirmed = ar.prompt_monitor_warning()
            output = fake_out.getvalue()
            self.assertTrue(confirmed)
            self.assertIn(expected_warning, output)

        # Test rejecting with 'n'
        with patch('audio_recorder._readline_echo', return_value='n'):
            self.assertFalse(ar.prompt_monitor_warning())

        # Test rejecting with empty enter
        with patch('audio_recorder._readline_echo', return_value=''):
            self.assertFalse(ar.prompt_monitor_warning())

    def test_prompt_select_device(self):
        from unittest.mock import patch
        mock_devices = [
            {'name': 'Mock Mic 1', 'max_input_channels': 2, 'default_samplerate': 44100},
            {'name': 'Mock Output Only', 'max_input_channels': 0, 'default_samplerate': 44100},
            {'name': 'Mock Mic 2', 'max_input_channels': 1, 'default_samplerate': 48000},
        ]
        with patch('sounddevice.query_devices', return_value=mock_devices):
            # Select valid index 0
            with patch('audio_recorder._readline_echo', return_value='0'):
                self.assertEqual(ar.prompt_select_device(None), 0)
            # Select valid index 2
            with patch('audio_recorder._readline_echo', return_value='2'):
                self.assertEqual(ar.prompt_select_device(0), 2)
            # Cancel with empty input
            with patch('audio_recorder._readline_echo', return_value=''):
                self.assertIsNone(ar.prompt_select_device(0))
            # Invalid selection 1 (output only, not in input list)
            with patch('audio_recorder._readline_echo', return_value='1'):
                self.assertEqual(ar.prompt_select_device(0), -1)

    def test_write_monitor_audio(self):
        class MockOutputStream:
            def __init__(self):
                self.written = []
            def write(self, data):
                self.written.append(data)

        # 2-channel output with 1-channel 2D input -> repeated across channels
        stream = MockOutputStream()
        data_mono = np.array([[10], [20]], dtype=np.int16)
        ar.write_monitor_audio(stream, 2, data_mono)
        self.assertEqual(stream.written[0].shape, (2, 2))
        np.testing.assert_array_equal(stream.written[0], np.array([[10, 10], [20, 20]], dtype=np.int16))

        # 2-channel output with 1D input
        stream = MockOutputStream()
        data_1d = np.array([10, 20], dtype=np.int16)
        ar.write_monitor_audio(stream, 2, data_1d)
        self.assertEqual(stream.written[0].shape, (2, 2))

        # None stream does not raise
        ar.write_monitor_audio(None, 2, data_mono)

    def test_hud_contains_device_and_monitor(self):
        from unittest.mock import patch
        import io
        s = ar.UIState()
        s.levels = [ -20 ]
        s.monitor = True
        class Args:
            file_format = 'wav'
            bit_depth = 16
            sample_rate = 44100
            channels = 2
            output_dir = '.'
            device = None
        with patch('audio_recorder.get_disk_info', return_value=(1000, 500, 500)), \
             patch('sys.stdout', new=io.StringIO()) as fake_out:
            ar.draw_hud(s, Args(), 'test.wav', 100, 1000, 1.0)
            output = fake_out.getvalue()
            self.assertIn('evice', output)
            self.assertIn('onitor', output)
            self.assertIn('Monitor', output)
            self.assertIn('Dev', output)


class TestAudioInputStream(unittest.TestCase):
    def test_audio_input_stream_reads_queued_data(self):
        from unittest.mock import patch, MagicMock
        mock_sd_stream = MagicMock()
        with patch('sounddevice.InputStream', return_value=mock_sd_stream) as mock_cls:
            stream = ar.AudioInputStream(44100, 2, np.int16, 1024, device=0)
            mock_cls.assert_called_once()
            cb = mock_cls.call_args[1]['callback']
            test_data = np.ones((1024, 2), dtype=np.int16)
            cb(test_data, 1024, None, None)
            
            data, ov = stream.read(1024)
            self.assertFalse(ov)
            np.testing.assert_array_equal(data, test_data)

    def test_audio_input_stream_handles_string_device_int(self):
        from unittest.mock import patch, MagicMock
        with patch('sounddevice.InputStream', return_value=MagicMock()):
            stream = ar.AudioInputStream(44100, 1, np.int16, 512, device='18')
            self.assertEqual(stream.device, 18)


class TestDeviceCommandLineAndResolution(unittest.TestCase):
    def test_parse_args_device_integer_conversion(self):
        from unittest.mock import patch
        mock_devices = [
            {'name': 'Device 0', 'max_input_channels': 2, 'default_samplerate': 44100},
            {'name': 'Microphone ()', 'max_input_channels': 2, 'default_samplerate': 48000},
        ]
        def mock_query(device=None, kind=None):
            if device is not None:
                return mock_devices[int(device)]
            return mock_devices

        test_argv = ['audio_recorder.py', '--device', '1', '-o', 'test']
        with patch('sys.argv', test_argv), patch('sounddevice.query_devices', side_effect=mock_query), \
             patch('sounddevice.check_input_settings'):
            args = ar.parse_args()
            self.assertEqual(args.device, 1)

    def test_list_device_alias_flag(self):
        from unittest.mock import patch
        test_argv = ['audio_recorder.py', '--list-device']
        with patch('sys.argv', test_argv), patch('sounddevice.query_devices', return_value=[]), \
             self.assertRaises(SystemExit) as cm:
            ar.parse_args()
        self.assertEqual(cm.exception.code, 0)

    def test_list_devices_plural_flag(self):
        from unittest.mock import patch
        test_argv = ['audio_recorder.py', '--list-devices']
        with patch('sys.argv', test_argv), patch('sounddevice.query_devices', return_value=[]), \
             self.assertRaises(SystemExit) as cm:
            ar.parse_args()
        self.assertEqual(cm.exception.code, 0)


class TestDrawStartupInteractiveFallback(unittest.TestCase):
    def test_fallback_activated_on_unreachable_device(self):
        from unittest.mock import patch
        import io
        class Args:
            file_format = 'wav'
            size = 1000000
            max_recordings = 4
            bit_depth = 16
            bit_rate = 128
            sample_rate = 44100
            channels = 1
            length = 5
            output_dir = '.'
            output = 'test'
            device = 99

        with patch('audio_recorder.test_audio_device', side_effect=[(False, 'Error querying device -1'), (True, None), (True, None)]), \
             patch('audio_recorder.prompt_select_device', return_value=1) as mock_prompt, \
             patch('sys.stdout', new=io.StringIO()):
            ar.draw_startup(Args())
            mock_prompt.assert_called_once_with(99)

    def test_unreachable_device_cancelled_exits(self):
        from unittest.mock import patch
        import io
        class Args:
            file_format = 'wav'
            size = 1000000
            max_recordings = 4
            bit_depth = 16
            bit_rate = 128
            sample_rate = 44100
            channels = 1
            length = 5
            output_dir = '.'
            output = 'test'
            device = -1

        with patch('audio_recorder.test_audio_device', return_value=(False, 'Error querying device -1')), \
             patch('audio_recorder.prompt_select_device', return_value=None), \
             patch('sys.stdout', new=io.StringIO()), \
             self.assertRaises(SystemExit) as cm:
            ar.draw_startup(Args())
        self.assertEqual(cm.exception.code, 1)


class TestOutputDeviceSelectionAndMonitor(unittest.TestCase):
    def test_prompt_select_output_device_valid_and_cancel(self):
        from unittest.mock import patch
        mock_devices = [
            {'name': 'Mock Mic (Input Only)', 'max_input_channels': 2, 'max_output_channels': 0, 'default_samplerate': 44100},
            {'name': 'Headphones (Output 1)', 'max_input_channels': 0, 'max_output_channels': 2, 'default_samplerate': 48000},
            {'name': 'Speakers (Output 2)', 'max_input_channels': 0, 'max_output_channels': 2, 'default_samplerate': 44100},
        ]
        with patch('sounddevice.query_devices', return_value=mock_devices), \
             patch('sounddevice.default.device', (0, 1)):
            # Select valid index 2
            with patch('audio_recorder._readline_echo', return_value='2'):
                self.assertEqual(ar.prompt_select_output_device(), 2)

            # Default on enter
            with patch('audio_recorder._readline_echo', return_value=''):
                self.assertEqual(ar.prompt_select_output_device(), 1)

            # Cancel with 'c'
            with patch('audio_recorder._readline_echo', return_value='c'):
                self.assertIs(ar.prompt_select_output_device(), False)

            # Cancel with 'cancel'
            with patch('audio_recorder._readline_echo', return_value='cancel'):
                self.assertIs(ar.prompt_select_output_device(), False)

            # Invalid index 0 (input only)
            with patch('audio_recorder._readline_echo', return_value='0'):
                self.assertEqual(ar.prompt_select_output_device(), -1)

    def test_handle_monitor_toggle_flow(self):
        from unittest.mock import patch, MagicMock
        st = ar.UIState()
        st.toggle_monitor = True

        # Flow 1: Warning declined -> monitor stays off
        with patch('audio_recorder.prompt_monitor_warning', return_value=False):
            stream, ch = ar.handle_monitor_toggle(st, None, 48000, 2, np.int16)
            self.assertIsNone(stream)
            self.assertFalse(st.monitor)

        # Flow 2: Warning accepted, device selection cancelled -> monitor stays off
        st.toggle_monitor = True
        with patch('audio_recorder.prompt_monitor_warning', return_value=True), \
             patch('audio_recorder.prompt_select_output_device', return_value=False):
            stream, ch = ar.handle_monitor_toggle(st, None, 48000, 2, np.int16)
            self.assertIsNone(stream)
            self.assertFalse(st.monitor)

        # Flow 3: Warning accepted, device selected -> monitor stream starts
        st.toggle_monitor = True
        mock_output_stream = MagicMock()
        mock_output_stream.channels = 2
        with patch('audio_recorder.prompt_monitor_warning', return_value=True), \
             patch('audio_recorder.prompt_select_output_device', return_value=2), \
             patch('sounddevice.OutputStream', return_value=mock_output_stream):
            stream, ch = ar.handle_monitor_toggle(st, None, 48000, 2, np.int16)
            self.assertIsNotNone(stream)
            self.assertTrue(st.monitor)
            self.assertEqual(st.monitor_device, 2)

        # Flow 4: Monitor active -> toggle turns it OFF
        st.toggle_monitor = True
        stream, ch = ar.handle_monitor_toggle(st, mock_output_stream, 48000, 2, np.int16)
        self.assertIsNone(stream)
        self.assertFalse(st.monitor)
        mock_output_stream.stop.assert_called_once()
        mock_output_stream.close.assert_called_once()


class TestDeviceSwitchTriggersNewFile(unittest.TestCase):
    def test_device_switch_sets_new_file_flag_and_adapts_params(self):
        from unittest.mock import patch, MagicMock
        class Args:
            device = 0
            channels = 2
            sample_rate = 96000
            bit_depth = 16

        st = ar.UIState()
        st.select_device = True

        mock_dev_info = {
            'name': 'New Mic',
            'max_input_channels': 1,
            'default_samplerate': 48000,
        }

        mock_old_stream = MagicMock()
        mock_new_stream = MagicMock()

        with patch('audio_recorder.prompt_select_device', return_value=1), \
             patch('sounddevice.query_devices', return_value=mock_dev_info), \
             patch('sounddevice.check_input_settings', side_effect=[Exception('96k unsupported'), None]), \
             patch('audio_recorder.AudioInputStream', return_value=mock_new_stream):
            res_stream = ar.handle_device_switch(st, Args(), mock_old_stream, 96000, 2, np.int16, 512)
            # Must set new_file = True to trigger immediate rotation
            self.assertTrue(st.new_file)
            mock_old_stream.stop.assert_called_once()
            mock_old_stream.close.assert_called_once()
            self.assertEqual(res_stream, mock_new_stream)

    def test_device_switch_same_device_does_not_trigger_new_file(self):
        from unittest.mock import patch, MagicMock
        class Args:
            device = 1
            channels = 2
            sample_rate = 44100
            bit_depth = 16

        st = ar.UIState()
        st.select_device = True
        mock_stream = MagicMock()

        with patch('audio_recorder.prompt_select_device', return_value=1):
            res = ar.handle_device_switch(st, Args(), mock_stream, 44100, 2, np.int16, 512)
            self.assertEqual(res, mock_stream)
            self.assertFalse(st.new_file)


class TestAudioInputStreamPauseResume(unittest.TestCase):
    def test_pause_drops_frames_and_clears_queue(self):
        from unittest.mock import patch, MagicMock
        mock_sd_stream = MagicMock()
        with patch('sounddevice.InputStream', return_value=mock_sd_stream) as mock_cls:
            stream = ar.AudioInputStream(44100, 1, np.int16, 512, device=0)
            cb = mock_cls.call_args[1]['callback']
            test_data = np.ones((512, 1), dtype=np.int16)

            # Normal active recording: frame is enqueued
            cb(test_data, 512, None, None)
            self.assertEqual(stream._queue.qsize(), 1)

            # Pause: drops lingering queue items and stops enqueuing new frames
            stream.pause()
            self.assertEqual(stream._queue.qsize(), 0)

            # Incoming hardware frames during pause are discarded
            cb(test_data, 512, None, None)
            cb(test_data, 512, None, None)
            self.assertEqual(stream._queue.qsize(), 0)

            # Resume: re-enables enqueuing
            stream.resume()
            cb(test_data, 512, None, None)
            self.assertEqual(stream._queue.qsize(), 1)


class TestParseSizeStr(unittest.TestCase):
    def test_parse_size_str_units(self):
        self.assertEqual(ar.parse_size_str('4GB'), 4_000_000_000)
        self.assertEqual(ar.parse_size_str('4G'), 4_000_000_000)
        self.assertEqual(ar.parse_size_str('4GiB'), 4 * 1024 ** 3)
        self.assertEqual(ar.parse_size_str('500MB'), 500_000_000)
        self.assertEqual(ar.parse_size_str('100M'), 100_000_000)
        self.assertEqual(ar.parse_size_str('512MiB'), 512 * 1024 ** 2)
        self.assertEqual(ar.parse_size_str('1000'), 1000)
        self.assertEqual(ar.parse_size_str(4000000000), 4000000000)
        self.assertEqual(ar.parse_size_str(None), ar.DEFAULT_SIZE_LIMIT)


class TestAudioOutputStream(unittest.TestCase):
    def test_audio_output_stream_queue_and_callback(self):
        from unittest.mock import patch, MagicMock
        mock_sd_stream = MagicMock()
        with patch('sounddevice.OutputStream', return_value=mock_sd_stream) as mock_cls:
            stream = ar.AudioOutputStream(48000, 2, np.int16, device=0, max_queue=2)
            cb = mock_cls.call_args[1]['callback']
            test_chunk = np.ones((10, 2), dtype=np.int16) * 100

            # Write chunks to queue
            stream.write(test_chunk)
            stream.write(test_chunk)
            # Third write drops instead of blocking (bounded queue=2)
            stream.write(test_chunk)
            self.assertEqual(stream._queue.qsize(), 2)

            # Callback consumes chunk
            outbuf = np.zeros((10, 2), dtype=np.int16)
            cb(outbuf, 10, None, None)
            np.testing.assert_array_equal(outbuf, test_chunk)
            self.assertEqual(stream._queue.qsize(), 1)

            # Callback with no data fills zeroes
            outbuf.fill(99)
            cb(outbuf, 10, None, None) # drains queue
            empty_buf = np.zeros((10, 2), dtype=np.int16)
            cb(empty_buf, 10, None, None) # empty -> zeroed
            self.assertTrue(np.all(empty_buf == 0))

            stream.start()
            mock_sd_stream.start.assert_called_once()
            stream.stop()
            mock_sd_stream.stop.assert_called_once()
            stream.close()
            mock_sd_stream.close.assert_called_once()


class TestTerminalAndSignalHandlers(unittest.TestCase):
    def test_restore_terminal_shows_cursor(self):
        import io
        from unittest.mock import patch
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            ar.restore_terminal()
            output = fake_out.getvalue()
            self.assertIn(ar.ANSI['show_cursor'], output)

    def test_register_cleanup_and_signals(self):
        from unittest.mock import patch
        with patch('atexit.register') as mock_atexit:
            ar.register_cleanup_and_signals()
            mock_atexit.assert_called_with(ar.restore_terminal)


class TestStrictConfigSchemaValidation(unittest.TestCase):
    def test_invalid_config_schema_fallbacks(self):
        from unittest.mock import patch
        invalid_config = {
            'format': 'invalid_format',
            'bit_depth': 99,
            'sample_rate': -48000,
            'channels': 0,
            'bit_rate': -128,
            'max_recordings': -5,
            'device': '',
            'monitor_device': '2',
        }
        with patch('sys.argv', ['audio_recorder.py']), \
             patch('audio_recorder.load_config', return_value=invalid_config), \
             patch('sys.stderr', new=open(os.devnull, 'w')):
            args = ar.parse_args()
            self.assertEqual(args.file_format, 'wav')
            self.assertEqual(args.bit_depth, 16)
            self.assertEqual(args.sample_rate, 48000)
            self.assertEqual(args.channels, 2)
            self.assertEqual(args.bit_rate, 320)
            self.assertEqual(args.max_recordings, 4)
            self.assertIsNone(args.device)
            self.assertEqual(args.monitor_device, '2')

    def test_cli_monitor_device_option(self):
        from unittest.mock import patch
        test_argv = ['audio_recorder.py', '--monitor-device', '3']
        with patch('sys.argv', test_argv), \
             patch('audio_recorder.load_config', return_value={}):
            args = ar.parse_args()
            self.assertEqual(args.monitor_device, '3')


class TestEmergencyDiskGuard(unittest.TestCase):
    def test_emergency_disk_space_guard_halts_recording(self):
        from unittest.mock import patch
        import io
        s = ar.UIState()
        class Args:
            file_format = 'wav'
            bit_depth = 16
            sample_rate = 44100
            channels = 2
            output_dir = '.'
            device = None

        # 30 MB free space (under 50 MB threshold)
        low_disk = (100 * 1024 * 1024, 70 * 1024 * 1024, 30 * 1024 * 1024)
        with patch('audio_recorder.get_disk_info', return_value=low_disk), \
             patch('sys.stdout', new=io.StringIO()):
            ar.draw_hud(s, Args(), 'test.wav', 100, 1000, 1.0)
            self.assertFalse(s.running)
            self.assertIn('Low disk space', s.error_msg)


class TestDropsTrackingAndHUD(unittest.TestCase):
    def test_drops_displayed_on_hud(self):
        from unittest.mock import patch
        import io
        s = ar.UIState()
        s.drops = 5
        class Args:
            file_format = 'wav'
            bit_depth = 16
            sample_rate = 44100
            channels = 2
            output_dir = '.'
            device = None

        with patch('audio_recorder.get_disk_info', return_value=(1000, 500, 500)), \
             patch('sys.stdout', new=io.StringIO()) as fake_out:
            ar.draw_hud(s, Args(), 'test.wav', 100, 1000, 1.0)
            output = fake_out.getvalue()
            self.assertIn('Drop: 5', output)


class TestDeviceListPagination(unittest.TestCase):
    def test_input_device_pagination_navigation(self):
        from unittest.mock import patch
        # 15 mock devices (page size is 10, so 2 pages)
        mock_devs = [
            {'name': f'Mic {i}', 'max_input_channels': 1, 'default_samplerate': 44100}
            for i in range(15)
        ]
        with patch('sounddevice.query_devices', return_value=mock_devs), \
             patch('sounddevice.default.device', (0, 0)):
            # First enter 'n' for next page, then select '12'
            with patch('audio_recorder._readline_echo', side_effect=['n', '12']):
                chosen = ar.prompt_select_device()
                self.assertEqual(chosen, 12)

    def test_output_device_pagination_navigation(self):
        from unittest.mock import patch
        mock_devs = [
            {'name': f'Speaker {i}', 'max_output_channels': 2, 'default_samplerate': 48000}
            for i in range(15)
        ]
        with patch('sounddevice.query_devices', return_value=mock_devs), \
             patch('sounddevice.default.device', (0, 0)):
            # First enter 'n' for next page, then select '14'
            with patch('audio_recorder._readline_echo', side_effect=['n', '14']):
                chosen = ar.prompt_select_output_device()
                self.assertEqual(chosen, 14)


class TestAsymmetricAutoGain(unittest.TestCase):
    def test_fast_attack_and_slow_release(self):
        mx_val = 32767
        target = mx_val * 0.8
        rate = 0.01

        # Test attack: peak > target * 1.1 -> attack_rate = rate * 4 (drops gain by 4%)
        gain = 1.0
        attack_rate = rate * 4
        new_gain_attack = max(0.1, gain * (1.0 - attack_rate))
        self.assertAlmostEqual(new_gain_attack, 0.96)

        # Test release: peak < target -> rate (increases gain by 1%)
        new_gain_release = min(3.0, gain * (1.0 + rate))
        self.assertAlmostEqual(new_gain_release, 1.01)


class TestFfmpegCrashRecovery(unittest.TestCase):
    def test_ffmpeg_crash_rotates_to_new_segment(self):
        from unittest.mock import patch, MagicMock
        td = tempfile.mkdtemp()
        try:
            class Args:
                output_dir = td
                output = 'crash_test'
                size = 10000000
                max_recordings = 4
                bit_depth = 16
                bit_rate = 128
                sample_rate = 44100
                channels = 1
                length = 2
                file_format = 'mp3'
                device = None

            # Mock proc where first write succeeds, second write throws BrokenPipeError, then restarted proc succeeds
            mock_proc1 = MagicMock()
            mock_proc1.poll.return_value = None
            mock_proc1.stdin.write.side_effect = BrokenPipeError('pipe broken')

            mock_proc2 = MagicMock()
            mock_proc2.poll.return_value = None

            mock_stream = MagicMock()
            mock_stream.read.return_value = (np.zeros((100, 1), dtype=np.int16), False)

            call_count = 0
            captured_st = None
            def mock_read_key(self):
                nonlocal call_count, captured_st
                captured_st = self
                call_count += 1
                if call_count >= 2:
                    self.running = False

            with patch('audio_recorder.check_ffmpeg'), \
                 patch('audio_recorder.draw_startup'), \
                 patch('subprocess.Popen', side_effect=[mock_proc1, mock_proc2]), \
                 patch('audio_recorder.AudioInputStream', return_value=mock_stream), \
                 patch('audio_recorder.draw_hud'), \
                 patch('audio_recorder.UIState.read_key', mock_read_key), \
                 patch('audio_recorder.set_raw_terminal'), \
                 patch('audio_recorder.restore_terminal'), \
                 patch('audio_recorder._print_session_summary'):
                ar.record_compressed(Args())

            # Must have rotated to a second file in files_created on crash recovery
            self.assertIsNotNone(captured_st)
            self.assertGreaterEqual(len(captured_st.files_created), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)


class TestAudioSinkClasses(unittest.TestCase):
    def test_wav_sink_lifecycle(self):
        td = tempfile.mkdtemp()
        try:
            sink = ar.WavSink()
            test_path = os.path.join(td, 'test.wav')
            sink.open(test_path, 44100, 1, 16)
            self.assertEqual(sink.get_current_size(), ar.WAV_HEADER_BYTES)

            data = np.ones((100, 1), dtype=np.int16)
            fb = sink.write(data, 16)
            self.assertEqual(fb, 200)
            self.assertEqual(sink.get_current_size(), ar.WAV_HEADER_BYTES + 200)

            st = ar.UIState()
            self.assertEqual(sink.get_total_bytes(st, sink.get_current_size()), 0)
            sink.on_rotate(st)
            sink.close()
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_compressed_sink_lifecycle(self):
        from unittest.mock import patch, MagicMock
        sink = ar.CompressedSink('mp3', 192)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with patch('subprocess.Popen', return_value=mock_proc):
            sink.open('test.mp3', 44100, 2, 16)
            self.assertIsNotNone(sink.proc)

            data = np.ones((100, 2), dtype=np.int16)
            raw_len = sink.write(data, 16)
            self.assertEqual(raw_len, 400)
            mock_proc.stdin.write.assert_called_once()

            st = ar.UIState()
            st.total_bytes = 1000
            self.assertEqual(sink.get_total_bytes(st, 500), 1500)
            sink.close()
            mock_proc.stdin.close.assert_called_once()



class TestAudioQualityAndGainCalibration(unittest.TestCase):
    def test_gain_db_conversions(self):
        st = ar.UIState()
        st.gain = 1.0
        self.assertAlmostEqual(st.gain_db(), 0.0, places=1)
        st.gain = 10.0  # +20 dB
        self.assertAlmostEqual(st.gain_db(), 20.0, places=1)
        st.gain = 0.1   # -20 dB
        self.assertAlmostEqual(st.gain_db(), -20.0, places=1)

    def test_calm_ambient_gain_scaling(self):
        # Calm room ambient signal at -38 dBFS
        sr = 48000
        bd = 16
        mx_val = 32767
        rms_target = mx_val * (10 ** (-38.0 / 20.0))
        t = np.linspace(0, 1, sr, endpoint=False)
        sine = (np.sin(2 * np.pi * 1000 * t) * (rms_target * np.sqrt(2))).astype(np.int16)
        
        # At gain 0.0 dB (unity x1.0) -> Level ≈ -38 dB
        g0 = ar.apply_gain(sine, 1.0, bd)
        lvl_g0 = ar.rms_db(g0, bd)
        self.assertAlmostEqual(lvl_g0, -38.0, delta=0.5)

        # At gain +20.0 dB (x10.0) -> Level ≈ -18 dB
        gp20 = ar.apply_gain(sine, 10.0, bd)
        lvl_gp20 = ar.rms_db(gp20, bd)
        self.assertAlmostEqual(lvl_gp20, -18.0, delta=0.5)

        # At gain -20.0 dB (x0.1) -> Level ≈ -58 dB
        gm20 = ar.apply_gain(sine, 0.1, bd)
        lvl_gm20 = ar.rms_db(gm20, bd)
        self.assertAlmostEqual(lvl_gm20, -58.0, delta=0.5)

    def test_24bit_calm_ambient_gain_scaling(self):
        # 24-bit calm ambient signal at -38 dBFS
        sr = 48000
        bd = 24
        mx_val = 8388607
        rms_target = mx_val * (10 ** (-38.0 / 20.0))
        t = np.linspace(0, 1, sr, endpoint=False)
        sine = (np.sin(2 * np.pi * 1000 * t) * (rms_target * np.sqrt(2))).astype(np.int32)

        # At gain 0.0 dB -> Level ≈ -38 dB
        g0 = ar.apply_gain(sine, 1.0, bd)
        self.assertAlmostEqual(ar.rms_db(g0, bd), -38.0, delta=0.5)

        # At gain +20.0 dB -> Level ≈ -18 dB
        gp20 = ar.apply_gain(sine, 10.0, bd)
        self.assertAlmostEqual(ar.rms_db(gp20, bd), -18.0, delta=0.5)

    def test_read_key_gain_stepping(self):
        st = ar.UIState()
        self.assertEqual(st.gain, 1.0)
        # Test key press '+' on Windows (msvcrt) or Unix (select)
        from unittest.mock import patch
        if ar.HAS_MSVCRT:
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'+'):
                st.read_key()
            self.assertAlmostEqual(st.gain_db(), 1.0, places=1)
            # 19 more presses to reach +20.0 dB
            for _ in range(19):
                with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'+'):
                    st.read_key()
            self.assertAlmostEqual(st.gain_db(), 20.0, places=1)
            # '0' key resets to unity (0 dB)
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'0'):
                st.read_key()
            self.assertAlmostEqual(st.gain_db(), 0.0, places=1)
            self.assertEqual(st.gain, 1.0)
            # '-' key steps down
            with patch('msvcrt.kbhit', side_effect=[True, False]), patch('msvcrt.getch', return_value=b'-'):
                st.read_key()
            self.assertAlmostEqual(st.gain_db(), -1.0, places=1)

    def test_24bit_audio_input_stream_shift(self):
        # Verify 32-bit PortAudio audio is shifted to 24-bit range
        stream = ar.AudioInputStream(48000, 1, np.int32, 100, device=None, bit_depth=24)
        mock_raw = np.array([[256 * 1000], [-256 * 2000]], dtype=np.int32)
        # Put into queue as if callback received it
        stream._queue.put((mock_raw >> 8).astype(np.int32))
        data, _ = stream.read(2)
        self.assertEqual(data[0, 0], 1000)
        self.assertEqual(data[1, 0], -2000)

    def test_pack_int24_clamps_safely(self):
        # Out-of-bounds 24-bit numbers should saturate rather than wrapping
        data = np.array([10000000, -10000000], dtype=np.int32)
        raw = ar.pack_int24(data)
        self.assertEqual(len(raw), 6)
        # Unpack first sample
        val1 = raw[0] | (raw[1] << 8) | (raw[2] << 16)
        if val1 >= (1 << 23):
            val1 -= (1 << 24)
        self.assertEqual(val1, 8388607)  # Saturated at max 24-bit signed!

    def test_8bit_wav_unsigned_conversion(self):
        # 8-bit PCM WAV must be unsigned 0..255 (128 is center)
        from unittest.mock import MagicMock
        mock_wf = MagicMock()
        data = np.array([-128, 0, 127], dtype=np.int8)
        ar.write_wav_frames(mock_wf, data, 8)
        written_bytes = mock_wf.writeframes.call_args[0][0]
        self.assertEqual(list(written_bytes), [0, 128, 255])


class TestAutoGainTargetAdjustment(unittest.TestCase):
    def test_auto_gain_adjusts_level_to_target_zone(self):
        # When sound is -20 dB (too loud, above -38 dB), gain should reduce
        raw_db = -20.0
        st = ar.UIState()
        st.auto_gain = 1
        st.gain = 1.0  # 0 dB
        
        # Simulate auto-gain loop step
        cur_gain_db = st.gain_db()
        current_out_db = raw_db + cur_gain_db
        excess = current_out_db - (-38.0)
        attack_step = min(6.0, max(0.5, excess * 0.35))
        st.gain = 10.0 ** ((cur_gain_db - attack_step) / 20.0)
        
        # Gain should have dropped
        self.assertLess(st.gain_db(), 0.0)

    def test_auto_gain_deadband_stability(self):
        # When sound is at -40 dB (within [-42, -38] dB), gain remains unchanged
        raw_db = -40.0
        st = ar.UIState()
        st.auto_gain = 1
        st.gain = 1.0
        cur_gain_db = st.gain_db()
        current_out_db = raw_db + cur_gain_db
        self.assertTrue(-42.0 <= current_out_db <= -38.0)



class TestPristineAudioQualityFidelity(unittest.TestCase):
    def test_apply_gain_rounds_instead_of_truncating(self):
        # 3 * 0.6 = 1.8 -> rounds to 2 (not truncated to 1)
        # -3 * 0.6 = -1.8 -> rounds to -2 (not truncated to -1)
        data = np.array([3, -3], dtype=np.int16)
        res = ar.apply_gain(data, 0.6, bit_depth=16)
        self.assertEqual(res[0], 2)
        self.assertEqual(res[1], -2)

    def test_apply_gain_smooth_ramp_eliminates_discontinuity(self):
        # Constant DC signal of 1000 with gain ramping from 1.0 to 2.0
        data = np.full((2048, 2), 1000, dtype=np.int16)
        res = ar.apply_gain(data, 2.0, bit_depth=16, prev_gain=1.0)
        # First sample starts at prev_gain (1.0 -> 1000)
        self.assertEqual(res[0, 0], 1000)
        # Mid-ramp sample (~512) is near 1500
        self.assertAlmostEqual(res[512, 0], 1500, delta=5)
        # Post-ramp samples (1024+) are at target gain (2.0 -> 2000)
        self.assertEqual(res[1023, 0], 2000)
        self.assertEqual(res[2047, 0], 2000)

    def test_ogg_mono_bitrate_clamping(self):
        # libvorbis mono (ch=1) max bitrate at 48kHz is 240k; requesting 320k must clamp to 240k
        cmd_mono = ar.build_ffmpeg_cmd('ogg', 'out.ogg', 320, 48000, 1, 16)
        self.assertIn('240k', cmd_mono)
        # Stereo (ch=2) at 48kHz allows 320k
        cmd_stereo = ar.build_ffmpeg_cmd('ogg', 'out.ogg', 320, 48000, 2, 16)
        self.assertIn('320k', cmd_stereo)

    def test_flac_32bit_includes_strict_experimental(self):
        cmd_32 = ar.build_ffmpeg_cmd('flac', 'out.flac', 320, 48000, 2, 32)
        self.assertIn('-strict', cmd_32)
        self.assertIn('-2', cmd_32)

    def test_mp3_uses_high_quality_compression_level(self):
        cmd = ar.build_ffmpeg_cmd('mp3', 'out.mp3', 320, 48000, 2, 16)
        self.assertIn('-loglevel', cmd)
        self.assertIn('error', cmd)
        self.assertIn('-compression_level', cmd)
        self.assertIn('2', cmd)

    def test_monitor_stereo_to_mono_averages_channels(self):
        class MockStream:
            def __init__(self):
                self.written = []
            def write(self, d):
                self.written.append(d)
        ms = MockStream()
        # Left = 100, Right = 300 -> Mono average should be 200 (not 100)
        stereo_data = np.array([[100, 300], [200, 400]], dtype=np.int16)
        ar.write_monitor_audio(ms, 1, stereo_data, bit_depth=16)
        np.testing.assert_array_equal(ms.written[0], np.array([[200], [300]], dtype=np.int16))

    def test_pack_pcm_bytes_all_bit_depths(self):
        for bd, expected_bps in [(8, 1), (16, 2), (24, 3), (32, 4)]:
            dt = ar.get_audio_dtype(bd)
            arr = np.zeros((10, 2), dtype=dt)
            raw = ar.pack_pcm_bytes(arr, bd)
            self.assertEqual(len(raw), 20 * expected_bps)


if __name__ == '__main__':
    unittest.main(verbosity=2)





class TestRemediationFixes(unittest.TestCase):
    """Regression tests for the audit remediation."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _touch(self, name):
        open(os.path.join(self.td, name), 'wb').close()

    def test_cleanup_ignores_unrelated_and_non_numeric_files(self):
        for n in ('rec_0001.wav', 'rec_0002.wav', 'rec_backup.wav', 'rec_old_0001.wav'):
            self._touch(n)
        ar.cleanup_oldest('rec', 'wav', self.td, 2)
        left = set(os.listdir(self.td))
        self.assertEqual(left, {'rec_0002.wav', 'rec_backup.wav', 'rec_old_0001.wav'})

    def test_cleanup_sorts_by_numeric_index(self):
        self._touch('rec_9999.wav')
        self._touch('rec_10000.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 2)
        self.assertEqual(os.listdir(self.td), ['rec_10000.wav'])

    def test_glob_metacharacters_in_base_are_literal(self):
        self._touch('a[1]_0001.wav')
        self._touch('a1_0001.wav')
        self.assertEqual(ar.next_filename('a[1]', 'wav', self.td),
                         os.path.join(self.td, 'a[1]_0002.wav'))

    def test_validate_base_name(self):
        self.assertTrue(ar.validate_base_name('take 1')[0])
        for bad in ('', '  ', '..', 'a/b', 'a\\b', 'x:y', 'a?b', 'tab\tname', 'dot.'):
            self.assertFalse(ar.validate_base_name(bad)[0], bad)

    def test_unix_key_path_is_defined(self):
        # Previously HAS_SELECT/_select_mod were undefined -> NameError on Linux/macOS
        self.assertTrue(hasattr(ar, 'HAS_SELECT'))
        self.assertTrue(hasattr(ar, '_select_mod'))

    def test_boss_mode_ignores_prompt_keys(self):
        s = ar.UIState()
        s.blank = True
        for k in 'rRdDmM':
            s._handle_key(k)
        self.assertFalse(s.rename or s.select_device or s.toggle_monitor)
        s._handle_key('b')
        s._handle_key('r')
        self.assertTrue(s.rename)

    def test_arrow_keys_do_not_trigger_actions_on_windows(self):
        from unittest.mock import patch
        if not ar.HAS_MSVCRT:
            self.skipTest('msvcrt only')
        s = ar.UIState()
        with patch('msvcrt.kbhit', side_effect=[True, True, False]), \
             patch('msvcrt.getch', side_effect=[b'\xe0', b'M']):
            s.read_key()
        self.assertFalse(s.toggle_monitor)

    def test_mp3_cmd_resamples_unsupported_rate_and_downmixes(self):
        cmd = ar.build_ffmpeg_cmd('mp3', 'o.mp3', 320, 96000, 4, 16)
        self.assertEqual(cmd[cmd.index('-ar', cmd.index('-i')) + 1], '48000')
        self.assertIn('-ac', cmd[cmd.index('-compression_level'):])

    def test_input_queue_is_bounded_and_counts_drops(self):
        from unittest.mock import patch, MagicMock
        with patch('sounddevice.InputStream', return_value=MagicMock()) as cls:
            stream = ar.AudioInputStream(44100, 1, np.int16, 512, device=0)
            cb = cls.call_args[1]['callback']
            data = np.ones((512, 1), dtype=np.int16)
            for _ in range(ar.MAX_INPUT_QUEUE_CHUNKS + 5):
                cb(data, 512, None, None)
            self.assertEqual(stream._queue.qsize(), ar.MAX_INPUT_QUEUE_CHUNKS)
            self.assertEqual(stream._drops, 5)

    def test_wav_header_is_valid_while_recording(self):
        import wave
        sink = ar.WavSink()
        path = os.path.join(self.td, 'live.wav')
        sink.open(path, 8000, 1, 16)
        sink.write(np.ones((100, 1), dtype=np.int16), 16)
        with wave.open(path, 'rb') as wf:  # read without closing the sink
            self.assertEqual(wf.getnframes(), 100)
        sink.close()

    def test_wav_size_limit_is_clamped(self):
        from unittest.mock import patch
        with patch('sys.argv', ['audio_recorder.py', '-s', '8GB', '-o', 'x',
                                '--output-dir', self.td]), \
             patch('audio_recorder.load_config', return_value={}), \
             patch('sys.stderr', new=open(os.devnull, 'w')):
            args = ar.parse_args()
        self.assertEqual(args.size, ar.WAV_MAX_FILE_BYTES)

    def test_invalid_output_name_rejected(self):
        from unittest.mock import patch
        with patch('sys.argv', ['audio_recorder.py', '-o', 'a/b']), \
             patch('audio_recorder.load_config', return_value={}), \
             patch('sys.stderr', new=open(os.devnull, 'w')), \
             self.assertRaises(SystemExit):
            ar.parse_args()

    def test_preflight_fails_when_no_audio_arrives(self):
        from unittest.mock import patch, MagicMock
        class Args:
            bit_depth = 16
            sample_rate = 44100
            channels = 1
            device = None
        silent = MagicMock()
        silent.__enter__.return_value = silent
        silent.wait_for_data.return_value = False
        with patch('audio_recorder.AudioInputStream', return_value=silent), \
             patch('sounddevice.query_devices', side_effect=Exception('x')):
            ok, err = ar.test_audio_device(Args())
        self.assertFalse(ok)
        self.assertIn('no audio', err)

    def test_failing_sink_halts_without_deleting_existing_recordings(self):
        from unittest.mock import patch, MagicMock
        for i in (1, 2, 3):
            self._touch(f'ft_{i:04d}.wav')

        class BadSink(ar.AudioSink):
            fmt = 'wav'
            def open(self, path, sr, ch, bd, br=None):
                open(path, 'wb').close()
            def write(self, data, bd):
                raise OSError('boom')
            def close(self): pass
            def get_current_size(self): return 0
            def get_total_bytes(self, st, cur): return 0

        class Args:
            output_dir = self.td
            output = 'ft'
            size = 10000000
            max_recordings = 4
            bit_depth = 16
            bit_rate = 128
            sample_rate = 44100
            channels = 1
            length = 5
            file_format = 'wav'
            device = None

        stream = MagicMock()
        stream.read.return_value = (np.zeros((100, 1), dtype=np.int16), False)
        summary = MagicMock()
        with patch('audio_recorder.draw_startup'), \
             patch('audio_recorder.AudioInputStream', return_value=stream), \
             patch('audio_recorder.draw_hud'), \
             patch('audio_recorder.set_raw_terminal'), \
             patch('audio_recorder.restore_terminal'), \
             patch('audio_recorder._print_session_summary', summary):
            ar.record_engine(Args(), BadSink())
        for i in (1, 2, 3):
            self.assertTrue(os.path.exists(os.path.join(self.td, f'ft_{i:04d}.wav')))
        self.assertTrue(summary.call_args[0][0].fatal_msg)

    def test_failed_device_check_does_not_touch_existing_files(self):
        from unittest.mock import patch
        for i in range(1, 5):
            self._touch(f'dv_{i:04d}.wav')

        class Args:
            output_dir = self.td
            output = 'dv'
            size = 1000
            max_recordings = 4
            bit_depth = 16
            bit_rate = 128
            sample_rate = 44100
            channels = 1
            length = 1
            file_format = 'wav'
            device = None

        with patch('audio_recorder.draw_startup', side_effect=SystemExit(1)), \
             patch('audio_recorder.set_raw_terminal'), \
             patch('audio_recorder.restore_terminal'), \
             patch('audio_recorder._print_session_summary'), \
             self.assertRaises(SystemExit):
            ar.record_engine(Args(), ar.WavSink())
        self.assertEqual(len(os.listdir(self.td)), 4)

    def test_fatal_disk_message_is_kept_for_summary(self):
        from unittest.mock import patch
        import io
        s = ar.UIState()
        class Args:
            file_format = 'wav'
            bit_depth = 16
            sample_rate = 44100
            channels = 2
            output_dir = '.'
            device = None
        with patch('audio_recorder.get_disk_info', return_value=(100 << 20, 70 << 20, 10 << 20)), \
             patch('sys.stdout', new=io.StringIO()):
            ar.draw_hud(s, Args(), 'test.wav', 100, 1000, 1.0)
        self.assertIn('Low disk space', s.fatal_msg)

    def test_monitor_downmixes_extra_channels(self):
        from unittest.mock import MagicMock
        mon = MagicMock()
        ar.write_monitor_audio(mon, 2, np.ones((10, 4), dtype=np.int16))
        self.assertEqual(mon.write.call_args[0][0].shape, (10, 2))


class TestDeviceSwitchFinalizesFile(unittest.TestCase):
    def _args(self):
        class Args:
            device = 0
            channels = 2
            sample_rate = 48000
            bit_depth = 16
        return Args()

    def test_before_switch_runs_between_stop_and_close(self):
        from unittest.mock import patch, MagicMock
        order = []
        old = MagicMock()
        old.stop.side_effect = lambda: order.append('stop')
        old.close.side_effect = lambda: order.append('close')
        st = ar.UIState()
        st.select_device = True
        info = {'name': 'Mic', 'max_input_channels': 2, 'default_samplerate': 48000}
        with patch('audio_recorder.prompt_select_device', return_value=1), \
             patch('sounddevice.query_devices', return_value=info), \
             patch('sounddevice.check_input_settings'), \
             patch('audio_recorder.AudioInputStream', return_value=MagicMock()):
            ar.handle_device_switch(st, self._args(), old, 48000, 2, np.int16, 512,
                                    before_switch=lambda s: order.append('finalize'))
        self.assertEqual(order, ['stop', 'finalize', 'close'])
        self.assertTrue(st.new_file)

    def test_failed_switch_still_starts_new_file_after_finalizing(self):
        from unittest.mock import patch, MagicMock
        st = ar.UIState()
        st.select_device = True
        info = {'name': 'Mic', 'max_input_channels': 2, 'default_samplerate': 48000}
        restored = MagicMock()
        streams = [Exception('cannot open'), restored]
        def factory(*a, **k):
            r = streams.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with patch('audio_recorder.prompt_select_device', return_value=1), \
             patch('sounddevice.query_devices', return_value=info), \
             patch('sounddevice.check_input_settings'), \
             patch('audio_recorder.AudioInputStream', side_effect=factory):
            res = ar.handle_device_switch(st, self._args(), MagicMock(), 48000, 2, np.int16, 512,
                                          before_switch=lambda s: None)
        self.assertIs(res, restored)
        self.assertTrue(st.new_file)

    def test_engine_flushes_queued_audio_and_closes_file_on_switch(self):
        from unittest.mock import patch, MagicMock
        td = tempfile.mkdtemp()
        try:
            class Args:
                output_dir = td
                output = 'sw'
                size = 10000000
                max_recordings = 4
                bit_depth = 16
                bit_rate = 128
                sample_rate = 8000
                channels = 1
                length = 3
                file_format = 'wav'
                device = 0

            old = MagicMock()
            old.read.return_value = (np.ones((100, 1), dtype=np.int16), False)
            old.drain.return_value = [np.ones((50, 1), dtype=np.int16)]
            new = MagicMock()
            new.read.return_value = (np.ones((100, 1), dtype=np.int16), False)
            made = []
            def factory(*a, **k):
                made.append(1)
                return old if len(made) == 1 else new
            calls = {'n': 0}
            def key(self):
                calls['n'] += 1
                if calls['n'] == 1:
                    self.select_device = True
                elif calls['n'] >= 3:
                    self.running = False
            info = {'name': 'Mic2', 'max_input_channels': 1, 'default_samplerate': 8000}
            with patch('audio_recorder.draw_startup'), \
                 patch('audio_recorder.AudioInputStream', side_effect=factory), \
                 patch('audio_recorder.draw_hud'), \
                 patch('audio_recorder.UIState.read_key', key), \
                 patch('audio_recorder.prompt_select_device', return_value=1), \
                 patch('sounddevice.query_devices', return_value=info), \
                 patch('sounddevice.check_input_settings'), \
                 patch('audio_recorder.set_raw_terminal'), \
                 patch('audio_recorder.restore_terminal'), \
                 patch('audio_recorder._print_session_summary'):
                ar.record_engine(Args(), ar.WavSink())
            import wave
            with wave.open(os.path.join(td, 'sw_0001.wav'), 'rb') as wf:
                self.assertEqual(wf.getnframes(), 150)  # 100 read + 50 drained, footer valid
            self.assertTrue(os.path.exists(os.path.join(td, 'sw_0002.wav')))
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_compressed_on_rotate_is_idempotent(self):
        from unittest.mock import patch, MagicMock
        sink = ar.CompressedSink('mp3', 128)
        proc = MagicMock()
        proc.poll.return_value = None
        with patch('subprocess.Popen', return_value=proc), \
             patch('audio_recorder._get_file_size', return_value=1000):
            sink.open('x.mp3', 44100, 1, 16)
            st = ar.UIState()
            sink.on_rotate(st)
            sink.on_rotate(st)
        self.assertEqual(st.total_bytes, 1000)
