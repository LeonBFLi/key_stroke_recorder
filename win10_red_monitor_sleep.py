import logging
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover
    ImageGrab = None


logger = logging.getLogger("red_monitor")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler("red_monitor.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def set_dpi_awareness():
    """提升 Win10/11 下高 DPI 缩放时的坐标准确性。"""
    try:
        import ctypes

        # Windows 10+ 推荐 API
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # pragma: no cover
        pass


@dataclass
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


class RegionSelector(tk.Toplevel):
    """全屏覆盖层：拖拽鼠标选择监控区域。"""

    def __init__(self, master: tk.Tk, callback):
        super().__init__(master)
        self.callback = callback
        self.start_x = 0
        self.start_y = 0
        self.start_root_x = 0
        self.start_root_y = 0
        self.rect_id = None

        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.25)
        self.attributes("-topmost", True)
        self.configure(bg="black")

        self.canvas = tk.Canvas(self, cursor="cross", bg="gray20", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", lambda _: self.destroy())

    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        self.start_root_x, self.start_root_y = event.x_root, event.y_root
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline="red",
            width=2,
        )

    def on_drag(self, event):
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        left = min(self.start_root_x, event.x_root)
        top = min(self.start_root_y, event.y_root)
        right = max(self.start_root_x, event.x_root)
        bottom = max(self.start_root_y, event.y_root)

        if right - left < 5 or bottom - top < 5:
            messagebox.showwarning("区域太小", "请至少选择 5x5 像素区域")
            return

        self.callback(Region(left, top, right, bottom))
        self.destroy()


class RedMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Win10 红点监控自动切换窗口")
        self.root.geometry("640x420")

        self.region = None
        self.running = False
        self.worker = None
        self.last_trigger = 0.0
        self.restore_delay = tk.IntVar(value=60)
        self._alt_tab_cycle_active = False

        self.red_threshold = tk.IntVar(value=220)
        self.delta_threshold = tk.IntVar(value=35)
        self.green_max = tk.IntVar(value=90)
        self.blue_max = tk.IntVar(value=90)
        self.min_saturation = tk.IntVar(value=150)
        self.min_blob_pixels = tk.IntVar(value=14)
        self.min_blob_density = tk.IntVar(value=55)
        self.check_interval = tk.IntVar(value=120)
        self.cooldown = tk.IntVar(value=8)
        self.min_red_pixels = tk.IntVar(value=1)
        self.action_mode = tk.StringVar(value="alt_tab")

        self._build_ui()
        self._start_clock()

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        top_info = ttk.Frame(container)
        top_info.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        self.datetime_label = ttk.Label(top_info, text="系统时间：--")
        self.datetime_label.pack(side=tk.LEFT)

        ttk.Button(container, text="选择监控区域", command=self.select_region).grid(
            row=1, column=0, sticky="w"
        )
        self.region_label = ttk.Label(container, text="尚未选择区域")
        self.region_label.grid(row=1, column=1, columnspan=3, sticky="w", padx=8)

        ttk.Label(container, text="红色阈值 (R >=)").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Entry(container, textvariable=self.red_threshold, width=8).grid(
            row=2, column=1, sticky="w"
        )

        ttk.Label(container, text="红色优势 (R-G/B >=)").grid(row=2, column=2, sticky="w")
        ttk.Entry(container, textvariable=self.delta_threshold, width=8).grid(
            row=2, column=3, sticky="w"
        )

        ttk.Label(container, text="G/B 上限 (忽略类红)").grid(row=3, column=0, sticky="w", pady=8)
        gb_frame = ttk.Frame(container)
        gb_frame.grid(row=3, column=1, sticky="w")
        ttk.Entry(gb_frame, textvariable=self.green_max, width=6).pack(side=tk.LEFT)
        ttk.Label(gb_frame, text="/").pack(side=tk.LEFT, padx=2)
        ttk.Entry(gb_frame, textvariable=self.blue_max, width=6).pack(side=tk.LEFT)

        ttk.Label(container, text="最小饱和度(0-255)").grid(row=3, column=2, sticky="w")
        ttk.Entry(container, textvariable=self.min_saturation, width=8).grid(
            row=3, column=3, sticky="w"
        )

        ttk.Label(container, text="最少红像素数量").grid(row=4, column=0, sticky="w", pady=8)
        ttk.Entry(container, textvariable=self.min_red_pixels, width=8).grid(
            row=4, column=1, sticky="w"
        )

        ttk.Label(container, text="最小红团像素/密度%").grid(row=4, column=2, sticky="w")
        blob_frame = ttk.Frame(container)
        blob_frame.grid(row=4, column=3, sticky="w")
        ttk.Entry(blob_frame, textvariable=self.min_blob_pixels, width=6).pack(side=tk.LEFT)
        ttk.Label(blob_frame, text="/").pack(side=tk.LEFT, padx=2)
        ttk.Entry(blob_frame, textvariable=self.min_blob_density, width=6).pack(side=tk.LEFT)

        ttk.Label(container, text="检测间隔(ms)").grid(row=5, column=0, sticky="w")
        ttk.Entry(container, textvariable=self.check_interval, width=8).grid(
            row=5, column=1, sticky="w"
        )

        ttk.Label(container, text="触发冷却(秒)").grid(row=5, column=2, sticky="w", pady=8)
        ttk.Entry(container, textvariable=self.cooldown, width=8).grid(
            row=5, column=3, sticky="w"
        )

        ttk.Label(container, text="还原延时(秒)").grid(row=6, column=0, sticky="w")
        ttk.Entry(container, textvariable=self.restore_delay, width=8).grid(
            row=6, column=1, sticky="w"
        )
        ttk.Label(container, text="检测后执行动作").grid(row=6, column=2, sticky="w")
        action_frame = ttk.Frame(container)
        action_frame.grid(row=6, column=3, sticky="w")
        ttk.Radiobutton(
            action_frame,
            text="关闭当前窗口",
            value="close_window",
            variable=self.action_mode,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            action_frame,
            text="Alt+Tab 切换",
            value="alt_tab",
            variable=self.action_mode,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(
            action_frame,
            text="点击鼠标中键",
            value="middle_click",
            variable=self.action_mode,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            action_frame,
            text="关游戏并休眠",
            value="close_and_sleep",
            variable=self.action_mode,
        ).pack(side=tk.LEFT, padx=6)

        btn_row = ttk.Frame(container)
        btn_row.grid(row=7, column=0, columnspan=4, sticky="w", pady=10)
        ttk.Button(btn_row, text="开始监控", command=self.start).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="停止监控", command=self.stop).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_row, text="3秒后测试动作", command=self.delayed_test_switch).pack(
            side=tk.LEFT
        )
        ttk.Button(btn_row, text="套用大红点推荐参数", command=self.apply_big_red_preset).pack(
            side=tk.LEFT, padx=8
        )

        self.status = ttk.Label(container, text="状态：待机")
        self.status.grid(row=8, column=0, columnspan=4, sticky="w", pady=(10, 0))

        tips = (
            "说明（更易理解）：\n"
            "当监控区域里出现“明显偏红”的像素，且数量达到“最少红像素数量”时，\n"
            "程序会：1) 对该区域截图保存到脚本同目录；2) 执行你在界面里选择的动作。\n"
            "当动作为“Alt+Tab 切换”时，检测到红点会先截图并立即 Alt+Tab 切到另一个窗口，\n"
            "暂停检测并在日志中显示 60 秒倒计时；倒计时结束后自动 Alt+Tab 切回原窗口复检。\n"
            "若仍有红点则立刻再次 Alt+Tab 切走并重复上述流程。"
        )
        ttk.Label(container, text=tips, foreground="gray35", wraplength=610, justify="left").grid(
            row=9, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )

        log_frame = ttk.LabelFrame(container, text="运行日志")
        log_frame.grid(row=10, column=0, columnspan=4, sticky="nsew", pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=6, wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state="disabled")

        for col in range(4):
            container.columnconfigure(col, weight=1)
        container.rowconfigure(10, weight=1)

    def apply_big_red_preset(self):
        """按用户示例里的大红圆点调优，尽量排除其他偏红元素。"""
        self.red_threshold.set(215)
        self.delta_threshold.set(95)
        self.green_max.set(95)
        self.blue_max.set(95)
        self.min_saturation.set(160)
        self.min_red_pixels.set(18)
        self.min_blob_pixels.set(14)
        self.min_blob_density.set(55)
        self._append_log("已套用“大红点”推荐参数（已增强去干扰能力）")

    def _start_clock(self):
        self._update_clock()

    def _update_clock(self):
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.datetime_label.config(text=f"系统时间：{now_text}")
        self.root.after(1000, self._update_clock)

    def _append_log(self, message: str):
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now_text}] {message}\n"
        logger.info(message)
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def select_region(self):
        RegionSelector(self.root, self._on_region_selected)

    def _on_region_selected(self, region: Region):
        self.region = region
        region_text = (
            f"({region.left},{region.top})-({region.right},{region.bottom}) "
            f"{region.width}x{region.height}"
        )
        self.region_label.config(text=region_text)
        self._append_log(f"已更新监控区域：{region_text}")

    def start(self):
        if self.running:
            return
        if not self.region:
            messagebox.showwarning("提示", "请先选择监控区域")
            return

        self.running = True
        self._alt_tab_cycle_active = False
        self.status.config(text="状态：监控中")
        self._append_log("开始监控")
        self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
        self.worker.start()

    def stop(self):
        self.running = False
        self._alt_tab_cycle_active = False
        self.status.config(text="状态：已停止")
        self._append_log("停止监控")

    def monitor_loop(self):
        while self.running:
            red_count, ratio = self._get_red_stats(self.region)
            now = time.time()
            cooling = (now - self.last_trigger) < self.cooldown.get()
            has_enough_red = red_count >= max(1, self.min_red_pixels.get())

            if has_enough_red and not cooling:
                self.last_trigger = now
                self.root.after(
                    0,
                    lambda current_ratio=ratio, current_count=red_count: self._on_detected(
                        current_count, current_ratio
                    ),
                )
            time.sleep(max(0.02, self.check_interval.get() / 1000.0))

    def _on_detected(self, red_count: int, ratio: float):
        self.status.config(text=f"状态：检测到红点 {red_count}px，占比 {ratio:.4f}")
        saved_path = self.capture_target_region()
        self._append_log(f"检测到红点：{red_count}px，占比 {ratio:.4f}")
        self.play_alert_sound()
        if saved_path:
            self._append_log(f"区域截图已保存：{saved_path}")
        self.perform_selected_action("检测到红点")
        if self.action_mode.get() == "alt_tab" and not self._alt_tab_cycle_active:
            self._alt_tab_cycle_active = True
            threading.Thread(target=self._alt_tab_cycle_worker, daemon=True).start()

    def play_alert_sound(self):
        """红点出现时播放轻微提示音，不影响原有动作。"""
        try:
            import winsound

            winsound.Beep(950, 120)
            self._append_log("已播放轻微提示音")
        except Exception as exc:  # pragma: no cover
            self._append_log(f"提示音播放失败：{exc}")

    def _get_red_stats(self, region: Region) -> tuple[int, float]:
        if ImageGrab is None:
            raise RuntimeError("缺少依赖 Pillow，请先执行: pip install pillow")
        img = ImageGrab.grab(bbox=(region.left, region.top, region.right, region.bottom))
        pixels = img.convert("RGB").getdata()

        red_th = self.red_threshold.get()
        delta = self.delta_threshold.get()
        green_max = self.green_max.get()
        blue_max = self.blue_max.get()
        sat_min = self.min_saturation.get()
        min_blob_pixels = max(1, self.min_blob_pixels.get())
        min_blob_density = max(1, self.min_blob_density.get()) / 100.0

        width = region.width
        height = region.height

        candidate = [False] * (width * height)
        for idx, (r, g, b) in enumerate(pixels):
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            sat_255 = int(((max_c - min_c) / max_c) * 255) if max_c else 0
            is_target_red = (
                r >= red_th
                and g <= green_max
                and b <= blue_max
                and (r - g) >= delta
                and (r - b) >= delta
                and sat_255 >= sat_min
            )
            candidate[idx] = is_target_red

        # 只统计“连成团”的大红点，过滤零散或细碎的红色元素
        visited = [False] * (width * height)
        valid_red_count = 0
        neighbors = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

        for idx, is_red in enumerate(candidate):
            if not is_red or visited[idx]:
                continue

            q = deque([idx])
            visited[idx] = True
            comp_size = 0
            min_x = width
            min_y = height
            max_x = 0
            max_y = 0

            while q:
                cur = q.popleft()
                y, x = divmod(cur, width)
                comp_size += 1
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)

                for dx, dy in neighbors:
                    nx = x + dx
                    ny = y + dy
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    nidx = ny * width + nx
                    if candidate[nidx] and not visited[nidx]:
                        visited[nidx] = True
                        q.append(nidx)

            bbox_area = (max_x - min_x + 1) * (max_y - min_y + 1)
            density = (comp_size / bbox_area) if bbox_area else 0.0
            if comp_size >= min_blob_pixels and density >= min_blob_density:
                valid_red_count += comp_size

        total = width * height
        return valid_red_count, (valid_red_count / total if total else 0.0)

    def capture_target_region(self) -> str:
        if ImageGrab is None or self.region is None:
            return ""
        script_dir = Path(__file__).resolve().parent
        filename = datetime.now().strftime("red_region_%Y%m%d_%H%M%S.png")
        save_path = script_dir / filename
        img = ImageGrab.grab(
            bbox=(self.region.left, self.region.top, self.region.right, self.region.bottom)
        )
        img.save(save_path)
        return str(save_path)

    def switch_to_recent_window(self, reason: str = "手动测试"):
        try:
            import ctypes

            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            VK_MENU = 0x12  # Alt
            VK_TAB = 0x09
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_TAB, 0, 0, 0)
            user32.keybd_event(VK_TAB, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            self._append_log(f"已执行 Alt+Tab 切换窗口（原因：{reason}）")
        except Exception as exc:  # pragma: no cover
            self._append_log(f"Alt+Tab 切换失败（原因：{reason}）：{exc}")

    def close_foreground_window(self, reason: str = "手动测试"):
        try:
            import ctypes

            user32 = ctypes.windll.user32
            WM_CLOSE = 0x0010
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                self._append_log(f"已发送关闭当前窗口指令（原因：{reason}）")
            else:
                self._append_log(f"未找到前台窗口，无法关闭（原因：{reason}）")
        except Exception as exc:  # pragma: no cover
            self._append_log(f"关闭当前窗口失败（原因：{reason}）：{exc}")

    def click_middle_button(self, reason: str = "手动测试"):
        try:
            import ctypes

            user32 = ctypes.windll.user32
            MOUSEEVENTF_MIDDLEDOWN = 0x0020
            MOUSEEVENTF_MIDDLEUP = 0x0040
            down_ret = user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
            up_ret = user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
            self._append_log(
                f"已执行鼠标中键点击（原因：{reason}，返回值 down={down_ret}, up={up_ret}）"
            )
            self._append_log("提示：mouse_event 通常返回 0；请在目标程序中确认是否产生中键效果")
        except Exception as exc:  # pragma: no cover
            self._append_log(f"鼠标中键点击失败（原因：{reason}）：{exc}")

    def perform_selected_action(self, reason: str = "手动测试"):
        action = self.action_mode.get()
        if action == "close_window":
            self.close_foreground_window(reason)
        elif action == "middle_click":
            self.click_middle_button(reason)
        elif action == "close_and_sleep":
            self.close_game_and_sleep(reason)
        else:
            self.switch_to_recent_window(reason)

    def delayed_test_switch(self):
        """界面测试按钮：延迟 3 秒后执行当前选中的动作。"""
        self._append_log("收到测试指令，将在 3 秒后执行当前动作")
        self.status.config(text="状态：测试中（3秒后执行动作）")
        self.root.after(3000, lambda: self.perform_selected_action("延迟3秒测试"))

    def close_game_and_sleep(self, reason: str = "手动测试"):
        self.close_foreground_window(f"{reason} - 关闭游戏")
        time.sleep(0.8)
        try:
            import ctypes

            powrprof = ctypes.windll.powrprof
            ret = powrprof.SetSuspendState(False, True, False)
            if ret:
                self._append_log(f"已触发系统休眠（原因：{reason}）")
            else:
                self._append_log(f"触发系统休眠失败（原因：{reason}，返回值={ret}）")
        except Exception as exc:  # pragma: no cover
            self._append_log(f"触发系统休眠失败（原因：{reason}）：{exc}")

    def _alt_tab_cycle_worker(self):
        while self.running and self._alt_tab_cycle_active and self.action_mode.get() == "alt_tab":
            # 第一次由 _on_detected 触发，此处只负责等待后切回复检。
            countdown = max(1, self.restore_delay.get())
            while countdown > 0 and self.running and self._alt_tab_cycle_active:
                self.root.after(0, lambda sec=countdown: self.status.config(text=f"状态：Alt+Tab 冷却中，{sec} 秒后复检"))
                self.root.after(0, lambda sec=countdown: self._append_log(f"Alt+Tab 冷却倒计时：{sec} 秒"))
                time.sleep(1)
                countdown -= 1

            if not self.running or not self._alt_tab_cycle_active:
                break

            self.root.after(0, lambda: self.switch_to_recent_window("倒计时结束，切回原窗口复检"))
            time.sleep(0.2)
            red_count, ratio = self._get_red_stats(self.region)
            has_enough_red = red_count >= max(1, self.min_red_pixels.get())
            self.root.after(0, lambda: self.status.config(text=f"状态：还原后复检 红点 {red_count}px，占比 {ratio:.4f}"))
            self.root.after(0, lambda: self._append_log(f"还原后复检：{red_count}px，占比 {ratio:.4f}"))

            if has_enough_red:
                saved_path = self.capture_target_region()
                if saved_path:
                    self.root.after(0, lambda: self._append_log(f"区域截图已保存：{saved_path}"))
                self.root.after(0, lambda: self.switch_to_recent_window("复检仍有红点，立即切换到另一窗口"))
                continue

            self._alt_tab_cycle_active = False
            self.root.after(0, lambda: self.status.config(text="状态：还原后红点已消失"))
            self.root.after(0, lambda: self._append_log("还原后红点已消失，结束 Alt+Tab 循环"))


if __name__ == "__main__":
    set_dpi_awareness()
    root = tk.Tk()
    app = RedMonitorApp(root)
    root.mainloop()
