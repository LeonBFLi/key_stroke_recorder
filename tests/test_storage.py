import json
import tempfile
import unittest
from pathlib import Path

from pynput import keyboard

from app import KeyEvent, decode_key, encode_key, load_events, save_events


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


if __name__ == "__main__":
    unittest.main()
