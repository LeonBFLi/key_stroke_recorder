import json
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pynput import keyboard, mouse
from app import (KeyEvent, MacroApp, MouseEvent, Region, RedMonitorPanel, YellowStabilityMonitorPanel, RedPresenceFilter, save_configuration, count_red_blob_pixels, decode_key, encode_key, format_hotkey,
                 load_events, parse_click_rate, parse_hotkey, save_events, save_red_detection_screenshot,
                 signature_difference, visual_signature)


class FakeImage:
    def __init__(self, width, height, pixels):
        self.size = (width, height)
        self._pixels = pixels

    def convert(self, _mode):
        return self

    def getdata(self):
        return self._pixels

    def resize(self, size, _resampling):
        # Tests below use the signature's native sample size.
        self.size = size
        return self

    def save(self, path, format=None):
        self.saved = (path, format)


class MonitorActionTests(unittest.TestCase):
    def make_panel(self, panel_class):
        panel = panel_class.__new__(panel_class)
        panel.root = Mock()
        panel.running = True
        panel.stop_event = threading.Event()
        panel.start_button = Mock()
        panel.stop_button = Mock()
        panel.status = Mock()
        panel._log = Mock()
        panel.sound_on_detection = Mock(get=Mock(return_value=False))
        return panel

    def test_yellow_disappearance_only_closes_after_tracking_when_enabled(self):
        for enabled, counts, expected in ((True, [0, 12, 0], True),
                                          (True, [0, 0, 0], False),
                                          (False, [0, 12, 0], False)):
            with self.subTest(enabled=enabled, counts=counts):
                panel = self.make_panel(YellowStabilityMonitorPanel)
                frames = iter(counts)
                panel._close_foreground_window = Mock()
                panel._failed = Mock()
                panel.root.after.side_effect = lambda _delay, callback: callback()

                def signature(_image):
                    return b"frame", next(frames)

                waits = 0

                def wait(_seconds):
                    nonlocal waits
                    waits += 1
                    if waits >= len(counts):
                        panel.stop_event.set()

                with patch("app.ImageGrab.grab"), patch("app.visual_signature", side_effect=signature), \
                        patch.object(panel.stop_event, "wait", side_effect=wait):
                    panel._monitor(Region(0, 0, 10, 10), (999, 50, 8, 1.0, enabled))
                panel._failed.assert_not_called()
                if expected:
                    panel._close_foreground_window.assert_called_once_with(disappeared=True)
                else:
                    panel._close_foreground_window.assert_not_called()

    def test_red_detection_sound_can_be_disabled(self):
        panel = self.make_panel(RedMonitorPanel)
        with patch("app.play_notification_sound") as sound:
            panel._first_detected(12)
            sound.assert_not_called()
            panel._log.assert_called_with("检测到红点（12 像素），已开始倒计时")
            panel.sound_on_detection.get.return_value = True
            panel._first_detected(12)
            sound.assert_called_once_with(panel.root)

    def test_either_monitor_close_stops_all_tasks_and_always_sounds(self):
        for trigger_class, disappeared in ((RedMonitorPanel, False),
                                           (YellowStabilityMonitorPanel, False),
                                           (YellowStabilityMonitorPanel, True)):
            with self.subTest(trigger=trigger_class.__name__, disappeared=disappeared):
                app = MacroApp.__new__(MacroApp)
                app.recording = app.playing = app.clicking = True
                app.stop_recording = Mock()
                app.stop_playback = threading.Event()
                app.stop_clicking = threading.Event()
                auto_stop = threading.Event()
                app.auto_key_slots = [{"running": True, "stop_event": auto_stop}]
                app.red_monitor = self.make_panel(RedMonitorPanel)
                app.yellow_monitor = self.make_panel(YellowStabilityMonitorPanel)
                for panel in (app.red_monitor, app.yellow_monitor):
                    panel.on_close_trigger = app.stop_all
                trigger = app.red_monitor if trigger_class is RedMonitorPanel else app.yellow_monitor
                other = app.yellow_monitor if trigger_class is RedMonitorPanel else app.red_monitor

                def assert_stopped(*_args):
                    app.stop_recording.assert_called_once()
                    self.assertTrue(app.stop_playback.is_set())
                    self.assertTrue(app.stop_clicking.is_set())
                    self.assertTrue(auto_stop.is_set())
                    for panel in (trigger, other):
                        self.assertFalse(panel.running)
                        self.assertTrue(panel.stop_event.is_set())

                user32 = Mock()
                user32.GetForegroundWindow.return_value = 123
                user32.PostMessageW.side_effect = assert_stopped
                with patch("ctypes.windll", Mock(user32=user32), create=True), \
                        patch("app.play_notification_sound", side_effect=assert_stopped) as sound:
                    if disappeared:
                        trigger._close_foreground_window(disappeared=True)
                    else:
                        trigger._close_foreground_window()
                    user32.PostMessageW.assert_called_once_with(123, 0x0010, 0, 0)
                    sound.assert_called_once_with(trigger.root)
                    # A queued callback from the other monitor must not close
                    # the next foreground window or play another alert.
                    other._close_foreground_window()
                    trigger._close_foreground_window()
                    self.assertEqual(user32.PostMessageW.call_count, 1)
                    self.assertEqual(sound.call_count, 1)

    def test_close_failure_still_stops_monitor_and_sounds(self):
        for panel_class in (RedMonitorPanel, YellowStabilityMonitorPanel):
            with self.subTest(trigger=panel_class.__name__):
                panel = self.make_panel(panel_class)
                panel.on_close_trigger = Mock()
                user32 = Mock()
                user32.GetForegroundWindow.return_value = 0
                with patch("ctypes.windll", Mock(user32=user32), create=True), \
                        patch("app.play_notification_sound") as sound:
                    panel._close_foreground_window()
                    panel.on_close_trigger.assert_called_once()
                    self.assertFalse(panel.running)
                    self.assertTrue(panel.stop_event.is_set())
                    sound.assert_called_once_with(panel.root)
                    user32.PostMessageW.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def make_app(self):
        interpreter = tk.Tcl()
        app = MacroApp.__new__(MacroApp)
        app.root = Mock()
        app.root.globalgetvar.side_effect = interpreter.globalgetvar
        app.root.geometry.return_value = "820x620+20+30"
        app.loop_mode = tk.StringVar(interpreter, "forever")
        app.count_var = tk.StringVar(interpreter, "27")
        app.click_rate_var = tk.StringVar(interpreter, "18.5")
        app.click_hotkey = frozenset({keyboard.KeyCode.from_char("c"), mouse.Button.right})
        app.click_hotkey_var = tk.StringVar(interpreter)
        app.auto_key_slots = [{"key": keyboard.KeyCode.from_char(str(index)),
                               "key_var": tk.StringVar(interpreter),
                               "interval_var": tk.StringVar(interpreter, str(index + 1)),
                               "running": False} for index in range(5)]
        app.red_monitor = Mock(region=Region(1, 2, 30, 40))
        app.red_monitor.sound_on_detection = tk.BooleanVar(interpreter, False)
        app.red_monitor.settings = {"red_threshold": tk.IntVar(interpreter, 190),
                                    "close_delay": tk.DoubleVar(interpreter, 12.5)}
        app.yellow_monitor = Mock(region=Region(10, 20, 300, 400))
        for name, value in (("stall_seconds", 20), ("check_interval", 250),
                            ("min_yellow_pixels", 15), ("change_percent", 2.5)):
            setattr(app.yellow_monitor, name, tk.DoubleVar(interpreter, value))
        app.yellow_monitor.close_on_disappearance = tk.BooleanVar(interpreter, True)
        app.notebook = Mock()
        app.notebook.index.return_value = 2
        app.feature_tabs = Mock()
        app.feature_tabs.index.return_value = 1
        app.file_path = None
        app.events = []
        app.playing = app.clicking = app.recording = False
        app._mode_changed = Mock()
        app.path_label = Mock()
        app.event_label = Mock()
        app.play_button = Mock()
        app.status = Mock()
        return app

    def test_configuration_round_trip_restores_all_options_without_starting_tasks(self):
        original = self.make_app()
        with tempfile.TemporaryDirectory() as folder:
            recording = Path(folder) / "macro.ksr.json"
            events = [KeyEvent(0.1, "press", "char", "a")]
            save_events(recording, events)
            original.file_path = recording
            expected = original._configuration()
            path = Path(folder) / "settings.json"
            save_configuration(path, expected)
            restored = self.make_app()
            for variable in restored._configuration_variables().values():
                variable.set("0")
            restored.click_hotkey = frozenset()
            for slot in restored.auto_key_slots:
                slot["key"] = None
            with patch("app.configuration_path", return_value=path), patch("app.messagebox.showwarning") as warning:
                restored._restore_configuration()
                warning.assert_not_called()
            self.assertEqual(restored._configuration()["variables"], expected["variables"])
            self.assertEqual(restored.click_hotkey, original.click_hotkey)
            self.assertEqual([slot["key"] for slot in restored.auto_key_slots],
                             [slot["key"] for slot in original.auto_key_slots])
            restored.red_monitor._region_selected.assert_called_once_with(original.red_monitor.region)
            restored.yellow_monitor._region_selected.assert_called_once_with(original.yellow_monitor.region)
            restored.root.geometry.assert_any_call(expected["geometry"])
            restored.notebook.select.assert_called_once_with(2)
            restored.feature_tabs.select.assert_called_once_with(1)
            self.assertEqual(restored.file_path, recording)
            self.assertEqual(restored.events, events)
            self.assertFalse(restored.playing or restored.clicking or restored.recording)
            self.assertTrue(all(not slot["running"] for slot in restored.auto_key_slots))

    def test_missing_and_corrupt_configuration_do_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for content in (None, "{bad json", "[]"):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_text(content, encoding="utf-8")
                    app = self.make_app()
                    with patch("app.configuration_path", return_value=path), patch("app.messagebox.showwarning") as warning:
                        app._restore_configuration()
                        self.assertEqual(warning.call_count, 0 if content is None else 1)
                    self.assertEqual(app.count_var.get(), "27")

    def test_close_saves_before_destroying_and_preserves_file_on_failed_write(self):
        app = self.make_app()
        app.stop_all = Mock()
        app.hotkey_listener = Mock()
        app.mouse_hotkey_listener = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with patch("app.configuration_path", return_value=path):
                app.close()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), app._configuration())
            app.root.destroy.assert_called_once()
            saved = path.read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("denied")):
                with self.assertRaises(OSError):
                    save_configuration(path, {"new": True})
            self.assertEqual(path.read_bytes(), saved)

    def test_failed_save_leaves_window_open_for_retry(self):
        app = self.make_app()
        app.stop_all = Mock()
        app.hotkey_listener = Mock()
        app.mouse_hotkey_listener = Mock()
        with patch("app.save_configuration", side_effect=OSError("denied")), \
                patch("app.messagebox.showerror") as error:
            app.close()
        app.stop_all.assert_called_once()
        error.assert_called_once()
        app.root.destroy.assert_not_called()
        app.hotkey_listener.stop.assert_not_called()

    def test_unfinished_numeric_entry_is_saved_without_losing_other_settings(self):
        app = self.make_app()
        app.yellow_monitor.stall_seconds.set("unfinished")
        self.assertEqual(app._configuration()["variables"]["yellow_stall_seconds"], "unfinished")
        self.assertEqual(app._configuration()["variables"]["loop_count"], "27")


class StorageTests(unittest.TestCase):
    def test_f8_starts_playback_instead_of_recording_when_idle(self):
        app = MacroApp.__new__(MacroApp)
        app.capturing_hotkey = False
        app.recording = app.playing = app.clicking = False
        app.red_monitor = Mock(running=False)
        app.yellow_monitor = Mock(running=False)
        app.root = Mock()
        app.toggle_playback = Mock()
        app.toggle_recording = Mock()

        app._global_key_press(keyboard.Key.f8)

        app.root.after.assert_called_once_with(0, app.toggle_playback)
        app.toggle_recording.assert_not_called()

    def test_f8_still_stops_active_tasks(self):
        app = MacroApp.__new__(MacroApp)
        app.capturing_hotkey = False
        app.recording = True
        app.playing = app.clicking = False
        app.red_monitor = Mock(running=False)
        app.yellow_monitor = Mock(running=False)
        app.root = Mock()
        app.stop_all = Mock()

        app._global_key_press(keyboard.Key.f8)

        app.root.after.assert_called_once_with(0, app.stop_all)

    def test_key_round_trip(self):
        for key in (keyboard.Key.enter, keyboard.KeyCode.from_char("中"), keyboard.KeyCode.from_vk(65)):
            key_type, value = encode_key(key)
            decoded = decode_key(KeyEvent(0.1, "press", key_type, value))
            self.assertEqual(decoded, key)

    def test_file_round_trip(self):
        expected = [
            KeyEvent(0.25, "press", "char", "a"),
            MouseEvent(0.02, "move", 320, 240),
            MouseEvent(0.1, "press", 320, 240, "left"),
            MouseEvent(0.05, "release", 320, 240, "left"),
            KeyEvent(0.1, "release", "char", "a"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro.ksr.json"
            save_events(path, expected)
            self.assertEqual(load_events(path), expected)

    def test_loads_version_one_keyboard_recording(self):
        payload = {"version": 1, "events": [
            {"delay": 0.2, "action": "press", "key_type": "char", "value": "a"},
        ]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.ksr.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_events(path), [KeyEvent(0.2, "press", "char", "a")])

    def test_rejects_invalid_mouse_event(self):
        payload = {"version": 2, "events": [
            {"device": "mouse", "delay": 0, "action": "press", "x": 1, "y": 2,
             "button": "unknown"},
        ]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad-mouse.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_events(path)

    def test_rejects_invalid_action(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text(json.dumps({"version": 1, "events": [{"delay": 0, "action": "bad", "key_type": "char", "value": "x"}]}))
            with self.assertRaises(ValueError):
                load_events(path)

    def test_click_rate_validation(self):
        self.assertEqual(parse_click_rate("12.5"), 12.5)
        for value in ("nope", "0", "101"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_click_rate(value)

    def test_hotkey_validation(self):
        # The headless pynput dummy backend does not expose real modifier keys.
        # Mock only its platform parser while exercising our validation logic.
        with patch("app.keyboard.HotKey.parse", return_value=[keyboard.KeyCode.from_vk(1)]):
            self.assertTrue(parse_hotkey("<ctrl>+<alt>+c"))
        for value in ("", "<f8>", "<not-a-key>"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_hotkey(value)

    def test_captured_hotkey_formatting(self):
        # The dummy backend aliases several special keys, so compare against
        # the backend's own key name rather than assuming its Windows value.
        self.assertEqual(format_hotkey(frozenset({keyboard.Key.f6})), keyboard.Key.f6.name.upper())
        self.assertEqual(format_hotkey(frozenset({mouse.Button.right})), "鼠标右键")
        label = format_hotkey(frozenset({keyboard.Key.ctrl, keyboard.KeyCode.from_char("c")}))
        self.assertEqual(set(label.split(" + ")), {keyboard.Key.ctrl.name.upper(), "C"})

    def test_red_blob_detection_ignores_isolated_pixels(self):
        pixels = [(255, 255, 255)] * 400
        pixels[21] = (255, 0, 0)
        image = FakeImage(20, 20, pixels)
        self.assertEqual(count_red_blob_pixels(image, 215, 95, 95, 95, 160, 4, 55), 0)

        for y in range(5, 9):
            for x in range(5, 9):
                pixels[y * 20 + x] = (255, 0, 0)
        self.assertEqual(count_red_blob_pixels(image, 215, 95, 95, 95, 160, 4, 55), 16)

    def test_sensitive_defaults_detect_small_antialiased_red_dot(self):
        pixels = [(40, 80, 100)] * 100
        for index, color in zip((44, 45, 54, 55, 56, 65),
                                ((205, 82, 76), (220, 70, 70), (195, 90, 85),
                                 (235, 80, 75), (190, 95, 90), (210, 85, 80))):
            pixels[index] = color
        image = FakeImage(10, 10, pixels)
        self.assertEqual(count_red_blob_pixels(image, 180, 55, 165, 165, 85, 5, 30), 6)

    def test_red_detection_rejects_yellow_but_accepts_red_orange(self):
        pixels = [(30, 30, 30)] * 100
        for y in range(3, 7):
            for x in range(3, 7):
                pixels[y * 10 + x] = (245, 165, 15)
        image = FakeImage(10, 10, pixels)
        self.assertEqual(count_red_blob_pixels(image, 180, 55, 200, 165, 85, 5, 30), 0)

        for y in range(3, 7):
            for x in range(3, 7):
                pixels[y * 10 + x] = (245, 70, 40)
        self.assertEqual(count_red_blob_pixels(image, 180, 55, 200, 165, 85, 5, 30), 16)

    def test_red_presence_filter_tolerates_brief_misses_and_movement(self):
        tracker = RedPresenceFilter(detect_confirmations=2, missing_confirmations=3)
        self.assertEqual(tracker.update(True), (False, False))
        self.assertEqual(tracker.update(True), (True, True))
        # A moving dot can be missed for a frame while screenshots are sampled.
        self.assertEqual(tracker.update(False), (True, False))
        self.assertEqual(tracker.update(True), (True, False))
        self.assertEqual(tracker.update(False), (True, False))
        self.assertEqual(tracker.update(False), (True, False))
        self.assertEqual(tracker.update(False), (False, True))

    def test_red_detection_screenshot_is_saved_in_requested_directory(self):
        image = FakeImage(1, 1, [(255, 0, 0)])
        captured_at = __import__("datetime").datetime(2026, 8, 30, 12, 34, 56, 789)
        with tempfile.TemporaryDirectory() as folder:
            path = save_red_detection_screenshot(image, Path(folder), captured_at)
        self.assertEqual(path.name, "red_dot_detection_20260830_123456_000789.png")
        self.assertEqual(image.saved, (path, "PNG"))

    def test_visual_signature_tracks_yellow_bar_and_number_changes(self):
        pixels = [(20, 20, 20)] * (48 * 16)
        for index in range(100):
            pixels[index] = (230, 190, 20)
        first, yellow_count = visual_signature(FakeImage(48, 16, pixels))
        self.assertEqual(yellow_count, 100)

        changed = list(pixels)
        changed[300:320] = [(240, 240, 240)] * 20  # changed digit strokes
        second, _ = visual_signature(FakeImage(48, 16, changed))
        self.assertGreater(signature_difference(first, second), 0)
        self.assertEqual(signature_difference(first, first), 0)


if __name__ == "__main__":
    unittest.main()
