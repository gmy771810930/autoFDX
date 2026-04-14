import tkinter as tk
from tkinter import ttk
import ctypes
from pathlib import Path
from time import monotonic, sleep

import cv2
import numpy as np
import pyautogui

from .config import CALIBRATION_ITEMS
from .config import PROJECT_ROOT
from .vision_service import sample_hsv_profile

BG_APP = "#0f172a"
BG_CARD = "#111827"
BG_SUBCARD = "#1f2937"
FG_MAIN = "#e5e7eb"
FG_MUTED = "#9ca3af"
ACCENT = "#2563eb"
BTN_BG = "#334155"
BTN_HOVER = "#475569"
BTN_DANGER = "#b83b3b"
BTN_SUCCESS = "#3aa655"


class CalibrationOverlay:
    """单项标定层：按标定类型绘制矩形或圆点。"""

    def __init__(self, root, config_store, state, window_service):
        self.root = root
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.win = None
        self.canvas = None
        self.item_key = None
        self.item_label = None
        self.rect = [50, 50, 150, 120]
        self.point = [100, 100]
        self.point_radius = 8
        self.drag_mode = None
        self.last_xy = (0, 0)
        self.game_left = self.game_top = self.game_width = self.game_height = 0
        self.edge_threshold = 12

    @property
    def config(self):
        return self.config_store.data

    def is_open(self):
        return self.win is not None and self.win.winfo_exists()

    def _is_like_item(self):
        return bool(self.item_key and self.item_key.startswith("like"))

    def open_for_item(self, item_key, item_label):
        if self.is_open():
            self.cancel()
        self.item_key = item_key
        self.item_label = item_label

        left, top, width, height = self.window_service.get_window_region()
        self.game_left, self.game_top, self.game_width, self.game_height = left, top, width, height
        r = self.config["calibration_rects"].get(item_key, [0.4, 0.4, 0.5, 0.5])
        self.rect = [int(r[0] * width), int(r[1] * height), int(r[2] * width), int(r[3] * height)]
        if self._is_like_item():
            # 点赞点位标定改为“单圆点模式”：
            # 兼容历史矩形数据，默认取矩形中心作为初始圆点位置。
            cx = int((self.rect[0] + self.rect[2]) / 2)
            cy = int((self.rect[1] + self.rect[3]) / 2)
            self.point = [cx, cy]
            self.rect = [cx - 1, cy - 1, cx + 1, cy + 1]

        self.win = tk.Toplevel(self.root)
        self.win.title(f"标定: {item_label}")
        # 去掉系统标题栏与边框，避免窗口装饰造成坐标偏移。
        # 这样 canvas 坐标可与游戏窗口像素坐标一一对应。
        self.win.overrideredirect(True)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG_APP)
        self.win.attributes("-alpha", 0.35)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        # 让回车可直接应用，Esc 直接取消，提升标定操作效率。
        self.win.bind("<Return>", lambda _e: self.save())
        self.win.bind("<Escape>", lambda _e: self.cancel())
        self.win.focus_force()

        panel = tk.Frame(self.win, bg=BG_SUBCARD)
        panel.place(x=10, y=10)
        tk.Button(panel, text="保存", command=self.save, bg=ACCENT, fg="white", relief="flat").pack(side="left", padx=4, pady=4)
        tk.Button(panel, text="取消", command=self.cancel, bg=BTN_DANGER, fg="white", relief="flat").pack(side="left", padx=4, pady=4)
        tip_text = "左键点击/拖动圆点" if self._is_like_item() else "左键拖动/缩放"
        tk.Label(panel, text=f"正在标定：{item_label}（{tip_text}）", fg=FG_MAIN, bg=BG_SUBCARD).pack(
            side="left", padx=6
        )

        self.canvas = tk.Canvas(self.win, bg=BG_APP, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.redraw()
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Motion>", self.on_motion)

        self.state.manual_pause = True
        self.state.set_status(f"标定中: {item_label}")

    def redraw(self):
        self.canvas.delete("all")
        if self._is_like_item():
            # 点赞项使用圆点可视化，仅表达“点击坐标”，不再表达区域范围。
            cx, cy = self.point
            r = self.point_radius
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#00ff88", width=2)
            self.canvas.create_line(cx - 14, cy, cx + 14, cy, fill="#00ff88", width=1)
            self.canvas.create_line(cx, cy - 14, cx, cy + 14, fill="#00ff88", width=1)
            self.canvas.create_text(
                cx + 12,
                cy - 12,
                anchor="nw",
                text=self.item_label,
                fill="yellow",
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            return

        x1, y1, x2, y2 = self.rect
        self.canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff88", width=2)
        self.canvas.create_text(
            x1 + 8,
            y1 + 8,
            anchor="nw",
            text=self.item_label,
            fill="yellow",
            font=("Microsoft YaHei UI", 11, "bold"),
        )

    def clamp(self):
        if self._is_like_item():
            # 圆点模式只需约束单点坐标，避免越界到游戏窗口外。
            px, py = self.point
            px = max(0, min(self.game_width - 1, px))
            py = max(0, min(self.game_height - 1, py))
            self.point = [px, py]
            self.rect = [px - 1, py - 1, px + 1, py + 1]
            return

        x1, y1, x2, y2 = self.rect
        x1 = max(0, min(self.game_width - 2, x1))
        y1 = max(0, min(self.game_height - 2, y1))
        x2 = max(x1 + 2, min(self.game_width, x2))
        y2 = max(y1 + 2, min(self.game_height, y2))
        self.rect = [x1, y1, x2, y2]

    def in_rect(self, x, y):
        x1, y1, x2, y2 = self.rect
        return x1 <= x <= x2 and y1 <= y <= y2

    def get_drag_mode(self, x, y):
        x1, y1, x2, y2 = self.rect
        t = self.edge_threshold
        near_left = abs(x - x1) <= t
        near_right = abs(x - x2) <= t
        near_top = abs(y - y1) <= t
        near_bottom = abs(y - y2) <= t
        inside = self.in_rect(x, y)

        if near_left and near_top:
            return "nw"
        if near_right and near_top:
            return "ne"
        if near_left and near_bottom:
            return "sw"
        if near_right and near_bottom:
            return "se"
        if near_left and inside:
            return "w"
        if near_right and inside:
            return "e"
        if near_top and inside:
            return "n"
        if near_bottom and inside:
            return "s"
        if inside:
            return "move"
        return None

    def on_left_down(self, event):
        if self._is_like_item():
            # 圆点模式：按下即更新目标点并进入拖动。
            self.drag_mode = "point"
            self.point = [event.x, event.y]
            self.clamp()
            self.redraw()
            return

        mode = self.get_drag_mode(event.x, event.y)
        if mode is None:
            return
        self.drag_mode = mode
        self.last_xy = (event.x, event.y)

    def on_left_move(self, event):
        if self.drag_mode is None:
            return

        if self._is_like_item() and self.drag_mode == "point":
            self.point = [event.x, event.y]
            self.clamp()
            self.redraw()
            return

        dx, dy = event.x - self.last_xy[0], event.y - self.last_xy[1]
        x1, y1, x2, y2 = self.rect

        if self.drag_mode == "move":
            self.rect = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
        else:
            if "w" in self.drag_mode:
                x1 += dx
            if "e" in self.drag_mode:
                x2 += dx
            if "n" in self.drag_mode:
                y1 += dy
            if "s" in self.drag_mode:
                y2 += dy
            self.rect = [x1, y1, x2, y2]

        self.last_xy = (event.x, event.y)
        self.clamp()
        self.redraw()

    def on_left_up(self, _event):
        self.drag_mode = None

    def on_motion(self, event):
        if self._is_like_item():
            self.canvas.configure(cursor="crosshair")
            return

        mode = self.get_drag_mode(event.x, event.y)
        cursor_map = {
            "move": "fleur",
            "n": "sb_v_double_arrow",
            "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow",
            "w": "sb_h_double_arrow",
            "ne": "top_right_corner",
            "sw": "bottom_left_corner",
            "nw": "top_left_corner",
            "se": "bottom_right_corner",
        }
        self.canvas.configure(cursor=cursor_map.get(mode, "arrow"))

    def save(self):
        x1, y1, x2, y2 = self.rect
        norm = [x1 / self.game_width, y1 / self.game_height, x2 / self.game_width, y2 / self.game_height]
        if self._is_like_item():
            # 点赞项只保留单点：为了复用既有数据结构，写为零面积矩形 [x,y,x,y]。
            # 后续计算中心点时仍可得到准确点击坐标。
            self.clamp()
            px, py = self.point
            nx = px / self.game_width
            ny = py / self.game_height
            norm = [nx, ny, nx, ny]
        self.config["calibration_rects"][self.item_key] = norm

        # 只有需要图像的标定项才截图；点赞点位和滚动区域仅保存坐标，不截图。
        need_screenshot = self.item_key in ("start", "cum2", "finish", "bar_female", "bar_male")
        screenshot = None
        if need_screenshot:
            # 截图前先临时隐藏标定层，避免把绘制矩形、提示文字、按钮一起截进去。
            # 该步骤可确保模板图像纯净且与实际游戏画面一致。
            self.win.withdraw()
            self.win.update_idletasks()
            self.win.update()
            sleep(0.05)
            screenshot = cv2.cvtColor(
                np.array(
                    pyautogui.screenshot(region=(self.game_left, self.game_top, self.game_width, self.game_height))
                ),
                cv2.COLOR_RGB2BGR,
            )

        # 模板按钮：保存模板图 + 限定匹配区域
        if self.item_key in ("start", "cum2", "finish"):
            crop = screenshot[y1:y2, x1:x2]
            if crop.size > 0:
                file_name = f"custom_{self.item_key}.png"
                rel_path = str(Path("assets") / "templates" / file_name)
                cv2.imwrite(str(PROJECT_ROOT / rel_path), crop)
                self.config["custom_templates"][self.item_key] = rel_path
                self.config["template_regions"][self.item_key] = norm
        # 女进度条：独立保存区域与颜色采样
        elif self.item_key == "bar_female":
            self.config["bar_regions"]["bar1"] = norm
            c1 = screenshot[y1:y2, x1:x2]
            if c1.size > 0:
                self.config["bar_profiles"]["bar1"] = sample_hsv_profile(c1)
        # 男进度条：独立保存区域与颜色采样
        elif self.item_key == "bar_male":
            self.config["bar_regions"]["bar2"] = norm
            c2 = screenshot[y1:y2, x1:x2]
            if c2.size > 0:
                self.config["bar_profiles"]["bar2"] = sample_hsv_profile(c2)
        elif self.item_key == "scroll_area":
            self.config["scroll_region"] = norm
        elif self.item_key.startswith("like"):
            points = []
            for idx in range(1, 7):
                rr = self.config["calibration_rects"][f"like{idx}"]
                points.append([(rr[0] + rr[2]) / 2, (rr[1] + rr[3]) / 2])
            self.config["like_points"] = points

        self.config["calibration_done"][self.item_key] = True
        self.config_store.save()
        # 标定完成后不自动恢复，等待用户点“继续运行”再进入自动流程。
        self.state.calibration_updated = True
        self.state.manual_pause = True
        self.state.set_status(f"已应用标定: {self.item_label}（等待继续运行）")
        self.win.destroy()
        self.win = None

    def cancel(self):
        self.state.manual_pause = False
        self.state.set_status("取消标定")
        if self.is_open():
            self.win.destroy()
        self.win = None


def launch_floating_window(config_store, state, window_service):
    """悬浮窗入口：二级菜单逐项标定 + 颜色状态。"""

    root = tk.Tk()
    root.title("autoFDX 悬浮窗")
    # 根据屏幕自适应窗口宽度，保证按钮显示完整又不会超出屏幕。
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = min(980, max(760, screen_w - 80))
    saved_pos = config_store.data.get("ui_window_pos", [20, 20])
    if not isinstance(saved_pos, list) or len(saved_pos) != 2:
        saved_pos = [20, 20]
    win_x = int(saved_pos[0])
    win_y = int(saved_pos[1])
    # 启动时做一次边界约束，防止历史坐标落在屏幕外。
    win_x = max(0, min(max(0, screen_w - 220), win_x))
    win_y = max(0, min(max(0, screen_h - 140), win_y))
    win_h_expanded = 460
    win_h_collapsed = 280
    root.geometry(f"{win_w}x{win_h_collapsed}+{win_x}+{win_y}")
    # 使用内嵌窗口控制按钮，隐藏系统边框与标题栏。
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=BG_APP)
    like_chk_style = ttk.Style(root)
    # 优先使用 Windows 主题，按钮外观更接近圆角。
    if "vista" in like_chk_style.theme_names():
        like_chk_style.theme_use("vista")
    like_chk_style.configure(
        "Like.Big.TCheckbutton",
        font=("Microsoft YaHei UI", 10),
        indicatorsize=24,
        padding=(8, 6),
        background=BG_CARD,
        foreground=FG_MAIN,
    )
    like_chk_style.map(
        "Like.Big.TCheckbutton",
        background=[("active", BG_CARD), ("disabled", BG_CARD), ("!disabled", BG_CARD)],
        foreground=[("disabled", FG_MUTED), ("!disabled", FG_MAIN)],
    )

    status_var = tk.StringVar(value="流程: init")
    init_run_text = "已暂停" if state.manual_pause else "运行中"
    run_var = tk.StringVar(value=init_run_text)
    status_line_var = tk.StringVar(value=f"流程: init  |  状态: {init_run_text}")
    like_enabled_var = tk.BooleanVar(value=bool(config_store.data.get("like_enabled", True)))
    like_force_next_var = tk.BooleanVar(value=bool(config_store.data.get("like_force_next", False)))
    overlay_dx_var = tk.BooleanVar(value=bool(config_store.data.get("overlay_dx_hook_enabled", False)))
    overlay = CalibrationOverlay(root, config_store, state, window_service)
    calib_buttons = {}
    label_map = {k: t for k, t in CALIBRATION_ITEMS}
    save_pos_after_id = None
    is_pinned_topmost = True
    drag_start_x = 0
    drag_start_y = 0
    overlay_last_applied = None

    def persist_window_pos():
        pos = [int(root.winfo_x()), int(root.winfo_y())]
        if config_store.data.get("ui_window_pos") != pos:
            config_store.data["ui_window_pos"] = pos
            config_store.save()

    def schedule_persist_window_pos():
        nonlocal save_pos_after_id
        if save_pos_after_id is not None:
            root.after_cancel(save_pos_after_id)
        # 防抖保存，避免拖动窗口时频繁写文件。
        save_pos_after_id = root.after(250, persist_window_pos)

    def on_root_configure(event):
        if event.widget is root:
            schedule_persist_window_pos()

    def on_title_press(event):
        nonlocal drag_start_x, drag_start_y
        drag_start_x = event.x_root
        drag_start_y = event.y_root

    def on_title_drag(event):
        # 固定状态下不允许拖动，避免误移动影响匹配与点击区域。
        if is_pinned_topmost:
            return
        dx = event.x_root - drag_start_x
        dy = event.y_root - drag_start_y
        new_x = int(root.winfo_x() + dx)
        new_y = int(root.winfo_y() + dy)
        root.geometry(f"{win_w}x{root.winfo_height()}+{new_x}+{new_y}")
        on_title_press(event)

    def refresh_pin_button():
        # 悬浮按钮：控制是否保持悬浮显示。
        if is_pinned_topmost:
            btn_win_pin.set_text("悬浮中")
        else:
            btn_win_pin.set_text("悬浮")

    def on_toggle_pin():
        nonlocal is_pinned_topmost
        is_pinned_topmost = not is_pinned_topmost
        apply_overlay_mode(force=True)
        refresh_pin_button()

    def apply_overlay_mode(force=False):
        """
        DX Overlay（实验）实现：
        - 通过 Win32 扩展样式强化悬浮层级（layered/toolwindow/no-redirection）；
        - 使用 SetWindowPos + NOACTIVATE 降低抢焦点概率；
        - 失败时回退到常规 topmost 设置。
        """
        nonlocal overlay_last_applied
        enabled = bool(overlay_dx_var.get())
        desired_state = (enabled, is_pinned_topmost)
        if (not force) and overlay_last_applied == desired_state:
            return

        try:
            if root.winfo_exists() == 0:
                return
            root.update_idletasks()
            hwnd = int(root.winfo_id())
            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOREDIRECTIONBITMAP = 0x00200000
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            LWA_ALPHA = 0x00000002

            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enabled:
                exstyle |= WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOREDIRECTIONBITMAP
            else:
                exstyle &= ~WS_EX_NOREDIRECTIONBITMAP
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)

            # 维持可见度：使用分层窗口不透明 alpha。
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)

            top_hwnd = HWND_TOPMOST if is_pinned_topmost else HWND_NOTOPMOST
            user32.SetWindowPos(
                hwnd,
                top_hwnd,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            root.attributes("-topmost", is_pinned_topmost)
            overlay_last_applied = desired_state
        except Exception:
            # 回退常规模式，保证功能可用。
            root.attributes("-topmost", is_pinned_topmost)
            overlay_last_applied = desired_state

    def pause_button_text():
        # 约定：暂停中显示“继续”，运行中显示“暂停”。
        return "继续" if state.manual_pause else "暂停"

    def apply_pause_ui_text():
        run_var.set("已暂停" if state.manual_pause else "运行中")
        btn_pause.set_text(pause_button_text())
        # 继续=绿色，暂停=红色，便于一眼识别当前可执行动作。
        if state.manual_pause:
            btn_pause.set_style(normal_bg=BTN_SUCCESS, hover_bg="#2f8f4a", fg="white")
        else:
            btn_pause.set_style(normal_bg=BTN_DANGER, hover_bg="#dc2626", fg="white")
        status_line_var.set(f"流程: {state.current_status}  |  状态: {run_var.get()}")

    def toggle_pause():
        state.manual_pause = not state.manual_pause
        if state.manual_pause:
            state.set_status("手动暂停")
        else:
            # 点击继续运行时，清除“新标定待应用”标记；运行逻辑会直接使用最新配置。
            if state.calibration_updated:
                state.calibration_updated = False
                state.set_status("使用新标定运行中")
        apply_pause_ui_text()

    def on_exit():
        state.stop_requested = True
        if overlay.is_open():
            overlay.cancel()
        persist_window_pos()
        # 先 quit 再 destroy，确保 mainloop 干净退出。
        root.quit()
        root.destroy()

    def toggle_submenu():
        cur_x = int(root.winfo_x())
        cur_y = int(root.winfo_y())
        if submenu_host.winfo_ismapped():
            submenu_host.pack_forget()
            # 标定面板收起时同步缩短窗口高度，让下边沿回收。
            root.geometry(f"{win_w}x{win_h_collapsed}+{cur_x}+{cur_y}")
        else:
            submenu_host.pack(fill="both", expand=True, pady=(4, 0))
            # 展开标定面板时恢复完整高度。
            root.geometry(f"{win_w}x{win_h_expanded}+{cur_x}+{cur_y}")

    def trigger_item(item_key):
        state.pending_calibration = item_key

    def refresh_button_colors():
        done_map = config_store.data.get("calibration_done", {})
        for key, _ in CALIBRATION_ITEMS:
            btn = calib_buttons[key]
            if done_map.get(key, False):
                btn._normal_bg = BTN_SUCCESS
                btn._hover_bg = "#2f8f4a"
                btn.configure(bg=btn._normal_bg, fg="white", activebackground=btn._hover_bg)
            else:
                btn._normal_bg = BTN_DANGER
                btn._hover_bg = "#dc2626"
                btn.configure(bg=btn._normal_bg, fg="white", activebackground=btn._hover_bg)

    def style_button(btn, normal_bg=BTN_BG, hover_bg=BTN_HOVER):
        btn._normal_bg = normal_bg
        btn._hover_bg = hover_bg
        btn.configure(
            bg=btn._normal_bg,
            fg=FG_MAIN,
            activebackground=btn._hover_bg,
            activeforeground=FG_MAIN,
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10),
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=btn._hover_bg))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=btn._normal_bg))

    def create_round_button(
        parent,
        text,
        command,
        width,
        height,
        normal_bg=BTN_BG,
        hover_bg=BTN_HOVER,
        fg=FG_MAIN,
        radius=12,
        font=("Microsoft YaHei UI", 10),
    ):
        """Canvas 圆角按钮，避免系统主题导致白底与方角。"""
        holder = tk.Frame(parent, bg=parent.cget("bg"))
        canvas = tk.Canvas(holder, width=width, height=height, bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        canvas.pack()
        state = {"text": text, "normal_bg": normal_bg, "hover_bg": hover_bg, "fg": fg, "last_click_ts": 0.0}

        def _rounded_points(w, h, r):
            return [
                r,
                0,
                w - r,
                0,
                w,
                0,
                w,
                r,
                w,
                h - r,
                w,
                h,
                w - r,
                h,
                r,
                h,
                0,
                h,
                0,
                h - r,
                0,
                r,
                0,
                0,
            ]

        def redraw(fill):
            canvas.delete("all")
            canvas.create_polygon(
                _rounded_points(width, height, min(radius, width // 2, height // 2)),
                smooth=True,
                splinesteps=36,
                fill=fill,
                outline=fill,
            )
            canvas.create_text(width // 2, height // 2, text=state["text"], fill=state["fg"], font=font)

        def set_text(new_text):
            # 文案未变化时不重绘，避免 refresh 周期引发视觉抖动。
            if state["text"] == new_text:
                return
            state["text"] = new_text
            redraw(state["normal_bg"])

        def set_style(normal_bg=None, hover_bg=None, fg=None):
            """动态更新按钮配色，用于状态按钮颜色切换。"""
            if normal_bg is not None:
                state["normal_bg"] = normal_bg
            if hover_bg is not None:
                state["hover_bg"] = hover_bg
            if fg is not None:
                state["fg"] = fg
            redraw(state["normal_bg"])

        def on_enter(_e):
            redraw(state["hover_bg"])

        def on_leave(_e):
            redraw(state["normal_bg"])

        def on_click(_e):
            # 按钮点击防抖：抑制系统/鼠标抖动造成的重复触发。
            now = monotonic()
            if now - state["last_click_ts"] < 0.25:
                return
            state["last_click_ts"] = now
            command()

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", on_click)
        redraw(state["normal_bg"])
        holder.set_text = set_text
        holder.set_style = set_style
        return holder

    def refresh_like_force_state():
        # 仅在启用点赞功能时允许“立即执行点赞”勾选。
        force_state = "normal" if like_enabled_var.get() else "disabled"
        chk_like_force.configure(state=force_state)
        if not like_enabled_var.get():
            like_force_next_var.set(False)

    def on_like_enabled_toggle():
        enabled = bool(like_enabled_var.get())
        config_store.data["like_enabled"] = enabled
        if not enabled:
            config_store.data["like_force_next"] = False
            like_force_next_var.set(False)
        config_store.save()
        refresh_like_force_state()

    def on_like_force_next_toggle():
        # 关闭点赞功能时不允许设置“立即执行点赞”。
        if not like_enabled_var.get():
            like_force_next_var.set(False)
            return
        force_next = bool(like_force_next_var.get())
        config_store.data["like_force_next"] = force_next
        # 仅记录“下一次流程结束立即点赞”意图。
        # 点赞计数清零延后到“实际执行点赞后”再做，确保时序与业务一致。
        config_store.save()

    def on_overlay_dx_toggle():
        # 仅提供“用户可选”开关，便于后续接入 DX Hook/Overlay 实现。
        enabled = bool(overlay_dx_var.get())
        config_store.data["overlay_dx_hook_enabled"] = enabled
        config_store.save()
        apply_overlay_mode(force=True)
        if enabled:
            state.set_status("已启用全屏模式兼容")
        else:
            state.set_status("已切换常规悬浮模式")

    frame = tk.Frame(root, padx=14, pady=14, bg=BG_APP)
    frame.pack(fill="both", expand=True)
    header_card = tk.Frame(frame, bg=BG_CARD, padx=10, pady=8)
    header_card.pack(fill="x", pady=(0, 10))
    title_row = tk.Frame(header_card, bg=BG_CARD)
    title_row.pack(fill="x")
    lbl_title = tk.Label(title_row, text="autoFDX 控制台", fg=FG_MAIN, bg=BG_CARD, font=("Microsoft YaHei UI", 11, "bold"))
    lbl_title.pack(side="left", anchor="w")
    tk.Label(
        title_row,
        text="按“F1”键紧急暂停",
        fg=FG_MUTED,
        bg=BG_CARD,
        font=("Microsoft YaHei UI", 9),
    ).pack(side="left", padx=(10, 0), anchor="w")
    win_btn_host = tk.Frame(title_row, bg=BG_CARD)
    # 内嵌窗口控制按钮贴靠标题区右上角。
    win_btn_host.place(relx=1.0, x=0, y=0, anchor="ne")
    chk_overlay_dx = ttk.Checkbutton(
        win_btn_host,
        text="全屏模式兼容",
        variable=overlay_dx_var,
        command=on_overlay_dx_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    btn_win_pin = create_round_button(
        win_btn_host,
        text="悬浮中",
        command=on_toggle_pin,
        width=100,
        height=32,
        normal_bg=BTN_BG,
        hover_bg=BTN_HOVER,
        radius=12,
        font=("Microsoft YaHei UI", 10, "bold"),
    )
    btn_win_close = create_round_button(
        win_btn_host,
        text="✕",
        command=on_exit,
        width=42,
        height=32,
        normal_bg=BTN_DANGER,
        hover_bg="#dc2626",
        fg="white",
        radius=12,
        font=("Microsoft YaHei UI", 11, "bold"),
    )
    chk_overlay_dx.pack(side="left", padx=(0, 8))
    for btn in (btn_win_pin, btn_win_close):
        btn.pack(side="left", padx=2)
    refresh_pin_button()
    # 自定义标题栏拖动窗口。
    for w in (title_row, lbl_title):
        w.bind("<ButtonPress-1>", on_title_press)
        w.bind("<B1-Motion>", on_title_drag)
    # 将“流程/状态”与右侧勾选区改为双列固定布局：
    # 左列状态文本自适应，右列控件固定靠右，避免状态字数变化导致右侧位移。
    status_row = tk.Frame(header_card, bg=BG_CARD)
    status_row.pack(fill="x", pady=(3, 0))
    status_row.grid_columnconfigure(0, weight=1)
    status_row.grid_columnconfigure(1, weight=0)
    status_text_label = tk.Label(
        status_row,
        textvariable=status_line_var,
        fg="#93c5fd",
        bg=BG_CARD,
        font=("Microsoft YaHei UI", 10),
        anchor="w",
    )
    status_text_label.grid(row=0, column=0, sticky="w")

    # 主控制按钮横向排列，避免纵向堆叠占用空间。
    top_btn_frame = tk.Frame(frame, bg=BG_APP)
    top_btn_frame.pack(fill="x", pady=(0, 8))
    btn_pause = create_round_button(
        top_btn_frame,
        pause_button_text(),
        toggle_pause,
        168,
        42,
        BTN_SUCCESS if state.manual_pause else BTN_DANGER,
        "#2f8f4a" if state.manual_pause else "#dc2626",
        radius=16,
        font=("Microsoft YaHei UI", 11, "bold"),
        fg="white",
    )
    btn_menu = create_round_button(
        top_btn_frame,
        "自定义标定",
        toggle_submenu,
        168,
        42,
        BTN_BG,
        BTN_HOVER,
        radius=16,
        font=("Microsoft YaHei UI", 11, "bold"),
    )
    btn_exit = create_round_button(
        top_btn_frame,
        "退出脚本",
        on_exit,
        168,
        42,
        BTN_DANGER,
        "#dc2626",
        fg="white",
        radius=16,
        font=("Microsoft YaHei UI", 11, "bold"),
    )
    for btn in (btn_pause, btn_menu, btn_exit):
        btn.pack(side="left", padx=4)

    # 点赞控制开关移到状态区右侧，减少纵向占位。
    like_option_frame = tk.Frame(status_row, bg=BG_CARD)
    like_option_frame.grid(row=0, column=1, sticky="e")
    chk_like_enabled = ttk.Checkbutton(
        like_option_frame,
        text="启用点赞功能",
        variable=like_enabled_var,
        command=on_like_enabled_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_like_enabled.pack(side="left", padx=(8, 4))
    chk_like_force = ttk.Checkbutton(
        like_option_frame,
        text="结束后执行点赞",
        variable=like_force_next_var,
        command=on_like_force_next_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_like_force.pack(side="left", padx=(4, 0))
    refresh_like_force_state()

    submenu_host = tk.Frame(frame, bg=BG_APP)
    # 使用“无可见滚动条”模式：避免滚动条占位影响可视区域，滚动靠鼠标滚轮完成。
    submenu_canvas = tk.Canvas(submenu_host, bg=BG_APP, highlightthickness=0, bd=0, height=250)
    submenu_canvas.pack(side="left", fill="both", expand=True)

    submenu_frame = tk.LabelFrame(
        submenu_canvas,
        text="标定项（绿=已标定，红=未标定）",
        padx=8,
        pady=8,
        bg=BG_CARD,
        fg=FG_MUTED,
        font=("Microsoft YaHei UI", 10),
    )
    submenu_canvas_window = submenu_canvas.create_window((0, 0), window=submenu_frame, anchor="nw")

    def _on_submenu_configure(_event):
        submenu_canvas.configure(scrollregion=submenu_canvas.bbox("all"))
        submenu_canvas.itemconfigure(submenu_canvas_window, width=submenu_canvas.winfo_width())

    submenu_frame.bind("<Configure>", _on_submenu_configure)
    submenu_canvas.bind("<Configure>", _on_submenu_configure)

    def _on_mousewheel(event):
        # 仅在二级菜单展开时处理滚轮，避免影响其他区域。
        if not submenu_host.winfo_ismapped():
            return
        step = int(-event.delta / 120) if event.delta != 0 else 0
        if step != 0:
            submenu_canvas.yview_scroll(step, "units")

    def _on_shift_mousewheel(event):
        if not submenu_host.winfo_ismapped():
            return
        step = int(-event.delta / 120) if event.delta != 0 else 0
        if step != 0:
            submenu_canvas.xview_scroll(step, "units")

    # 使用全局绑定，避免鼠标位于子控件时滚轮事件丢失导致“滚不动”。
    root.bind_all("<MouseWheel>", _on_mousewheel, add="+")
    root.bind_all("<Shift-MouseWheel>", _on_shift_mousewheel, add="+")
    # 标定按钮横向网格排布：优先 4 列，空间不足时退化为 3 列。
    column_count = 4 if win_w >= 900 else 3
    for key, label in CALIBRATION_ITEMS:
        idx = len(calib_buttons)
        row = idx // column_count
        col = idx % column_count
        btn = tk.Button(submenu_frame, text=label, width=12, command=lambda k=key: trigger_item(k))
        style_button(btn, normal_bg=BTN_DANGER, hover_bg="#dc2626")
        btn.grid(row=row, column=col, padx=4, pady=4, sticky="ew")
        calib_buttons[key] = btn

    for col in range(column_count):
        submenu_frame.grid_columnconfigure(col, weight=1)

    root.bind("<Configure>", on_root_configure, add="+")
    root.protocol("WM_DELETE_WINDOW", on_exit)
    apply_overlay_mode(force=True)

    def refresh():
        status_var.set(f"流程: {state.current_status}")
        apply_pause_ui_text()
        # 不再周期性强制置顶，避免和游戏窗口抢焦点。
        # 悬浮状态仅在按钮切换时生效。
        # 与后台流程保持一致：若“立即执行点赞”已被消费，UI 勾选随之回收。
        cfg_force = bool(config_store.data.get("like_force_next", False))
        if like_force_next_var.get() != cfg_force:
            like_force_next_var.set(cfg_force)
        cfg_enabled = bool(config_store.data.get("like_enabled", True))
        if like_enabled_var.get() != cfg_enabled:
            like_enabled_var.set(cfg_enabled)
        cfg_overlay_dx = bool(config_store.data.get("overlay_dx_hook_enabled", False))
        if overlay_dx_var.get() != cfg_overlay_dx:
            overlay_dx_var.set(cfg_overlay_dx)
            apply_overlay_mode(force=True)
        refresh_like_force_state()
        refresh_button_colors()
        if state.pending_calibration is not None:
            key = state.pending_calibration
            state.pending_calibration = None
            overlay.open_for_item(key, label_map[key])
        root.after(250, refresh)

    refresh()
    root.mainloop()
