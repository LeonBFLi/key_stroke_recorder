"""Windows keyboard and mouse macro recorder with a small Tkinter UI."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pynput import keyboard, mouse

try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover - shown as a friendly UI error at runtime
    ImageGrab = None


APP_NAME = "键盘鼠标录制、回放与红点监控工具"
FILE_VERSION = 2
MIN_CLICK_RATE = 0.5
MAX_CLICK_RATE = 100.0
HotkeyInput = keyboard.Key | keyboard.KeyCode | mouse.Button


@dataclass
class KeyEvent:
    delay: float
    action: str
    key_type: str
    value: str | int | None


@dataclass
class MouseEvent:
    delay: float
    action: str
    x: int
    y: int
    button: str | None = None


MacroEvent = KeyEvent | MouseEvent


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def encode_key(key: keyboard.Key | keyboard.KeyCode) -> tuple[str, str | int | None]:
    if isinstance(key, keyboard.Key):
        return "special", key.name
    # A listener event normally includes the virtual-key code. Prefer it over
    # the translated character so playback reproduces the physical key (and
    # lets separately recorded Shift/Ctrl/Alt state determine the result).
    if key.vk is not None:
        return "vk", key.vk
    if key.char is not None:
        return "char", key.char
    return "vk", key.vk


def decode_key(event: KeyEvent) -> keyboard.Key | keyboard.KeyCode:
    if event.key_type == "special":
        try:
            return keyboard.Key[event.value]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"未知特殊按键: {event.value}") from exc
    if event.key_type == "char":
        return keyboard.KeyCode.from_char(str(event.value))
    if event.key_type == "vk":
        return keyboard.KeyCode.from_vk(int(event.value))
    raise ValueError(f"未知按键类型: {event.key_type}")


def save_events(path: Path, events: list[MacroEvent]) -> None:
    serialized = []
    for item in events:
        data = asdict(item)
        data["device"] = "keyboard" if isinstance(item, KeyEvent) else "mouse"
        serialized.append(data)
    payload = {"version": FILE_VERSION, "events": serialized}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_events(path: Path) -> list[MacroEvent]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version not in {1, FILE_VERSION} or not isinstance(payload.get("events"), list):
        raise ValueError("不是受支持的键盘鼠标录制文件")
    events: list[MacroEvent] = []
    for data in payload["events"]:
        if not isinstance(data, dict):
            raise ValueError("录制文件中包含无效事件")
        item = dict(data)
        device = item.pop("device", "keyboard" if version == 1 else None)
        if device == "keyboard":
            events.append(KeyEvent(**item))
        elif device == "mouse":
            events.append(MouseEvent(**item))
        else:
            raise ValueError("录制文件中包含未知设备事件")
    for item in events:
        if item.delay < 0:
            raise ValueError("录制文件中包含无效事件")
        if isinstance(item, KeyEvent):
            if item.action not in {"press", "release"}:
                raise ValueError("录制文件中包含无效键盘事件")
            decode_key(item)
        elif (item.action not in {"move", "press", "release"}
              or (item.action == "move" and item.button is not None)
              or (item.action != "move" and item.button not in {"left", "middle", "right"})):
            raise ValueError("录制文件中包含无效鼠标事件")
    return events


def parse_click_rate(value: str) -> float:
    """Validate clicks per second separately from the Tk user interface."""
    try:
        rate = float(value)
    except ValueError as exc:
        raise ValueError("连点频率必须是数字") from exc
    if not MIN_CLICK_RATE <= rate <= MAX_CLICK_RATE:
        raise ValueError(f"连点频率必须在 {MIN_CLICK_RATE:g} 到 {MAX_CLICK_RATE:g} 次/秒之间")
    return rate


def parse_hotkey(value: str) -> frozenset[keyboard.Key | keyboard.KeyCode]:
    """Parse pynput's portable hotkey syntax and reserve F8 for emergency stop."""
    try:
        keys = frozenset(keyboard.HotKey.parse(value.strip().lower()))
    except (ValueError, TypeError) as exc:
        raise ValueError("热键格式无效，例如：<f6> 或 <ctrl>+<alt>+c") from exc
    if not keys:
        raise ValueError("热键不能为空")
    if keyboard.Key.f8 in keys:
        raise ValueError("F8 是紧急停止键，请选择其他热键")
    return keys


def format_hotkey(keys: frozenset[HotkeyInput]) -> str:
    """Return a readable label for a hotkey captured from input devices."""
    labels: list[str] = []
    for key in sorted(keys, key=str):
        if isinstance(key, keyboard.Key):
            labels.append(key.name.upper())
        elif isinstance(key, mouse.Button):
            labels.append({
                mouse.Button.left: "鼠标左键",
                mouse.Button.middle: "鼠标中键",
                mouse.Button.right: "鼠标右键",
            }.get(key, str(key).removeprefix("Button.")))
        else:
            labels.append(key.char.upper() if key.char else f"VK {key.vk}")
    return " + ".join(labels)


class MacroApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: list[MacroEvent] = []
        self.file_path: Path | None = None
        self.recording = False
        self.playing = False
        self.record_listener: keyboard.Listener | None = None
        self.mouse_record_listener: mouse.Listener | None = None
        self.last_event_at = 0.0
        self.capture_lock = threading.Lock()
        self.stop_playback = threading.Event()
        self.clicking = False
        self.stop_clicking = threading.Event()
        self.click_hotkey: frozenset[HotkeyInput] = frozenset({keyboard.Key.f6})
        self.hotkey_pressed: set[HotkeyInput] = set()
        self.hotkey_armed = True
        self.capturing_hotkey = False
        self.hotkey_capture_started = False
        self.hotkey_capture_keys: set[HotkeyInput] = set()

        root.title(APP_NAME)
        root.geometry("780x720")
        root.minsize(700, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()

        # F8 is an emergency stop even while another program has focus.
        self.hotkey_listener = keyboard.Listener(on_press=self._global_key_press, on_release=self._global_key_release)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()
        self.mouse_hotkey_listener = mouse.Listener(
            on_click=self._global_mouse_click,
        )
        self.mouse_hotkey_listener.daemon = True
        self.mouse_hotkey_listener.start()

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        outer = ttk.Frame(notebook, padding=18)
        monitor_tab = ttk.Frame(notebook, padding=12)
        notebook.add(outer, text="键盘鼠标录制与回放")
        notebook.add(monitor_tab, text="红点监控")
        self.red_monitor = RedMonitorPanel(monitor_tab)

        ttk.Label(outer, text=APP_NAME, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="录制时可切换到任意窗口；F8 可随时停止所有任务。", foreground="#555").pack(anchor="w", pady=(4, 18))

        record_box = ttk.LabelFrame(outer, text="1. 录制", padding=12)
        record_box.pack(fill="x")
        self.record_button = ttk.Button(record_box, text="开始录制", command=self.toggle_recording)
        self.record_button.pack(side="left")
        self.save_button = ttk.Button(record_box, text="保存录制…", command=self.save, state="disabled")
        self.save_button.pack(side="left", padx=8)
        self.event_label = ttk.Label(record_box, text="尚未录制")
        self.event_label.pack(side="left", padx=10)

        play_box = ttk.LabelFrame(outer, text="2. 回放", padding=12)
        play_box.pack(fill="x", pady=14)
        ttk.Button(play_box, text="选择录制文件…", command=self.open_file).grid(row=0, column=0, padx=(0, 8))
        self.path_label = ttk.Label(play_box, text="未选择文件")
        self.path_label.grid(row=0, column=1, columnspan=4, sticky="w")
        ttk.Label(play_box, text="循环方式：").grid(row=1, column=0, sticky="e", pady=(14, 0))
        self.loop_mode = tk.StringVar(value="count")
        ttk.Radiobutton(play_box, text="指定次数", variable=self.loop_mode, value="count", command=self._mode_changed).grid(row=1, column=1, pady=(14, 0))
        self.count_var = tk.StringVar(value="1")
        self.count_spin = ttk.Spinbox(play_box, from_=1, to=9999, width=7, textvariable=self.count_var)
        self.count_spin.grid(row=1, column=2, pady=(14, 0), padx=(4, 12))
        ttk.Radiobutton(play_box, text="一直循环，直到停止", variable=self.loop_mode, value="forever", command=self._mode_changed).grid(row=1, column=3, pady=(14, 0))
        self.play_button = ttk.Button(play_box, text="开始运行", command=self.toggle_playback, state="disabled")
        self.play_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        play_box.columnconfigure(1, weight=1)

        click_box = ttk.LabelFrame(outer, text="3. 鼠标连点", padding=12)
        click_box.pack(fill="x", pady=(0, 14))
        ttk.Label(click_box, text="频率（次/秒）：").grid(row=0, column=0, sticky="e")
        self.click_rate_var = tk.StringVar(value="10")
        ttk.Spinbox(click_box, from_=MIN_CLICK_RATE, to=MAX_CLICK_RATE, increment=0.5, width=8,
                    textvariable=self.click_rate_var).grid(row=0, column=1, sticky="w")
        ttk.Label(click_box, text="开关热键：").grid(row=0, column=2, sticky="e", padx=(18, 0))
        self.click_hotkey_var = tk.StringVar(value=format_hotkey(self.click_hotkey))
        ttk.Label(click_box, width=18, textvariable=self.click_hotkey_var, relief="sunken", padding=(5, 3)).grid(
            row=0, column=3, sticky="ew")
        self.capture_hotkey_button = ttk.Button(click_box, text="设置热键", command=self.start_hotkey_capture)
        self.capture_hotkey_button.grid(row=0, column=4, padx=(8, 0))
        ttk.Label(click_box, text="点击“设置热键”后，按下键盘按键、组合键或鼠标键", foreground="#666").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.click_button = ttk.Button(click_box, text="开始连点", command=self.toggle_clicking)
        self.click_button.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        click_box.columnconfigure(3, weight=1)

        status_box = ttk.LabelFrame(outer, text="状态", padding=12)
        status_box.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(status_box, textvariable=self.status, wraplength=570).pack(anchor="w")
        self.progress = ttk.Progressbar(status_box, mode="indeterminate")
        self.progress.pack(fill="x", pady=(12, 0))

    def start_hotkey_capture(self) -> None:
        """Arm global listeners to learn the next complete keyboard/mouse chord."""
        if self.clicking:
            messagebox.showwarning("无法设置热键", "请先停止鼠标连点。")
            return
        self.capturing_hotkey = True
        self.hotkey_capture_started = False
        self.hotkey_capture_keys.clear()
        self.hotkey_pressed.clear()
        self.capture_hotkey_button.configure(state="disabled")
        self.click_button.configure(state="disabled")
        self.click_hotkey_var.set("等待按下热键…")
        self.status.set("请按下要使用的键盘按键、组合键或鼠标键；松开后即完成设置。")

    def _capture_hotkey_press(self, key: HotkeyInput) -> None:
        if key == keyboard.Key.f8:
            self.root.after(0, lambda: self._finish_hotkey_capture(None, "F8 是紧急停止键，请选择其他热键。"))
            return
        if key == mouse.Button.left:
            self.root.after(0, lambda: self._finish_hotkey_capture(None, "鼠标左键用于执行连点，请选择其他鼠标键。"))
            return
        self.hotkey_capture_started = True
        self.hotkey_capture_keys.add(key)
        self.hotkey_pressed.add(key)

    def _capture_hotkey_release(self, key: HotkeyInput) -> None:
        self.hotkey_pressed.discard(key)
        if self.hotkey_capture_started and not self.hotkey_pressed:
            captured = frozenset(self.hotkey_capture_keys)
            self.root.after(0, lambda: self._finish_hotkey_capture(captured))

    def _finish_hotkey_capture(self, keys: frozenset[HotkeyInput] | None, error: str | None = None) -> None:
        if not self.capturing_hotkey:
            return
        self.capturing_hotkey = False
        self.hotkey_capture_started = False
        self.hotkey_capture_keys.clear()
        self.hotkey_pressed.clear()
        self.capture_hotkey_button.configure(state="normal")
        self.click_button.configure(state="normal")
        if keys:
            self.click_hotkey = keys
            self.click_hotkey_var.set(format_hotkey(keys))
            self.status.set(f"鼠标连点热键已设置为：{format_hotkey(keys)}。")
        else:
            self.click_hotkey_var.set(format_hotkey(self.click_hotkey))
            if error:
                messagebox.showwarning("热键不可用", error)
                self.status.set("热键未更改。")

    def _mode_changed(self) -> None:
        self.count_spin.configure(state="normal" if self.loop_mode.get() == "count" else "disabled")

    def _global_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if key == keyboard.Key.f8:
            if self.capturing_hotkey:
                self._capture_hotkey_press(key)
                return
            self.root.after(0, self.stop_all)
            return
        canonical = self.hotkey_listener.canonical(key)
        if self.capturing_hotkey:
            self._capture_hotkey_press(canonical)
            return
        self.hotkey_pressed.add(canonical)
        if self.hotkey_armed and self.click_hotkey.issubset(self.hotkey_pressed):
            self.hotkey_armed = False
            self.root.after(0, self.toggle_clicking)

    def _global_key_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        canonical = self.hotkey_listener.canonical(key)
        if self.capturing_hotkey:
            self._capture_hotkey_release(canonical)
            return
        self.hotkey_pressed.discard(canonical)
        if not self.click_hotkey.issubset(self.hotkey_pressed):
            self.hotkey_armed = True

    def _global_mouse_click(self, _x: int, _y: int, button: mouse.Button, pressed: bool) -> None:
        if self.capturing_hotkey:
            if pressed:
                self._capture_hotkey_press(button)
            else:
                self._capture_hotkey_release(button)
            return
        if pressed:
            self.hotkey_pressed.add(button)
            if self.hotkey_armed and self.click_hotkey.issubset(self.hotkey_pressed):
                self.hotkey_armed = False
                self.root.after(0, self.toggle_clicking)
        else:
            self.hotkey_pressed.discard(button)
            if not self.click_hotkey.issubset(self.hotkey_pressed):
                self.hotkey_armed = True

    def toggle_recording(self) -> None:
        if self.recording:
            self.stop_recording()
            return
        if self.playing:
            return
        self.events = []
        self.file_path = None
        self.recording = True
        self.last_event_at = time.perf_counter()
        self.record_listener = keyboard.Listener(on_press=lambda key: self._capture("press", key), on_release=lambda key: self._capture("release", key))
        self.record_listener.start()
        self.record_button.configure(text="停止录制")
        self.save_button.configure(state="disabled")
        self.play_button.configure(state="disabled")
        self.mouse_record_listener = mouse.Listener(on_move=self._capture_mouse_move,
                                                    on_click=self._capture_mouse_click)
        self.mouse_record_listener.start()
        self.status.set("正在录制键盘、鼠标移动和点击…完成后点击“停止录制”或按 F8。")
        self.progress.start(12)

    def _capture(self, action: str, key: keyboard.Key | keyboard.KeyCode) -> None:
        # F8 belongs to the application itself and must never become part of a macro.
        if key == keyboard.Key.f8:
            return
        # Both listener callbacks can arrive nearly simultaneously.  Serialize the
        # timestamp and append operation so chords retain their true event order.
        with self.capture_lock:
            if not self.recording:
                return
            now = time.perf_counter()
            key_type, value = encode_key(key)
            self.events.append(KeyEvent(now - self.last_event_at, action, key_type, value))
            self.last_event_at = now
            event_count = len(self.events)
        self.root.after(0, lambda n=event_count: self.event_label.configure(text=f"已记录 {n} 个事件"))

    def _capture_mouse_move(self, x: int, y: int) -> None:
        self._append_mouse_event("move", x, y)

    def _capture_mouse_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        button_name = {
            mouse.Button.left: "left",
            mouse.Button.middle: "middle",
            mouse.Button.right: "right",
        }.get(button)
        if button_name is not None:
            self._append_mouse_event("press" if pressed else "release", x, y, button_name)

    def _append_mouse_event(self, action: str, x: int, y: int, button: str | None = None) -> None:
        with self.capture_lock:
            if not self.recording:
                return
            now = time.perf_counter()
            self.events.append(MouseEvent(now - self.last_event_at, action, int(x), int(y), button))
            self.last_event_at = now
            event_count = len(self.events)
        self.root.after(0, lambda n=event_count: self.event_label.configure(text=f"已记录 {n} 个事件"))

    def stop_recording(self) -> None:
        with self.capture_lock:
            if not self.recording:
                return
            self.recording = False
        if self.record_listener:
            self.record_listener.stop()
            self.record_listener = None
        if self.mouse_record_listener:
            self.mouse_record_listener.stop()
            self.mouse_record_listener = None
        self.record_button.configure(text="开始录制")
        self.save_button.configure(state="normal" if self.events else "disabled")
        self.play_button.configure(state="normal" if self.events else "disabled")
        self.progress.stop()
        self.status.set(f"录制完成，共 {len(self.events)} 个键盘和鼠标事件。请保存录制。")

    def save(self) -> None:
        if not self.events:
            return
        chosen = filedialog.asksaveasfilename(title="保存键盘鼠标录制", defaultextension=".ksr.json", filetypes=[("键盘鼠标录制", "*.ksr.json"), ("JSON", "*.json")])
        if not chosen:
            return
        try:
            save_events(Path(chosen), self.events)
            self.file_path = Path(chosen)
            self.path_label.configure(text=str(self.file_path))
            self.status.set("录制已保存。")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def open_file(self) -> None:
        chosen = filedialog.askopenfilename(title="选择键盘鼠标录制", filetypes=[("键盘鼠标录制", "*.ksr.json"), ("JSON", "*.json"), ("所有文件", "*.*")])
        if not chosen:
            return
        try:
            self.events = load_events(Path(chosen))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("打开失败", str(exc))
            return
        self.file_path = Path(chosen)
        self.path_label.configure(text=str(self.file_path))
        self.event_label.configure(text=f"已加载 {len(self.events)} 个事件")
        self.play_button.configure(state="normal" if self.events else "disabled")
        self.status.set("录制文件已加载，可以开始运行。")

    def toggle_playback(self) -> None:
        if self.playing:
            self.stop_playback.set()
            self.status.set("正在停止，请稍候…")
            return
        if not self.events or self.recording:
            return
        forever = self.loop_mode.get() == "forever"
        try:
            count = int(self.count_var.get())
            if not 1 <= count <= 9999:
                raise ValueError
        except ValueError:
            messagebox.showwarning("循环次数无效", "请输入 1 到 9999 之间的整数。")
            return
        self.playing = True
        self.stop_playback.clear()
        self.play_button.configure(text="停止运行")
        self.record_button.configure(state="disabled")
        self.progress.start(12)
        threading.Thread(target=self._play_worker, args=(None if forever else count,), daemon=True).start()

    def _play_worker(self, loops: int | None) -> None:
        key_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        completed = 0
        error: Exception | None = None
        pressed: set[keyboard.Key | keyboard.KeyCode] = set()
        pressed_buttons: set[mouse.Button] = set()
        try:
            while not self.stop_playback.is_set() and (loops is None or completed < loops):
                self.root.after(0, lambda n=completed + 1: self.status.set(f"正在运行第 {n} 次…按 F8 可停止。"))
                # Schedule against one monotonic origin instead of sleeping after
                # every injection. This prevents OS/controller overhead from
                # accumulating and keeps close-together chord events aligned.
                started_at = time.perf_counter()
                target_offset = 0.0
                for event in self.events:
                    target_offset += event.delay
                    remaining = started_at + target_offset - time.perf_counter()
                    if remaining > 0 and self.stop_playback.wait(remaining):
                        break
                    if isinstance(event, MouseEvent):
                        mouse_controller.position = (event.x, event.y)
                        if event.action == "move":
                            continue
                        button = mouse.Button[event.button]  # type: ignore[index]
                        if event.action == "press":
                            mouse_controller.press(button)
                            pressed_buttons.add(button)
                        else:
                            mouse_controller.release(button)
                            pressed_buttons.discard(button)
                    else:
                        key = decode_key(event)
                        if event.action == "press":
                            key_controller.press(key)
                            pressed.add(key)
                        else:
                            key_controller.release(key)
                            pressed.discard(key)
                else:
                    completed += 1
                    continue
                break
        except Exception as exc:  # Errors from the OS-level keyboard backend must reach the UI.
            error = exc
        finally:
            # Stopping halfway through a shortcut must not leave Ctrl/Alt/etc. held down.
            for key in pressed:
                try:
                    key_controller.release(key)
                except Exception:
                    pass
            for button in pressed_buttons:
                try:
                    mouse_controller.release(button)
                except Exception:
                    pass
        self.root.after(0, lambda: self._play_finished(completed, error))

    def toggle_clicking(self) -> None:
        if self.clicking:
            self.stop_clicking.set()
            return
        try:
            rate = parse_click_rate(self.click_rate_var.get())
        except ValueError as exc:
            messagebox.showwarning("连点设置无效", str(exc))
            return
        self.hotkey_pressed.clear()
        self.hotkey_armed = False  # re-arm after the currently held hotkey is released
        self.clicking = True
        self.stop_clicking.clear()
        self.click_button.configure(text="停止连点")
        self.status.set(f"正在以 {rate:g} 次/秒连点；按设置的热键或 F8 停止。")
        threading.Thread(target=self._click_worker, args=(rate,), daemon=True).start()

    def _click_worker(self, rate: float) -> None:
        controller = mouse.Controller()
        interval = 1.0 / rate
        next_click = time.perf_counter()
        error: Exception | None = None
        try:
            while not self.stop_clicking.is_set():
                controller.click(mouse.Button.left)
                next_click += interval
                wait = next_click - time.perf_counter()
                if wait > 0 and self.stop_clicking.wait(wait):
                    break
                if wait <= -interval:
                    next_click = time.perf_counter()
        except Exception as exc:
            error = exc
        self.root.after(0, lambda: self._click_finished(error))

    def _click_finished(self, error: Exception | None) -> None:
        self.clicking = False
        self.click_button.configure(text="开始连点")
        if error:
            messagebox.showerror("连点失败", str(error))
            self.status.set("连点失败。")
        else:
            self.status.set("鼠标连点已停止。")

    def _play_finished(self, completed: int, error: Exception | None) -> None:
        self.playing = False
        self.progress.stop()
        self.play_button.configure(text="开始运行", state="normal")
        self.record_button.configure(state="normal")
        if error:
            messagebox.showerror("运行失败", str(error))
            self.status.set("运行失败。")
        else:
            self.status.set(f"运行已停止，完整执行 {completed} 次。")

    def stop_all(self) -> None:
        if self.recording:
            self.stop_recording()
        if self.playing:
            self.stop_playback.set()
        if self.clicking:
            self.stop_clicking.set()
        self.red_monitor.stop(silent=True)

    def close(self) -> None:
        self.stop_all()
        self.hotkey_listener.stop()
        self.mouse_hotkey_listener.stop()
        self.root.destroy()


class RegionSelector(tk.Toplevel):
    """Transparent full-screen overlay used to choose the monitored area."""

    def __init__(self, master: tk.Misc, callback) -> None:
        super().__init__(master)
        self.callback = callback
        self.start_x = self.start_y = 0
        self.start_root_x = self.start_root_y = 0
        self.rectangle: int | None = None
        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.25)
        self.attributes("-topmost", True)
        self.configure(bg="black")
        self.canvas = tk.Canvas(self, cursor="cross", bg="gray20", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _event: self.destroy())

    def _press(self, event: tk.Event) -> None:
        self.start_x, self.start_y = event.x, event.y
        self.start_root_x, self.start_root_y = event.x_root, event.y_root
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=2)

    def _drag(self, event: tk.Event) -> None:
        if self.rectangle is not None:
            self.canvas.coords(self.rectangle, self.start_x, self.start_y, event.x, event.y)

    def _release(self, event: tk.Event) -> None:
        region = Region(
            min(self.start_root_x, event.x_root), min(self.start_root_y, event.y_root),
            max(self.start_root_x, event.x_root), max(self.start_root_y, event.y_root),
        )
        if region.width < 5 or region.height < 5:
            messagebox.showwarning("区域太小", "请至少选择 5×5 像素的区域。", parent=self)
            return
        self.callback(region)
        self.destroy()


def count_red_blob_pixels(image, red_threshold: int, delta_threshold: int, green_max: int,
                          blue_max: int, min_saturation: int, min_blob_pixels: int,
                          min_blob_density: int) -> int:
    """Count pixels belonging to sufficiently large, dense red components."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    candidates: list[bool] = []
    for red, green, blue in rgb.getdata():
        maximum, minimum = max(red, green, blue), min(red, green, blue)
        saturation = int((maximum - minimum) / maximum * 255) if maximum else 0
        candidates.append(red >= red_threshold and green <= green_max and blue <= blue_max
                          and red - green >= delta_threshold and red - blue >= delta_threshold
                          and saturation >= min_saturation)

    visited = [False] * len(candidates)
    valid_count = 0
    for start, candidate in enumerate(candidates):
        if not candidate or visited[start]:
            continue
        queue = deque([start])
        visited[start] = True
        component: list[tuple[int, int]] = []
        while queue:
            current = queue.popleft()
            y, x = divmod(current, width)
            component.append((x, y))
            for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0),
                           (-1, 1), (0, 1), (1, 1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    index = ny * width + nx
                    if candidates[index] and not visited[index]:
                        visited[index] = True
                        queue.append(index)
        xs, ys = zip(*component)
        area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
        if len(component) >= min_blob_pixels and len(component) / area >= min_blob_density / 100:
            valid_count += len(component)
    return valid_count


class RedMonitorPanel:
    """Red-dot monitor embedded in the application's second tab."""

    def __init__(self, parent: ttk.Frame) -> None:
        self.parent = parent
        self.root = parent.winfo_toplevel()
        self.region: Region | None = None
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.settings = {
            "red_threshold": tk.IntVar(value=215), "delta_threshold": tk.IntVar(value=95),
            "green_max": tk.IntVar(value=95), "blue_max": tk.IntVar(value=95),
            "min_saturation": tk.IntVar(value=160), "min_blob_pixels": tk.IntVar(value=14),
            "min_blob_density": tk.IntVar(value=55), "min_red_pixels": tk.IntVar(value=18),
            "check_interval": tk.IntVar(value=120), "close_delay": tk.DoubleVar(value=10),
        }
        self._build_ui()

    def _build_ui(self) -> None:
        ttk.Label(self.parent, text="红点监控", font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(self.parent, text="发现红点时立即提示；持续存在到设定时间后关闭当时的前台窗口。",
                  foreground="#555").grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 14))
        ttk.Button(self.parent, text="选择监控区域", command=self.select_region).grid(row=2, column=0, sticky="w")
        self.region_label = ttk.Label(self.parent, text="尚未选择区域")
        self.region_label.grid(row=2, column=1, columnspan=3, sticky="w", padx=8)

        fields = [
            ("红色阈值 (R ≥)", "red_threshold"), ("红色优势 (R-G/B ≥)", "delta_threshold"),
            ("绿色上限", "green_max"), ("蓝色上限", "blue_max"),
            ("最小饱和度 (0–255)", "min_saturation"), ("最少红像素", "min_red_pixels"),
            ("最小红团像素", "min_blob_pixels"), ("最小红团密度 (%)", "min_blob_density"),
            ("检测间隔 (ms)", "check_interval"), ("持续 N 秒后关闭窗口", "close_delay"),
        ]
        for index, (label, key) in enumerate(fields):
            row, pair = 3 + index // 2, index % 2
            column = pair * 2
            ttk.Label(self.parent, text=label).grid(row=row, column=column, sticky="w", pady=7)
            ttk.Entry(self.parent, textvariable=self.settings[key], width=10).grid(row=row, column=column + 1, sticky="w")

        buttons = ttk.Frame(self.parent)
        buttons.grid(row=8, column=0, columnspan=4, sticky="w", pady=12)
        self.start_button = ttk.Button(buttons, text="开始监控", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止监控", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.status = tk.StringVar(value="状态：待机")
        ttk.Label(self.parent, textvariable=self.status).grid(row=9, column=0, columnspan=4, sticky="w")
        log_frame = ttk.LabelFrame(self.parent, text="运行日志", padding=6)
        log_frame.grid(row=10, column=0, columnspan=4, sticky="nsew", pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        for column in range(4):
            self.parent.columnconfigure(column, weight=1)
        self.parent.rowconfigure(10, weight=1)

    def select_region(self) -> None:
        RegionSelector(self.root, self._region_selected)

    def _region_selected(self, region: Region) -> None:
        self.region = region
        text = f"({region.left}, {region.top})–({region.right}, {region.bottom})，{region.width}×{region.height}"
        self.region_label.configure(text=text)
        self._log(f"已选择监控区域：{text}")

    def _validated_settings(self) -> dict[str, float | int]:
        values = {key: variable.get() for key, variable in self.settings.items()}
        byte_fields = ("red_threshold", "green_max", "blue_max", "min_saturation")
        if any(not 0 <= values[key] <= 255 for key in byte_fields):
            raise ValueError("颜色阈值必须在 0 到 255 之间。")
        if values["delta_threshold"] < 0 or values["min_blob_pixels"] < 1 or values["min_red_pixels"] < 1:
            raise ValueError("像素数量必须至少为 1，红色优势不能小于 0。")
        if not 1 <= values["min_blob_density"] <= 100:
            raise ValueError("红团密度必须在 1% 到 100% 之间。")
        if values["check_interval"] < 20 or values["close_delay"] < 0:
            raise ValueError("检测间隔至少为 20 ms，关闭等待时间不能小于 0 秒。")
        return values

    def start(self) -> None:
        if self.running:
            return
        if self.region is None:
            messagebox.showwarning("提示", "请先选择监控区域。")
            return
        if ImageGrab is None:
            messagebox.showerror("缺少依赖", "红点监控需要 Pillow，请重新运行 run.bat 安装依赖。")
            return
        try:
            settings = self._validated_settings()
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning("监控设置无效", str(exc))
            return
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("状态：监控中")
        self._log("开始监控")
        self.worker = threading.Thread(target=self._monitor, args=(self.region, settings), daemon=True)
        self.worker.start()

    def stop(self, silent: bool = False) -> None:
        if not self.running:
            return
        self.running = False
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.set("状态：已停止")
        if not silent:
            self._log("停止监控")

    def _monitor(self, region: Region, settings: dict[str, float | int]) -> None:
        detected_at: float | None = None
        action_done = False
        try:
            while not self.stop_event.is_set():
                image = ImageGrab.grab(bbox=(region.left, region.top, region.right, region.bottom))
                red_count = count_red_blob_pixels(
                    image, *(int(settings[key]) for key in ("red_threshold", "delta_threshold", "green_max",
                           "blue_max", "min_saturation", "min_blob_pixels", "min_blob_density")))
                now = time.monotonic()
                has_red = red_count >= int(settings["min_red_pixels"])
                if has_red and detected_at is None:
                    detected_at = now
                    action_done = False
                    self.root.after(0, lambda count=red_count: self._first_detected(count))
                elif not has_red and detected_at is not None:
                    detected_at = None
                    action_done = False
                    self.root.after(0, self._red_disappeared)
                if has_red and detected_at is not None and not action_done:
                    remaining = float(settings["close_delay"]) - (now - detected_at)
                    if remaining <= 0:
                        action_done = True
                        self.root.after(0, self._close_foreground_window)
                    else:
                        self.root.after(0, lambda seconds=remaining: self.status.set(
                            f"状态：检测到红点，若持续存在将在 {seconds:.1f} 秒后关闭当前窗口"))
                self.stop_event.wait(int(settings["check_interval"]) / 1000)
        except Exception as exc:
            self.root.after(0, lambda error=exc: self._monitor_failed(error))

    def _first_detected(self, count: int) -> None:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError):  # pragma: no cover - Windows normally supplies winsound
            self.root.bell()
        self._log(f"检测到红点（{count} 像素），已播放提示音并开始倒计时")

    def _red_disappeared(self) -> None:
        self.status.set("状态：红点已消失，继续监控")
        self._log("红点在倒计时结束前消失，已取消关闭窗口")

    def _close_foreground_window(self) -> None:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                raise RuntimeError("未找到前台窗口")
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            self.status.set("状态：红点持续存在，已发送关闭当前窗口指令")
            self._log("红点倒计时结束后仍存在，已关闭当前窗口")
        except Exception as exc:  # pragma: no cover - depends on the Windows desktop
            self._log(f"关闭当前窗口失败：{exc}")

    def _monitor_failed(self, error: Exception) -> None:
        self.stop()
        messagebox.showerror("红点监控失败", str(error))
        self._log(f"监控失败：{error}")

    def _log(self, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except (ImportError, AttributeError):  # pragma: no cover - only available on Windows
        pass
    root = tk.Tk()
    try:
        root.iconname(APP_NAME)
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
