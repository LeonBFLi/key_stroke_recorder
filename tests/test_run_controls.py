import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from app import KeyEvent, MacroApp, Region, fit_window_geometry


class CombinedRunTests(unittest.TestCase):
    def make_app(self):
        interpreter = tk.Tcl()
        app = MacroApp.__new__(MacroApp)
        app.root = Mock()
        app.events = [KeyEvent(0.1, "press", "char", "a")]
        app.recording = app.playing = False
        app.loop_mode = tk.StringVar(interpreter, "count")
        app.count_var = tk.StringVar(interpreter, "2")
        app.run_red_monitor = tk.BooleanVar(interpreter, True)
        app.run_yellow_monitor = tk.BooleanVar(interpreter, True)
        app.red_monitor = Mock(running=False, region=Region(0, 0, 20, 20))
        app.yellow_monitor = Mock(running=False, region=Region(50, 50, 100, 100))
        app.red_monitor._validated_settings.return_value = {}
        app.yellow_monitor._settings.return_value = (10, 250, 8, 1, False)
        for panel in (app.red_monitor, app.yellow_monitor):
            panel.stop_event = threading.Event()

            def start(target=panel):
                target.running = True
                return True

            panel.start.side_effect = start
        app.toggle_playback = Mock(side_effect=lambda: setattr(app, "playing", True))
        app.stop_all = Mock()
        app.status = Mock()
        app._refresh_monitor_status = Mock()
        return app

    def test_starts_selected_monitors_before_playback(self):
        app = self.make_app()

        def playback():
            self.assertTrue(app.red_monitor.running)
            self.assertTrue(app.yellow_monitor.running)
            app.playing = True

        app.toggle_playback.side_effect = playback
        app.start_with_monitors()
        app.red_monitor.start.assert_called_once()
        app.yellow_monitor.start.assert_called_once()
        app.toggle_playback.assert_called_once()

    def test_missing_selected_region_does_not_partially_start(self):
        app = self.make_app()
        app.yellow_monitor.region = None
        with patch("app.messagebox.showwarning"):
            app.start_with_monitors()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_invalid_monitor_settings_do_not_partially_start(self):
        app = self.make_app()
        app.yellow_monitor._settings.side_effect = ValueError("invalid interval")
        with patch("app.messagebox.showwarning"):
            app.start_with_monitors()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_invalid_loop_count_does_not_start_any_monitor(self):
        app = self.make_app()
        app.count_var.set("unfinished")
        with patch("app.messagebox.showwarning"):
            app.start_with_monitors()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_unselected_monitor_need_not_be_configured(self):
        app = self.make_app()
        app.run_red_monitor.set(False)
        app.red_monitor.region = None
        app.start_with_monitors()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_called_once()
        app.toggle_playback.assert_called_once()

    def test_missing_recording_does_not_start_monitors(self):
        app = self.make_app()
        app.events = []
        with patch("app.messagebox.showwarning"):
            app.start_with_monitors()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_second_monitor_start_failure_rolls_back_only_newly_started_monitor(self):
        app = self.make_app()
        app.yellow_monitor.start.side_effect = None
        app.yellow_monitor.start.return_value = False
        app.start_with_monitors()
        app.red_monitor.stop.assert_called_once()
        app.yellow_monitor.stop.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_existing_monitor_survives_new_monitor_start_failure(self):
        app = self.make_app()
        app.red_monitor.running = True
        app.yellow_monitor.start.side_effect = None
        app.yellow_monitor.start.return_value = False
        app.start_with_monitors()
        app.red_monitor.stop.assert_not_called()
        app.toggle_playback.assert_not_called()

    def test_button_stops_all_when_playback_is_running(self):
        app = self.make_app()
        app.playing = True
        app.start_with_monitors()
        app.stop_all.assert_called_once()
        app.red_monitor.start.assert_not_called()
        app.yellow_monitor.start.assert_not_called()


class MonitorStatusTests(unittest.TestCase):
    def test_indicators_follow_running_state_rather_than_start_selection(self):
        interpreter = tk.Tcl()
        app = MacroApp.__new__(MacroApp)
        app.red_monitor = Mock(running=True, region=Region(0, 0, 20, 20))
        app.yellow_monitor = Mock(running=False, region=Region(30, 30, 60, 60))
        app.run_red_monitor = tk.BooleanVar(interpreter, False)
        app.run_yellow_monitor = tk.BooleanVar(interpreter, True)
        app.red_monitor_indicator = Mock()
        app.yellow_monitor_indicator = Mock()
        app._refresh_monitor_status()
        self.assertIn("已激活", app.red_monitor_indicator.configure.call_args.kwargs["text"])
        self.assertIn("未激活", app.yellow_monitor_indicator.configure.call_args.kwargs["text"])
        app.red_monitor.running = False
        app.yellow_monitor.region = None
        app._refresh_monitor_status()
        self.assertIn("未激活", app.red_monitor_indicator.configure.call_args.kwargs["text"])
        self.assertIn("未设置", app.yellow_monitor_indicator.configure.call_args.kwargs["text"])


class WindowGeometryTests(unittest.TestCase):
    def make_root(self, width=1024, height=768):
        root = Mock()
        root.winfo_screenwidth.return_value = width
        root.winfo_screenheight.return_value = height
        root.winfo_fpixels.side_effect = lambda measure: 16 if measure == "0.17i" else 48
        return root

    def test_oversized_saved_window_is_clamped_inside_screen(self):
        root = self.make_root()
        with patch("app.sys.platform", "linux"):
            fit_window_geometry(root, "1600x1200+1000+900")
        root.geometry.assert_called_once_with("1008x720+0+0")

    def test_normal_saved_size_and_position_are_retained(self):
        root = self.make_root()
        with patch("app.sys.platform", "linux"):
            fit_window_geometry(root, "600x400+20+30")
        root.geometry.assert_called_once_with("600x400+20+30")

    def test_saved_right_and_bottom_offsets_keep_their_position(self):
        root = self.make_root(1280, 720)
        with patch("app.sys.platform", "linux"):
            fit_window_geometry(root, "720x500-100-80")
        root.geometry.assert_called_once_with("720x500+460+140")

    def test_windows_work_area_leaves_the_taskbar_visible(self):
        root = self.make_root(1920, 1080)
        user32 = Mock()

        def monitor_info(_monitor, pointer):
            pointer._obj.rcWork.left = pointer._obj.rcWork.top = 0
            pointer._obj.rcWork.right = 1920
            pointer._obj.rcWork.bottom = 1040
            return True

        user32.GetMonitorInfoW.side_effect = monitor_info
        with patch("app.sys.platform", "win32"), patch("ctypes.windll", Mock(user32=user32), create=True):
            fit_window_geometry(root, "2000x1600+100+200")
        root.geometry.assert_called_once_with("1904x992+0+0")

    def test_small_screen_can_fit_window_below_normal_minimum(self):
        root = self.make_root(400, 300)
        with patch("app.sys.platform", "linux"):
            fit_window_geometry(root)
        root.minsize.assert_called_once_with(384, 252)
        root.geometry.assert_called_once_with("384x252+0+0")


if __name__ == "__main__":
    unittest.main()
