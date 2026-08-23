import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pynput import keyboard, mouse
from app import (KeyEvent, MouseEvent, RedPresenceFilter, count_red_blob_pixels, decode_key, encode_key, format_hotkey,
                 load_events, parse_click_rate, parse_hotkey, save_events)


class FakeImage:
    def __init__(self, width, height, pixels):
        self.size = (width, height)
        self._pixels = pixels

    def convert(self, _mode):
        return self

    def getdata(self):
        return self._pixels


class StorageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
