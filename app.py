"""Windows keyboard macro recorder with a small Tkinter user interface."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pynput import keyboard, mouse


APP_NAME = "键盘录制与回放工具"
FILE_VERSION = 1
MIN_CLICK_RATE = 0.5
MAX_CLICK_RATE = 100.0


@dataclass
class KeyEvent:
    delay: float
    action: str
    key_type: str
    value: str | int | None


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


def save_events(path: Path, events: list[KeyEvent]) -> None:
    payload = {"version": FILE_VERSION, "events": [asdict(item) for item in events]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_events(path: Path) -> list[KeyEvent]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != FILE_VERSION or not isinstance(payload.get("events"), list):
        raise ValueError("不是受支持的按键录制文件")
    events = [KeyEvent(**item) for item in payload["events"]]
    for item in events:
        if item.action not in {"press", "release"} or item.delay < 0:
            raise ValueError("录制文件中包含无效事件")
        decode_key(item)
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


class MacroApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: list[KeyEvent] = []
        self.file_path: Path | None = None
        self.recording = False
        self.playing = False
        self.record_listener: keyboard.Listener | None = None
        self.last_event_at = 0.0
        self.capture_lock = threading.Lock()
        self.stop_playback = threading.Event()
        self.clicking = False
        self.stop_clicking = threading.Event()
        self.click_hotkey = parse_hotkey("<f6>")
        self.hotkey_pressed: set[keyboard.Key | keyboard.KeyCode] = set()
        self.hotkey_armed = True

        root.title(APP_NAME)
        root.geometry("700x590")
        root.minsize(640, 540)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()

        # F8 is an emergency stop even while another program has focus.
        self.hotkey_listener = keyboard.Listener(on_press=self._global_key_press, on_release=self._global_key_release)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

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
        self.click_hotkey_var = tk.StringVar(value="<f6>")
        ttk.Entry(click_box, width=18, textvariable=self.click_hotkey_var).grid(row=0, column=3, sticky="w")
        ttk.Label(click_box, text="格式示例：<f6>、<ctrl>+<alt>+c", foreground="#666").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.click_button = ttk.Button(click_box, text="开始连点", command=self.toggle_clicking)
        self.click_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        click_box.columnconfigure(3, weight=1)

        status_box = ttk.LabelFrame(outer, text="状态", padding=12)
        status_box.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(status_box, textvariable=self.status, wraplength=570).pack(anchor="w")
        self.progress = ttk.Progressbar(status_box, mode="indeterminate")
        self.progress.pack(fill="x", pady=(12, 0))

    def _mode_changed(self) -> None:
        self.count_spin.configure(state="normal" if self.loop_mode.get() == "count" else "disabled")

    def _global_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if key == keyboard.Key.f8:
            self.root.after(0, self.stop_all)
            return
        canonical = self.hotkey_listener.canonical(key)
        self.hotkey_pressed.add(canonical)
        if self.hotkey_armed and self.click_hotkey.issubset(self.hotkey_pressed):
            self.hotkey_armed = False
            self.root.after(0, self.toggle_clicking)

    def _global_key_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        canonical = self.hotkey_listener.canonical(key)
        self.hotkey_pressed.discard(canonical)
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
        self.status.set("正在录制…请切换到目标窗口操作。完成后点击“停止录制”或按 F8。")
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

    def stop_recording(self) -> None:
        with self.capture_lock:
            if not self.recording:
                return
            self.recording = False
        if self.record_listener:
            self.record_listener.stop()
            self.record_listener = None
        self.record_button.configure(text="开始录制")
        self.save_button.configure(state="normal" if self.events else "disabled")
        self.play_button.configure(state="normal" if self.events else "disabled")
        self.progress.stop()
        self.status.set(f"录制完成，共 {len(self.events)} 个按键事件。请保存录制。")

    def save(self) -> None:
        if not self.events:
            return
        chosen = filedialog.asksaveasfilename(title="保存按键录制", defaultextension=".ksr.json", filetypes=[("按键录制", "*.ksr.json"), ("JSON", "*.json")])
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
        chosen = filedialog.askopenfilename(title="选择按键录制", filetypes=[("按键录制", "*.ksr.json"), ("JSON", "*.json"), ("所有文件", "*.*")])
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
        controller = keyboard.Controller()
        completed = 0
        error: Exception | None = None
        pressed: set[keyboard.Key | keyboard.KeyCode] = set()
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
                    key = decode_key(event)
                    if event.action == "press":
                        controller.press(key)
                        pressed.add(key)
                    else:
                        controller.release(key)
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
                    controller.release(key)
                except Exception:
                    pass
        self.root.after(0, lambda: self._play_finished(completed, error))

    def toggle_clicking(self) -> None:
        if self.clicking:
            self.stop_clicking.set()
            return
        try:
            rate = parse_click_rate(self.click_rate_var.get())
            hotkey = parse_hotkey(self.click_hotkey_var.get())
        except ValueError as exc:
            messagebox.showwarning("连点设置无效", str(exc))
            return
        self.click_hotkey = hotkey
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

    def close(self) -> None:
        self.stop_all()
        self.hotkey_listener.stop()
        self.root.destroy()


def main() -> None:
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
