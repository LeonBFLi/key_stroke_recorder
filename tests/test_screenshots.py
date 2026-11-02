import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import Region, ScreenshotPanel, save_monitor_screenshot


class ScreenshotTests(unittest.TestCase):
    def make_panel(self, directory):
        panel = ScreenshotPanel.__new__(ScreenshotPanel)
        panel.region = None
        panel.directory = Path(directory)
        panel.status = Mock()
        panel.region_label = Mock()
        return panel

    def test_saves_png_in_new_directory_with_trigger_and_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "screenshots"
            image = Image.new("RGB", (3, 2), "yellow")
            path = save_monitor_screenshot(image, "yellow_stall", directory,
                                           datetime(2026, 11, 2, 12, 34, 56, 123456))
            self.assertEqual(path.name, "yellow_stall_20261102_123456_123456.png")
            with Image.open(path) as restored:
                self.assertEqual(restored.format, "PNG")
                self.assertEqual(restored.size, (3, 2))
                self.assertEqual(restored.getpixel((0, 0)), (255, 255, 0))

    def test_custom_capture_area_overrides_monitor_area_and_reset_uses_fallback(self):
        custom = Region(10, 20, 110, 90)
        monitor = Region(2, 3, 20, 30)
        with tempfile.TemporaryDirectory() as folder:
            panel = self.make_panel(folder)
            panel._region_selected(custom)
            with patch("app.ImageGrab.grab", return_value=Image.new("RGB", (1, 1))) as grab:
                path = panel.capture_trigger("red_dot_detection", monitor)
                grab.assert_called_once_with(bbox=(10, 20, 110, 90))
                self.assertTrue(path.is_file())
                panel.clear_region()
                grab.reset_mock()
                path = panel.capture_trigger("yellow_disappeared", monitor)
                grab.assert_called_once_with(bbox=(2, 3, 20, 30))
                self.assertTrue(path.is_file())

    def test_manual_capture_requires_selected_area(self):
        panel = self.make_panel("unused")
        with patch("app.messagebox.showwarning") as warning, patch("app.ImageGrab.grab") as grab:
            panel.capture_now()
            warning.assert_called_once()
            grab.assert_not_called()

    def test_capture_failure_updates_status_and_propagates_to_monitor(self):
        panel = self.make_panel("unused")
        with patch("app.ImageGrab.grab", side_effect=OSError("screen unavailable")):
            with self.assertRaisesRegex(OSError, "screen unavailable"):
                panel.capture_trigger("yellow_stall", Region(1, 2, 3, 4))
        panel.status.set.assert_called_once_with("截图保存失败：screen unavailable")

    def test_manual_capture_reports_write_failure(self):
        panel = self.make_panel("unused")
        panel.region = Region(1, 2, 3, 4)
        with patch("app.ImageGrab.grab"), \
                patch("app.save_monitor_screenshot", side_effect=PermissionError("denied")), \
                patch("app.messagebox.showerror") as error:
            panel.capture_now()
        error.assert_called_once_with("截图失败", "denied")


if __name__ == "__main__":
    unittest.main()
