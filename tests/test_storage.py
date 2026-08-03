import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pynput import keyboard, mouse

from app import KeyEvent, decode_key, encode_key, format_hotkey, load_events, parse_click_rate, parse_hotkey, save_events


class StorageTests(unittest.TestCase):
    def test_key_round_trip(self):
        for key in (keyboard.Key.enter, keyboard.KeyCode.from_char("中"), keyboard.KeyCode.from_vk(65)):
            key_type, value = encode_key(key)
            decoded = decode_key(KeyEvent(0.1, "press", key_type, value))
            self.assertEqual(decoded, key)

    def test_file_round_trip(self):
        expected = [KeyEvent(0.25, "press", "char", "a"), KeyEvent(0.1, "release", "char", "a")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro.ksr.json"
            save_events(path, expected)
            self.assertEqual(load_events(path), expected)

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


if __name__ == "__main__":
    unittest.main()
