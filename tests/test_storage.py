import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pynput import keyboard, mouse
from app import (KeyEvent, MacroApp, MouseEvent, RedPresenceFilter, count_red_blob_pixels, decode_key, encode_key, format_hotkey,
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
