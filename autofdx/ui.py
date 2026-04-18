import tkinter as tk
from tkinter import font as tkfont
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
# 自定义标题栏区域：与主内容区背景区分，便于一眼分辨“可拖动标题区 / 内容区”
HEADER_BAR_BG = "#0b1224"
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
        # “拉出新实验滚动”专用输入：向下滚动距离（步数）。
        self.scroll_distance_var = None
        self.drag_mode = None
        self.last_xy = (0, 0)
        self.game_left = self.game_top = self.game_width = self.game_height = 0
        self.edge_threshold = 12
        # 女/男进度条：与「敏感进度条」同宽同高、横坐标对齐，仅允许整体上下平移；见 open_for_item / clamp。
        self._bar_lock = None

    @property
    def config(self):
        return self.config_store.data

    def is_open(self):
        return self.win is not None and self.win.winfo_exists()

    def _is_like_item(self):
        return bool(self.item_key and self.item_key.startswith("like"))

    def _is_pull_new_experiment_scroll_item(self):
        return self.item_key == "pull_new_experiment_scroll"

    def _is_point_mode_item(self):
        """
        点位模式项：
        - 点赞项：只标定点击点
        - 拉出新实验滚动：标定滚轮执行点
        """
        return self._is_like_item() or self._is_pull_new_experiment_scroll_item()

    def _is_experiment_switch_item(self):
        return self.item_key == "experiment_switch"

    def _is_body_part_item(self):
        return self.item_key == "body_part_switch"

    def _build_experiment_grid_points(self):
        """
        基于当前矩形生成 3x4 联合网格点（像素坐标）。
        约定：
        - 横向 4 列（0~3），纵向 3 行（0~2）
        - 从左到右、从上到下编号为 1~12
        """
        x1, y1, x2, y2 = self.rect
        points = []
        for row in range(3):
            y = y1 + (y2 - y1) * (row / 2.0)
            for col in range(4):
                x = x1 + (x2 - x1) * (col / 3.0)
                points.append([int(round(x)), int(round(y))])
        return points

    def _build_body_part_points(self):
        """
        生成“身体部位”单行 7 点中心坐标（像素坐标）。
        点位按从左到右编号 1~7，对应顶部身体部位按钮序列。
        """
        x1, y1, x2, y2 = self.rect
        cy = int(round((y1 + y2) / 2.0))
        points = []
        for col in range(7):
            x = x1 + (x2 - x1) * (col / 6.0)
            points.append([int(round(x)), cy])
        return points

    def _estimate_pull_scroll_distance_from_experiment_points(self, game_height):
        """
        基于“实验卡片 3x4 标定点”估算“向下滚动距离”默认值。

        估算思路（尽量稳健）：
        1) 实验卡片固定为 3 行：1~4、5~8、9~12；
        2) 分别计算三行 y 的均值（归一化 -> 像素）；
        3) 使用“两两行距”推导单行步长：
           - d12 = row2-row1
           - d23 = row3-row2
           - d13/2 作为跨两行的折算步长
        4) 取三者平均得到目标“单行像素位移”；
        5) 按当前滚轮标定比例（1档=10滚轮单位）换算默认档位。

        说明：
        - 这是“默认值估算”，用于减少手工试错，不覆盖用户已保存的手填值。
        - 若点位不完整或异常，返回 0 让输入框保持可手填。
        """
        points = self.config.get("experiment_points", [])
        if (not isinstance(points, list)) or len(points) != 12:
            return 0

        row_y_means = []
        for row_idx in range(3):
            row = points[row_idx * 4 : row_idx * 4 + 4]
            ys = []
            for p in row:
                if (not isinstance(p, list)) or len(p) != 2:
                    return 0
                y_norm = p[1]
                if not isinstance(y_norm, (int, float)):
                    return 0
                ys.append(float(y_norm) * float(game_height))
            row_y_means.append(sum(ys) / 4.0)

        d12 = abs(row_y_means[1] - row_y_means[0])
        d23 = abs(row_y_means[2] - row_y_means[1])
        d13_half = abs(row_y_means[2] - row_y_means[0]) / 2.0
        step_px = (d12 + d23 + d13_half) / 3.0
        if step_px <= 0.0:
            return 0

        # 当前实现按“1档=10滚轮单位”执行，因此默认值也按同一比例换算。
        estimated = int(round(step_px / 10.0))
        return max(1, estimated)

    def _build_center_rect_by_item(self, width, height):
        """
        为“未标定项”生成居中默认框，避免默认框落在边缘导致用户看不到。
        按标定类型给出不同尺寸，保证可见性与可操作性。
        """
        if self._is_experiment_switch_item():
            rw, rh = 0.45, 0.35
        elif self._is_body_part_item():
            rw, rh = 0.60, 0.16
        else:
            rw, rh = 0.20, 0.14

        w = max(20, int(width * rw))
        h = max(20, int(height * rh))
        cx, cy = width // 2, height // 2
        x1 = max(0, cx - w // 2)
        y1 = max(0, cy - h // 2)
        x2 = min(width, x1 + w)
        y2 = min(height, y1 + h)
        return [x1, y1, x2, y2]

    def open_for_item(self, item_key, item_label):
        if self.is_open():
            self.cancel()
        self.item_key = item_key
        self.item_label = item_label
        self.scroll_distance_var = None
        # 女/男条锁定状态在下方按敏感进度条结果设置；未进入锁定时必须为 None。
        self._bar_lock = None

        left, top, width, height = self.window_service.get_window_region()
        self.game_left, self.game_top, self.game_width, self.game_height = left, top, width, height
        r = self.config["calibration_rects"].get(item_key, [0.4, 0.4, 0.5, 0.5])
        self.rect = [int(r[0] * width), int(r[1] * height), int(r[2] * width), int(r[3] * height)]
        # 未完成标定时，统一以屏幕中心作为默认框位置，避免默认框在边缘不可见。
        done_map = self.config.get("calibration_done", {})
        if not bool(done_map.get(item_key, False)):
            self.rect = self._build_center_rect_by_item(width, height)
        if self._is_point_mode_item():
            # 点赞点位标定改为“单圆点模式”：
            # 兼容历史矩形数据，默认取矩形中心作为初始圆点位置。
            cx = int((self.rect[0] + self.rect[2]) / 2)
            cy = int((self.rect[1] + self.rect[3]) / 2)
            self.point = [cx, cy]
            self.rect = [cx - 1, cy - 1, cx + 1, cy + 1]
            if self._is_pull_new_experiment_scroll_item():
                # 优先读取历史动作点位，便于重复微调。
                action = self.config.get("pull_new_experiment_scroll_action", {})
                ax = action.get("x")
                ay = action.get("y")
                if isinstance(ax, (int, float)) and isinstance(ay, (int, float)):
                    self.point = [int(ax * width), int(ay * height)]
                    self.rect = [self.point[0] - 1, self.point[1] - 1, self.point[0] + 1, self.point[1] + 1]
                # 读取“向下滚动距离”，供用户直接输入编辑。
                # 若用户尚未配置（<=0），则基于“实验卡片3x4点位”的三行纵向距离估算默认值。
                raw_dist = action.get("distance_down", action.get("distance", 0))
                try:
                    dist = max(0.0, float(raw_dist))
                except Exception:
                    dist = 0.0
                if dist <= 0:
                    dist = float(self._estimate_pull_scroll_distance_from_experiment_points(height))
                dist_text = f"{dist:.2f}".rstrip("0").rstrip(".")
                self.scroll_distance_var = tk.StringVar(value=dist_text)
        elif self._is_experiment_switch_item():
            # 若历史已存在 12 点实验网格，优先用历史点位反推矩形，
            # 让用户二次标定时能在上次结果基础上微调。
            exp_points = self.config.get("experiment_points", [])
            if isinstance(exp_points, list) and len(exp_points) == 12:
                px = [int(p[0] * width) for p in exp_points if isinstance(p, list) and len(p) == 2]
                py = [int(p[1] * height) for p in exp_points if isinstance(p, list) and len(p) == 2]
                if len(px) == 12 and len(py) == 12:
                    self.rect = [min(px), min(py), max(px), max(py)]
        elif self._is_body_part_item():
            # 身体部位模式读取历史 7 点，便于二次标定时直接微调。
            part_points = self.config.get("body_part_points", [])
            if isinstance(part_points, list) and len(part_points) == 7:
                px = [int(p[0] * width) for p in part_points if isinstance(p, list) and len(p) == 2]
                py = [int(p[1] * height) for p in part_points if isinstance(p, list) and len(p) == 2]
                if len(px) == 7 and len(py) == 7:
                    self.rect = [min(px), min(py), max(px), max(py)]
        elif item_key in ("bar_female", "bar_male"):
            # 与「敏感进度条」完全同宽、同高、左右对齐；仅保留纵向位置由用户调整（整体上下平移）。
            # 游戏布局自上而下：敏感(蓝) → 女(红) → 男(红)。首次打开时默认纵向错开，减少框到蓝条或与另一条重叠。
            sr = self.config["calibration_rects"].get("sensitive_progress_bar")
            done_sens = bool(self.config.get("calibration_done", {}).get("sensitive_progress_bar", False))
            if isinstance(sr, (list, tuple)) and len(sr) == 4 and done_sens:
                sx1 = int(sr[0] * width)
                sy1 = int(sr[1] * height)
                sx2 = int(sr[2] * width)
                sy2 = int(sr[3] * height)
                if sx1 > sx2:
                    sx1, sx2 = sx2, sx1
                if sy1 > sy2:
                    sy1, sy2 = sy2, sy1
                bar_h = max(2, sy2 - sy1)
                if sx2 - sx1 >= 2 and bar_h >= 2:
                    gap_px = max(4, int(height * 0.008))
                    done_f = bool(done_map.get("bar_female", False))
                    done_m = bool(done_map.get("bar_male", False))
                    if item_key == "bar_female" and not done_f:
                        ny1 = min(max(0, height - bar_h), sy2 + gap_px)
                        ny2 = ny1 + bar_h
                        self.rect = [sx1, ny1, sx2, ny2]
                    elif item_key == "bar_male" and not done_m:
                        frn = self.config["calibration_rects"].get("bar_female")
                        if isinstance(frn, (list, tuple)) and len(frn) == 4 and done_f:
                            fy1 = int(min(frn[1], frn[3]) * height)
                            fy2 = int(max(frn[1], frn[3]) * height)
                            ny1 = min(max(0, height - bar_h), fy2 + gap_px)
                        else:
                            # 尚未标定女条时，退化为敏感条下方第二格位置，避免与蓝条同高。
                            ny1 = min(max(0, height - bar_h), sy2 + gap_px + bar_h + gap_px)
                        ny2 = ny1 + bar_h
                        self.rect = [sx1, ny1, sx2, ny2]
                    else:
                        ox1, oy1, ox2, oy2 = self.rect
                        mid_y = (oy1 + oy2) / 2.0
                        ny1 = int(round(mid_y - bar_h / 2.0))
                        ny2 = ny1 + bar_h
                        max_y1 = max(0, height - bar_h)
                        ny1 = max(0, min(max_y1, ny1))
                        ny2 = ny1 + bar_h
                        self.rect = [sx1, ny1, sx2, ny2]
                    self._bar_lock = {"x1": sx1, "x2": sx2, "h": bar_h}

        self.win = tk.Toplevel(self.root)
        self.win.title(f"标定: {item_label}")
        # 去掉系统标题栏与边框，避免窗口装饰造成坐标偏移。
        # 这样 canvas 坐标可与游戏窗口像素坐标一一对应。
        self.win.overrideredirect(True)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.attributes("-topmost", True)
        if self._is_pull_new_experiment_scroll_item():
            # 特殊规则：该标定项不使用暗色遮罩层。
            # 通过 transparentcolor 仅保留绘制元素与控制面板，背景保持透明。
            transparent_bg = "#ff00ff"
            self.win.configure(bg=transparent_bg)
            self.win.attributes("-transparentcolor", transparent_bg)
        else:
            self.win.configure(bg=BG_APP)
            # 提升标定层整体可见度，避免边框/点位/文字过淡看不清。
            self.win.attributes("-alpha", 0.48)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        # 让回车可直接应用，Esc 直接取消，提升标定操作效率。
        self.win.bind("<Return>", lambda _e: self.save())
        self.win.bind("<Escape>", lambda _e: self.cancel())
        self.win.focus_force()

        panel = tk.Frame(self.win, bg=BG_SUBCARD)
        panel.place(x=10, y=10)
        tk.Button(panel, text="保存", command=self.save, bg=ACCENT, fg="white", relief="flat").pack(side="left", padx=4, pady=4)
        tk.Button(panel, text="取消", command=self.cancel, bg=BTN_DANGER, fg="white", relief="flat").pack(side="left", padx=4, pady=4)
        if self._is_pull_new_experiment_scroll_item():
            tk.Label(panel, text="向下滚动距离:", fg=FG_MAIN, bg=BG_SUBCARD).pack(side="left", padx=(8, 4))
            entry = tk.Entry(panel, textvariable=self.scroll_distance_var, width=6)
            entry.pack(side="left", padx=(0, 6))
            tk.Label(panel, text="预览调试请按 F11", fg=FG_MAIN, bg=BG_SUBCARD).pack(side="left", padx=(4, 6))
            # 打开时默认聚焦到输入框，便于直接录入距离。
            entry.focus_set()
        if self._is_like_item():
            tip_text = "左键点击/拖动圆点"
        elif self._is_pull_new_experiment_scroll_item():
            tip_text = "左键定位执行点，填写向下滚动距离，按F11重播调试，回车保存"
        elif self._is_experiment_switch_item():
            tip_text = "左键拖动/缩放矩形，自动生成12点网格"
        elif self._is_body_part_item():
            tip_text = "左键拖动/缩放矩形，自动生成单行7个中心点"
        elif self._bar_lock is not None:
            # 女/男条：禁止改宽高与水平位置，避免与敏感条采样区域不一致。
            tip_text = "左键拖动整框上下平移（宽/高/横坐标与敏感进度条一致）"
        else:
            tip_text = "左键拖动/缩放"
        tk.Label(panel, text=f"正在标定：{item_label}（{tip_text}）", fg=FG_MAIN, bg=BG_SUBCARD).pack(
            side="left", padx=6
        )
        # 避免标定操作面板遮挡目标框（特别是左上角小目标，如“实验选定标志”）。
        # 若默认左上角面板与标定框重叠，则自动挪到窗口底部。
        panel.update_idletasks()
        panel_w = panel.winfo_width()
        panel_h = panel.winfo_height()
        panel_x = 10
        panel_y = 10
        rx1, ry1, rx2, ry2 = self.rect
        overlap_x = not (panel_x + panel_w < rx1 or panel_x > rx2)
        # 画布上的名称标签可能画在框体上方，检测重叠时把框顶向上虚拟扩展一段，避免控制条挡住标签区。
        label_reserve_top = 36
        expanded_top = max(0, ry1 - label_reserve_top)
        overlap_y = not (panel_y + panel_h < expanded_top or panel_y > ry2)
        if overlap_x and overlap_y and (not self._is_pull_new_experiment_scroll_item()):
            panel_y = max(10, self.game_height - panel_h - 10)
            panel.place_configure(x=panel_x, y=panel_y)

        canvas_bg = "#ff00ff" if self._is_pull_new_experiment_scroll_item() else BG_APP
        self.canvas = tk.Canvas(self.win, bg=canvas_bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.redraw()
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Motion>", self.on_motion)
        # 画布创建在后，可能遮住上方面板；显式提升面板层级确保输入控件可见可点。
        panel.lift()

        self.state.manual_pause = True
        self.state.set_status(f"标定中: {item_label}")

    def _rects_overlap(self, a, b):
        """两轴对齐矩形是否相交（含贴边视为不相交，避免零宽条误判）。"""
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)

    def _draw_label_outside(self, target_rect, text):
        """
        将标签绘制在目标框/点位外侧，避免遮挡边框或定位点。
        使用字体测量计算包围盒，在多个候选位置中选取与 target_rect 不相交且尽量在窗口内的一处。
        """
        x1, y1, x2, y2 = target_rect
        gw, gh = self.game_width, self.game_height
        font_ui = ("Microsoft YaHei UI", 11, "bold")
        fobj = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        tw = int(fobj.measure(text))
        th = max(14, int(fobj.metrics("linespace")))
        margin = 8

        def bbox_nw(tx, ty):
            return (tx, ty, tx + tw, ty + th)

        def ok_pos(tx, ty):
            bb = bbox_nw(tx, ty)
            if self._rects_overlap(bb, target_rect):
                return False
            # 允许轻微贴边，整块尽量留在游戏窗口内。
            pad = 2
            if bb[0] < -pad or bb[1] < -pad or bb[2] > gw + pad or bb[3] > gh + pad:
                return False
            return True

        cx = (x1 + x2 - tw) / 2.0
        candidates = [
            (cx, y1 - th - margin),
            (cx, y2 + margin),
            (x1 - tw - margin, (y1 + y2 - th) / 2.0),
            (x2 + margin, (y1 + y2 - th) / 2.0),
            (float(x1), y1 - th - margin),
            (float(x1), y2 + margin),
        ]
        tx, ty = float(x1), float(max(4, y1 - th - margin))
        for cand in candidates:
            ctx, cty = cand
            if ok_pos(ctx, cty):
                tx, ty = ctx, cty
                break
        else:
            # 兜底：贴游戏窗口左上角或右上角文字区，仍尽量避免盖住标定框。
            for try_tx, try_ty in ((4.0, 4.0), (max(4.0, gw - tw - 4), 4.0), (4.0, max(4.0, gh - th - 4))):
                if ok_pos(try_tx, try_ty):
                    tx, ty = try_tx, try_ty
                    break

        # 先画深色描边，再画亮色正文，提升复杂背景上的可读性。
        self.canvas.create_text(
            tx + 1,
            ty + 1,
            anchor="nw",
            text=text,
            fill="#111827",
            font=font_ui,
        )
        self.canvas.create_text(
            tx,
            ty,
            anchor="nw",
            text=text,
            fill="#ffe066",
            font=font_ui,
        )

    def redraw(self):
        self.canvas.delete("all")
        if self._is_point_mode_item():
            # 点位模式使用圆点可视化，仅表达“执行坐标”，不再表达区域范围。
            cx, cy = self.point
            r = self.point_radius
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#00ff88", width=3)
            self.canvas.create_line(cx - 16, cy, cx + 16, cy, fill="#00ff88", width=2)
            self.canvas.create_line(cx, cy - 16, cx, cy + 16, fill="#00ff88", width=2)
            label = self.item_label
            if self._is_pull_new_experiment_scroll_item():
                dist = self._get_pull_scroll_distance_from_input()
                label = f"{self.item_label} | 向下距离:{dist}"
            self._draw_label_outside([cx - r, cy - r, cx + r, cy + r], label)
            return

        if self._is_experiment_switch_item():
            x1, y1, x2, y2 = self.rect
            self.canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff88", width=3)
            points = self._build_experiment_grid_points()
            # 3x4 网格：统一横向/纵向间距，拖动矩形即可整体调整网格密度。
            for idx, (px, py) in enumerate(points, start=1):
                self.canvas.create_oval(px - 6, py - 6, px + 6, py + 6, outline="#00ff88", width=3)
                self.canvas.create_text(
                    px + 8,
                    py - 8,
                    anchor="nw",
                    text=str(idx),
                    fill="#ffe066",
                    font=("Microsoft YaHei UI", 10, "bold"),
                )
            cur_exp = self.config.get("current_experiment", [1, 1])
            self._draw_label_outside([x1, y1, x2, y2], f"{self.item_label} 当前实验: ({cur_exp[0]},{cur_exp[1]})")
            return

        if self._is_body_part_item():
            x1, y1, x2, y2 = self.rect
            self.canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff88", width=3)
            points = self._build_body_part_points()
            for idx, (px, py) in enumerate(points, start=1):
                self.canvas.create_oval(px - 6, py - 6, px + 6, py + 6, outline="#00ff88", width=3)
                self.canvas.create_text(
                    px + 8,
                    py - 8,
                    anchor="nw",
                    text=str(idx),
                    fill="#ffe066",
                    font=("Microsoft YaHei UI", 10, "bold"),
                )
            cur_part = int(self.config.get("current_body_part", 1))
            self._draw_label_outside([x1, y1, x2, y2], f"{self.item_label} 当前: {cur_part}")
            return

        x1, y1, x2, y2 = self.rect
        self.canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff88", width=3)
        self._draw_label_outside([x1, y1, x2, y2], self.item_label)

    def clamp(self):
        if self._is_point_mode_item():
            # 圆点模式只需约束单点坐标，避免越界到游戏窗口外。
            px, py = self.point
            px = max(0, min(self.game_width - 1, px))
            py = max(0, min(self.game_height - 1, py))
            self.point = [px, py]
            self.rect = [px - 1, py - 1, px + 1, py + 1]
            return

        if self._bar_lock is not None:
            # 女/男条：宽高与左右与敏感条一致，仅钳制纵向平移。
            h = int(self._bar_lock["h"])
            x1 = int(self._bar_lock["x1"])
            x2 = int(self._bar_lock["x2"])
            y1 = int(self.rect[1])
            y1 = max(0, min(self.game_height - h, y1))
            y2 = y1 + h
            self.rect = [x1, y1, x2, y2]
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
        if self._bar_lock is not None:
            # 仅允许框内拖动整体上下平移，不提供边角缩放（尺寸由敏感条决定）。
            if x1 <= x <= x2 and y1 <= y <= y2:
                return "move"
            return None

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
        if self._is_point_mode_item():
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

        if self._is_point_mode_item() and self.drag_mode == "point":
            self.point = [event.x, event.y]
            self.clamp()
            self.redraw()
            return

        dx, dy = event.x - self.last_xy[0], event.y - self.last_xy[1]
        x1, y1, x2, y2 = self.rect

        if self._bar_lock is not None and self.drag_mode == "move":
            # 忽略水平位移，避免误触导致框体漂移（左右已锁定）。
            h = int(self._bar_lock["h"])
            y1 += dy
            self.rect = [int(self._bar_lock["x1"]), y1, int(self._bar_lock["x2"]), y1 + h]
            self.last_xy = (event.x, event.y)
            self.clamp()
            self.redraw()
            return

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
        if self._is_point_mode_item():
            self.canvas.configure(cursor="crosshair")
            return

        if self._bar_lock is not None:
            self.canvas.configure(cursor="sb_v_double_arrow" if self.get_drag_mode(event.x, event.y) else "arrow")
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

    def _get_pull_scroll_distance_from_input(self):
        """
        读取“拉出新实验滚动”的向下滚动距离输入并做兜底清洗：
        - 空值/非法值统一回退 0；
        - 负数统一钳制为 0；
        - 支持小数（例如 8.5）。
        """
        if self.scroll_distance_var is None:
            return 0.0
        try:
            value = float(str(self.scroll_distance_var.get()).strip())
        except Exception:
            value = 0.0
        return max(0.0, value)

    def _print_bar_stack_layout_hints_after_save(self, item_key, norm):
        """
        游戏内自上而下为：敏感进度条(蓝) → 女进度条(红) → 男进度条(红)。
        若归一化矩形纵向与其它条严重交叠，运行时红色掩膜会读到错误行，表现为「总是一侧条偏高」。
        此处仅打印提示，不修改用户坐标。
        """
        eps = 0.005
        rects = self.config.get("calibration_rects", {})
        done = self.config.get("calibration_done", {})

        def bottom(nr):
            return max(float(nr[1]), float(nr[3]))

        def top(nr):
            return min(float(nr[1]), float(nr[3]))

        s = rects.get("sensitive_progress_bar")
        if item_key == "bar_female" and done.get("sensitive_progress_bar") and isinstance(s, (list, tuple)) and len(s) == 4:
            if top(norm) < bottom(s) - eps:
                print(
                    "\n[标定提示] 女进度条顶边高于敏感条底边，框选可能与蓝条区域重叠；"
                    "红蓝混合会使识别异常。请将女条框在第二根红色条上（紧贴敏感条下方）。"
                )
        if item_key == "bar_male":
            f = rects.get("bar_female")
            if done.get("bar_female") and isinstance(f, (list, tuple)) and len(f) == 4:
                if top(norm) < bottom(f) - eps:
                    print(
                        "\n[标定提示] 男进度条顶边高于女进度条底边，可能与女条重叠。"
                        "请把男条框在最下方红条上。"
                    )
        if item_key == "bar_female":
            m = rects.get("bar_male")
            if done.get("bar_male") and isinstance(m, (list, tuple)) and len(m) == 4:
                if bottom(norm) > top(m) + eps:
                    print(
                        "\n[标定提示] 女进度条底边低于已保存的男进度条顶边，女/男上下顺序可能反了或两条区域相交。"
                        "请确认女在上、男在下。"
                    )

    def save(self):
        x1, y1, x2, y2 = self.rect
        norm = [x1 / self.game_width, y1 / self.game_height, x2 / self.game_width, y2 / self.game_height]
        if self._is_point_mode_item():
            # 点赞项只保留单点：为了复用既有数据结构，写为零面积矩形 [x,y,x,y]。
            # 后续计算中心点时仍可得到准确点击坐标。
            self.clamp()
            px, py = self.point
            nx = px / self.game_width
            ny = py / self.game_height
            norm = [nx, ny, nx, ny]
        self.config["calibration_rects"][self.item_key] = norm

        # 只有需要图像的标定项才截图；点赞点位和网格类标定仅保存坐标，不截图。
        need_screenshot = self.item_key in (
            "start",
            "cum2",
            "cum_single",
            "finish",
            "experiment_selected_flag",
            "recover_stamina_button",
            "sensitive_progress_bar",
            "special_action_button",
            "bar_female",
            "bar_male",
        )
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
        if self.item_key in (
            "start",
            "cum2",
            "cum_single",
            "finish",
            "experiment_selected_flag",
            "recover_stamina_button",
            "sensitive_progress_bar",
            "special_action_button",
        ):
            crop = screenshot[y1:y2, x1:x2]
            if crop.size > 0:
                file_name = f"custom_{self.item_key}.png"
                rel_path = str(Path("assets") / "templates" / file_name)
                cv2.imwrite(str(PROJECT_ROOT / rel_path), crop)
                self.config["custom_templates"][self.item_key] = rel_path
                self.config["template_regions"][self.item_key] = norm
        # 女进度条：bar_regions 供 detect_bars 裁切；bar_profiles 仍写入备查（运行时女/男填充率已改用固定红色掩膜）。
        elif self.item_key == "bar_female":
            self.config["bar_regions"]["bar1"] = norm
            c1 = screenshot[y1:y2, x1:x2]
            if c1.size > 0:
                self.config["bar_profiles"]["bar1"] = sample_hsv_profile(c1)
            # 清理历史版本遗留的“bar_female 模板匹配”配置，避免与进度条纠偏混用。
            self.config["custom_templates"].pop("bar_female", None)
            self.config["template_regions"].pop("bar_female", None)
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
        elif self._is_pull_new_experiment_scroll_item():
            # 记录滚轮执行参数：坐标 + 用户输入的“向下滚动距离”。
            self.clamp()
            px, py = self.point
            distance_down = self._get_pull_scroll_distance_from_input()
            self.config["pull_new_experiment_scroll_action"] = {
                "x": px / self.game_width,
                "y": py / self.game_height,
                "distance_down": distance_down,
            }
        elif self._is_experiment_switch_item():
            # 将矩形内 3x4 网格点保存为归一化坐标，顺序固定为 1~12：
            # 1~4 第一行，5~8 第二行，9~12 第三行。
            exp_points = []
            for px, py in self._build_experiment_grid_points():
                exp_points.append([px / self.game_width, py / self.game_height])
            self.config["experiment_points"] = exp_points
            # 若当前实验索引缺失/非法，回落到 (1,1)。
            cur_exp = self.config.get("current_experiment", [1, 1])
            if (
                (not isinstance(cur_exp, list))
                or len(cur_exp) != 2
                or (not isinstance(cur_exp[0], int))
                or (not isinstance(cur_exp[1], int))
                or cur_exp[0] < 1
                or cur_exp[0] > 3
                or cur_exp[1] < 1
                or cur_exp[1] > 4
            ):
                self.config["current_experiment"] = [1, 1]
        elif self._is_body_part_item():
            part_points = []
            for px, py in self._build_body_part_points():
                part_points.append([px / self.game_width, py / self.game_height])
            self.config["body_part_points"] = part_points
            cur_part = self.config.get("current_body_part", 1)
            if (not isinstance(cur_part, int)) or cur_part < 1 or cur_part > 7:
                self.config["current_body_part"] = 1

        if self.item_key in ("bar_female", "bar_male"):
            self._print_bar_stack_layout_hints_after_save(self.item_key, norm)

        self.config["calibration_done"][self.item_key] = True
        self.config_store.save()
        if self._is_pull_new_experiment_scroll_item():
            # 按需求：标定确认（回车/保存）后，延迟 1 秒重播一遍滚轮动作。
            action = self.config.get("pull_new_experiment_scroll_action", {})
            ax = action.get("x", 0.5)
            ay = action.get("y", 0.5)
            try:
                distance = max(0.0, float(action.get("distance_down", action.get("distance", 0))))
            except Exception:
                distance = 0.0
            # 先关闭标定层，避免覆盖鼠标坐标与滚轮回放。
            if self.is_open():
                self.win.withdraw()
                self.win.update_idletasks()
                self.win.update()
            sleep(1.0)
            abs_x = int(self.game_left + ax * self.game_width)
            abs_y = int(self.game_top + ay * self.game_height)
            pyautogui.moveTo(abs_x, abs_y)
            # 新比例：1档=10滚轮单位，支持小数档位（如 8.5 -> 85 单位）。
            total_units = max(0, int(round(distance * 10.0)))
            full_steps = total_units // 10
            remain_units = total_units % 10
            for _ in range(full_steps):
                pyautogui.scroll(-10)
                # 提速 2 倍：每步滚动间隔由 10ms 降到 5ms。
                sleep(0.005)
            if remain_units > 0:
                pyautogui.scroll(-remain_units)
        # 标定完成后不自动恢复，等待用户点“继续运行”再进入自动流程。
        self.state.calibration_updated = True
        self.state.manual_pause = True
        self.state.set_status(f"已应用标定: {self.item_label}（等待继续运行）")
        self.win.destroy()
        self.win = None

    def cancel(self):
        # 不强制恢复 manual_pause：
        # 原来无条件设 False 会导致用户在标定窗口按 ESC 后自动流程立即恢复，
        # 现在只关闭窗口，由用户主动点"开始/继续"或按 F1 决定何时恢复。
        self.state.set_status("取消标定")
        if self.is_open():
            self.win.destroy()
        self.win = None


class AllCalibrationOverlay:
    """F12 调试叠加层：绘制所有标定框/点位。"""

    def __init__(self, root, config_store, state, window_service):
        self.root = root
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.win = None
        self.canvas = None

    @property
    def config(self):
        return self.config_store.data

    def is_open(self):
        return self.win is not None and self.win.winfo_exists()

    def open(self):
        if self.is_open():
            return
        left, top, width, height = self.window_service.get_window_region()
        self.win = tk.Toplevel(self.root)
        self.win.title("all-calibration-debug")
        self.win.overrideredirect(True)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG_APP)
        # 提升 F12 调试叠加层可见度，避免边框/点位/文字过淡看不清。
        self.win.attributes("-alpha", 0.48)
        self.canvas = tk.Canvas(self.win, bg=BG_APP, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.redraw()

    def close(self):
        if self.is_open():
            self.win.destroy()
        self.win = None
        self.canvas = None

    def _draw_label_outside(self, target_rect, text, color):
        """
        将标签绘制在目标框外侧，避免遮挡边框/定位点。
        优先放在目标上方；若空间不足则放在下方。
        """
        x1, y1, x2, y2 = target_rect
        top_y = y1 - 24
        if top_y >= 4:
            tx, ty = x1, top_y
        else:
            tx = x1
            ty = min(max(4, y2 + 8), self.win.winfo_height() - 24)
        self.canvas.create_text(
            tx + 1,
            ty + 1,
            anchor="nw",
            text=text,
            fill="#111827",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.canvas.create_text(
            tx,
            ty,
            anchor="nw",
            text=text,
            fill=color,
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def redraw(self):
        if not self.is_open():
            return
        left, top, width, height = self.window_service.get_window_region()
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.canvas.delete("all")
        done_map = self.config.get("calibration_done", {})
        rects = self.config.get("calibration_rects", {})
        selected_keys = list(getattr(self.state, "calibration_overlay_selected_keys", []))
        selected_set = set(selected_keys)
        for key, label in CALIBRATION_ITEMS:
            if selected_set and key not in selected_set:
                continue
            rect = rects.get(key)
            if (not isinstance(rect, list)) or len(rect) != 4:
                continue
            x1 = int(rect[0] * width)
            y1 = int(rect[1] * height)
            x2 = int(rect[2] * width)
            y2 = int(rect[3] * height)
            color = "#22c55e" if bool(done_map.get(key, False)) else "#ef4444"
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3)
            self._draw_label_outside([x1, y1, x2, y2], label, color)
        # 实验卡片点位（若存在）附加绘制
        if (not selected_set) or ("experiment_switch" in selected_set):
            exp_points = self.config.get("experiment_points", [])
            for idx, p in enumerate(exp_points, start=1):
                if isinstance(p, list) and len(p) == 2:
                    px = int(p[0] * width)
                    py = int(p[1] * height)
                    self.canvas.create_oval(px - 4, py - 4, px + 4, py + 4, outline="#f59e0b", width=3)
                    self.canvas.create_text(px + 8, py - 10, anchor="nw", text=str(idx), fill="#f59e0b")
        # 身体部位点位（若存在）附加绘制
        if (not selected_set) or ("body_part_switch" in selected_set):
            part_points = self.config.get("body_part_points", [])
            for idx, p in enumerate(part_points, start=1):
                if isinstance(p, list) and len(p) == 2:
                    px = int(p[0] * width)
                    py = int(p[1] * height)
                    self.canvas.create_oval(px - 4, py - 4, px + 4, py + 4, outline="#38bdf8", width=3)
                    self.canvas.create_text(px + 8, py - 10, anchor="nw", text=str(idx), fill="#38bdf8")


def launch_floating_window(config_store, state, window_service):
    """悬浮窗入口：二级菜单逐项标定 + 颜色状态。"""

    root = tk.Tk()
    root.title("autoFDX 悬浮窗")
    # 根据屏幕自适应窗口宽度（收紧版）：
    # 在当前两列紧凑布局下，整体宽度可适当下调，减少横向占位。
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = min(780, max(620, screen_w - 200))
    saved_pos = config_store.data.get("ui_window_pos", [20, 20])
    if not isinstance(saved_pos, list) or len(saved_pos) != 2:
        saved_pos = [20, 20]
    win_x = int(saved_pos[0])
    win_y = int(saved_pos[1])
    # 状态与控件布局收紧后，折叠态先给保守高度，启动后再按内容收紧（fit_collapsed_height）。
    win_h_expanded = 500
    win_h_collapsed = 260

    def clamp_window_pos(req_h, px, py):
        """将窗口左上角约束在当前屏幕工作区内，保证整块窗口可见（含多显示器保存坐标拉回主屏）。"""
        cx = max(0, min(max(0, screen_w - win_w), int(px)))
        cy = max(0, min(max(0, screen_h - req_h), int(py)))
        return cx, cy

    # 启动时边界约束：必须用「屏幕尺寸 - 窗口宽高」，否则窗口会整体落到屏幕外（多显示器/历史坐标常见）。
    # 旧逻辑用 screen_h-140 未计入窗口高度，易导致底边超出可视区甚至完全看不到。
    win_x, win_y = clamp_window_pos(win_h_collapsed, win_x, win_y)
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
    # padding 左右/上下尽量对称，便于指示器与文字在控件内垂直方向视觉居中。
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
    # 标题栏内勾选框：背景与 HEADER_BAR_BG 一致，避免色块割裂
    like_chk_style.configure(
        "Like.Header.TCheckbutton",
        font=("Microsoft YaHei UI", 10),
        indicatorsize=22,
        padding=(6, 5),
        background=HEADER_BAR_BG,
        foreground=FG_MAIN,
    )
    like_chk_style.map(
        "Like.Header.TCheckbutton",
        background=[("active", HEADER_BAR_BG), ("disabled", HEADER_BAR_BG), ("!disabled", HEADER_BAR_BG)],
        foreground=[("disabled", FG_MUTED), ("!disabled", FG_MAIN)],
    )

    status_var = tk.StringVar(value="流程: init")
    init_run_text = "已暂停" if state.manual_pause else "运行中"
    run_var = tk.StringVar(value=init_run_text)
    status_line_var = tk.StringVar(value=f"流程: init  |  状态: {init_run_text}")
    like_enabled_var = tk.BooleanVar(value=bool(config_store.data.get("like_enabled", True)))
    like_force_next_var = tk.BooleanVar(value=bool(config_store.data.get("like_force_next", False)))
    experiment_switch_enabled_var = tk.BooleanVar(value=bool(config_store.data.get("experiment_switch_enabled", False)))
    single_cum_mode_enabled_var = tk.BooleanVar(value=bool(config_store.data.get("single_cum_mode_enabled", False)))
    overlay_dx_var = tk.BooleanVar(value=bool(config_store.data.get("overlay_dx_hook_enabled", False)))
    overlay = CalibrationOverlay(root, config_store, state, window_service)
    all_overlay = AllCalibrationOverlay(root, config_store, state, window_service)
    selector_win = None
    selector_vars = {}
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
        # 固定按钮：控制是否保持置顶固定显示。
        if is_pinned_topmost:
            btn_win_pin.set_text("固定中")
        else:
            btn_win_pin.set_text("固定")

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
                # 关闭全屏兼容时移除分层相关标志，避免未分层却调用 SetLayeredWindowAttributes 导致显示异常
                exstyle &= ~(WS_EX_NOREDIRECTIONBITMAP | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)

            # SetLayeredWindowAttributes 仅在窗口带 WS_EX_LAYERED 时合法；否则部分环境下会导致客户区不可见或调用失败。
            if enabled:
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
        # 从“暂停->继续”前，先校验“实验切换”所需标定是否齐全。
        # 这样可确保开启实验切换后，不会在缺少关键点位时误运行。
        if state.manual_pause and experiment_switch_enabled_var.get() and (not single_cum_mode_enabled_var.get()):
            missing = []
            done_map = config_store.data.get("calibration_done", {})
            required = [
                ("experiment_selected_flag", "实验选定标志"),
                ("recover_stamina_button", "恢复体力按钮"),
                ("experiment_switch", "实验卡片"),
                ("body_part_switch", "身体部位"),
            ]
            for key, label in required:
                if not bool(done_map.get(key, False)):
                    missing.append(label)

            if len(config_store.data.get("experiment_points", [])) != 12 and "实验卡片" not in missing:
                missing.append("实验卡片")
            if len(config_store.data.get("body_part_points", [])) != 7 and "身体部位" not in missing:
                missing.append("身体部位")

            if missing:
                state.manual_pause = True
                state.set_status(f"实验切换缺少标定: {'、'.join(missing)}")
                apply_pause_ui_text()
                return

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
        nonlocal selector_win
        state.stop_requested = True
        if overlay.is_open():
            overlay.cancel()
        if all_overlay.is_open():
            all_overlay.close()
        if selector_win is not None and selector_win.winfo_exists():
            selector_win.destroy()
            selector_win = None
        persist_window_pos()
        # 先 quit 再 destroy，确保 mainloop 干净退出。
        root.quit()
        root.destroy()

    def close_selector_window(reset_phase=False):
        nonlocal selector_win, selector_vars
        if selector_win is not None and selector_win.winfo_exists():
            selector_win.destroy()
        selector_win = None
        selector_vars = {}
        state.open_calibration_overlay_selector = False
        if reset_phase:
            state.calibration_overlay_phase = "idle"
            state.calibration_overlay_selected_keys = []

    def open_selector_window():
        nonlocal selector_win, selector_vars
        if selector_win is not None and selector_win.winfo_exists():
            selector_win.lift()
            return

        selector_vars = {}
        selected_now = set(getattr(state, "calibration_overlay_selected_keys", []))
        selector_win = tk.Toplevel(root)
        selector_win.title("选择要显示的标定项")
        selector_win.attributes("-topmost", True)
        selector_win.configure(bg=BG_CARD)
        selector_win.resizable(False, False)

        card = tk.Frame(selector_win, bg=BG_CARD, padx=10, pady=10)
        card.pack(fill="both", expand=True)
        tk.Label(
            card,
            text="选择要显示的标定项（F12下一次按下将显示所选项）",
            bg=BG_CARD,
            fg=FG_MAIN,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        col_count = 3
        for i, (key, label) in enumerate(CALIBRATION_ITEMS):
            var = tk.BooleanVar(value=(key in selected_now))
            selector_vars[key] = var
            chk = ttk.Checkbutton(card, text=label, variable=var, style="Like.Big.TCheckbutton")
            chk.grid(row=1 + (i // col_count), column=i % col_count, sticky="nsew", padx=4, pady=3)

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.grid(row=1 + (len(CALIBRATION_ITEMS) // col_count) + 1, column=0, columnspan=3, sticky="e", pady=(10, 0))

        def on_confirm():
            selected = [k for k, _ in CALIBRATION_ITEMS if selector_vars.get(k) and bool(selector_vars[k].get())]
            state.calibration_overlay_selected_keys = selected
            if selected:
                state.calibration_overlay_phase = "ready_to_show"
                print(f"\n[F12] 已选定 {len(selected)} 个标定项，再按一次 F12 显示。")
            else:
                state.calibration_overlay_phase = "idle"
                print("\n[F12] 未选择任何标定项，已取消。")
            close_selector_window(reset_phase=False)

        def on_cancel():
            close_selector_window(reset_phase=True)
            print("\n[F12] 已取消标定项选择。")

        tk.Button(btn_row, text="取消", command=on_cancel, bg=BTN_DANGER, fg="white", relief="flat").pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="确定", command=on_confirm, bg=ACCENT, fg="white", relief="flat").pack(side="right")
        selector_win.protocol("WM_DELETE_WINDOW", on_cancel)

    def toggle_submenu():
        if submenu_host.winfo_ismapped():
            submenu_host.pack_forget()
            nh = win_h_collapsed
        else:
            submenu_host.pack(fill="both", expand=True, pady=(4, 0))
            nh = win_h_expanded
        # 高度变化后重新夹紧，避免拉高后底边超出屏幕
        cx, cy = clamp_window_pos(nh, root.winfo_x(), root.winfo_y())
        root.geometry(f"{win_w}x{nh}+{cx}+{cy}")
        # 收起标定菜单后再按实际内容收紧高度（fit_collapsed_height 在下方定义）
        if not submenu_host.winfo_ismapped():
            root.after(10, fit_collapsed_height)

    def trigger_item(item_key):
        # 女/男条依赖敏感条的宽高与水平位置，必须先完成敏感进度条标定。
        done_map = config_store.data.get("calibration_done", {})
        if item_key in ("bar_female", "bar_male") and not done_map.get("sensitive_progress_bar", False):
            state.set_status("请先完成「敏感进度条」标定（女/男条将与其同宽同高并可上下平移）")
            print("\n[标定] 请先完成「敏感进度条」标定，再标定女/男进度条。")
            return
        state.pending_calibration = item_key

    def refresh_button_colors():
        done_map = config_store.data.get("calibration_done", {})
        sens_ok = bool(done_map.get("sensitive_progress_bar", False))
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
            # 未完成敏感条时禁止进入女/男条标定，避免与锁定规则冲突。
            if key in ("bar_female", "bar_male"):
                btn.configure(state=("normal" if sens_ok else "disabled"))

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

    def refresh_feature_option_states():
        """
        单高潮模式开启时，仅禁用“实验切换”开关，避免配置语义冲突。
        点赞功能保持可用（按业务规则在后台按 10 回合节奏触发）。
        """
        single_mode = bool(single_cum_mode_enabled_var.get())
        if single_mode:
            chk_experiment_switch.configure(state="disabled")
        else:
            chk_experiment_switch.configure(state="normal")
        chk_like_enabled.configure(state="normal")
        refresh_like_force_state()

    def on_experiment_switch_toggle():
        """
        实验切换总开关：
        - 仅负责记录用户意图；
        - 具体“缺标定禁止运行”的硬约束由“继续运行”与后台流程共同兜底。
        """
        enabled = bool(experiment_switch_enabled_var.get())
        config_store.data["experiment_switch_enabled"] = enabled
        config_store.save()
        if enabled:
            state.set_status("已启用实验切换（需先完成实验相关标定）")
        else:
            state.set_status("已关闭实验切换")

    def on_single_cum_mode_toggle():
        """
        单高潮模式开关：
        - 开启后仅保留“开始 -> 单高潮 -> 再来一次”流程；
        - 点赞保持可用（该模式下后台按 10 回合节奏触发）；
        - 实验切换必须关闭，防止后台误进入其他逻辑。
        """
        enabled = bool(single_cum_mode_enabled_var.get())
        config_store.data["single_cum_mode_enabled"] = enabled
        if enabled:
            # 为保证“只跑三按钮流程”，仅关闭实验切换能力。
            config_store.data["experiment_switch_enabled"] = False
            experiment_switch_enabled_var.set(False)
            state.set_status("已启用单高潮模式（点赞按10回合触发）")
        else:
            state.set_status("已关闭单高潮模式")
        config_store.save()
        refresh_feature_option_states()

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

    # 不再使用外层加粗描边框（用户反馈过宽）；内容区直接铺满根窗口。
    frame = tk.Frame(root, padx=10, pady=8, bg=BG_APP)
    frame.pack(fill="both", expand=True)

    # 标题栏：单独一块深色背景，仅放标题 / 提示 / 窗口控制，与下方主内容区分界清晰
    title_bar = tk.Frame(frame, bg=HEADER_BAR_BG, padx=8, pady=6)
    title_bar.pack(fill="x", pady=(0, 0))
    title_row = tk.Frame(title_bar, bg=HEADER_BAR_BG)
    title_row.pack(fill="x")
    lbl_title = tk.Label(
        title_row, text="autoFDX 控制台", fg=FG_MAIN, bg=HEADER_BAR_BG, font=("Microsoft YaHei UI", 11, "bold")
    )
    lbl_f1_hint = tk.Label(
        title_row,
        text="“F1”键暂停",
        fg=FG_MUTED,
        bg=HEADER_BAR_BG,
        font=("Microsoft YaHei UI", 9),
    )
    win_btn_host = tk.Frame(title_row, bg=HEADER_BAR_BG)
    chk_overlay_dx = ttk.Checkbutton(
        win_btn_host,
        text="全屏模式兼容",
        variable=overlay_dx_var,
        command=on_overlay_dx_toggle,
        style="Like.Header.TCheckbutton",
        cursor="hand2",
    )
    btn_win_pin = create_round_button(
        win_btn_host,
        text="固定中",
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
    # 先固定右侧控件组，再从左侧 pack，保证标题/提示与勾选、按钮在同一行内垂直居中对齐。
    chk_overlay_dx.pack(side="left", padx=(0, 8), anchor="center")
    for btn in (btn_win_pin, btn_win_close):
        btn.pack(side="left", padx=2, anchor="center")
    win_btn_host.pack(side="right", anchor="center")
    lbl_title.pack(side="left", anchor="center")
    lbl_f1_hint.pack(side="left", padx=(10, 0), anchor="center")
    refresh_pin_button()
    # 自定义标题栏拖动窗口（标题栏整块可拖，避免误拖内容区）
    for w in (title_bar, title_row, lbl_title, lbl_f1_hint, win_btn_host, chk_overlay_dx, btn_win_pin, btn_win_close):
        w.bind("<ButtonPress-1>", on_title_press)
        w.bind("<B1-Motion>", on_title_drag)

    # 标题栏与主内容区之间的分隔线，强化层次
    title_sep = tk.Frame(frame, bg="#334155", height=1)
    title_sep.pack(fill="x", pady=(0, 0))

    # 主内容区：与标题栏背景区分；状态与勾选分两行，避免长文案与勾选框同一行重叠
    # 注意：Frame 构造参数 pady 必须是单值，不能传 (top, bottom) 元组；
    # 否则 Tk 会抛出 TclError（bad screen distance），导致窗口启动即失败。
    content_card = tk.Frame(frame, bg=BG_CARD, padx=8, pady=6)
    # 上提主内容卡片，紧贴标题分隔线下方。
    content_card.pack(fill="x", pady=(0, 4))
    # 主内容区按 2 列布局：
    # - 左列分两行：上=状态、下=主按钮
    # - 右列：勾选框
    content_grid = tk.Frame(content_card, bg=BG_CARD)
    content_grid.pack(fill="x")
    content_grid.grid_columnconfigure(0, weight=1)
    content_grid.grid_columnconfigure(1, weight=0)
    content_grid.grid_rowconfigure(0, weight=0)
    content_grid.grid_rowconfigure(1, weight=0)

    left_status_frame = tk.Frame(content_grid, bg=BG_CARD)
    left_status_frame.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
    status_text_label = tk.Label(
        left_status_frame,
        textvariable=status_line_var,
        fg="#93c5fd",
        bg=BG_CARD,
        font=("Microsoft YaHei UI", 10),
        anchor="w",
        justify="left",
        # 左列预留给右侧勾选区，状态文本只在左列内换行。
        wraplength=max(320, win_w - 360),
    )
    # anchor=center：在父 Frame 行高大于标签内容时，整块状态文案在垂直方向居中（单行时无影响）。
    status_text_label.pack(fill="x", anchor="center")

    # 左列第二行：主控制按钮，紧贴状态下方。
    top_btn_frame = tk.Frame(content_grid, bg=BG_CARD)
    top_btn_frame.grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(0, 0))
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
    # 原第三颗「退出脚本」已取消（标题栏 ✕ 关闭即可）；此处保留同尺寸占位，避免两键左移改变布局。
    btn_exit_spacer = tk.Frame(top_btn_frame, width=168, height=42, bg=BG_CARD)
    btn_exit_spacer.pack_propagate(False)
    for btn in (btn_pause, btn_menu):
        btn.pack(side="left", padx=4, anchor="center")
    btn_exit_spacer.pack(side="left", padx=4, anchor="center")

    # 右列：勾选项竖向排列、左端对齐；用 grid 上下行 weight 均分，使整列在「状态+按钮」总高度内垂直居中。
    like_option_frame = tk.Frame(content_grid, bg=BG_CARD, padx=1, pady=2)
    like_option_frame.grid(row=0, column=1, rowspan=2, sticky="ns")
    like_option_frame.grid_columnconfigure(0, weight=1)
    like_option_frame.grid_rowconfigure(0, weight=1)
    like_option_frame.grid_rowconfigure(1, weight=0)
    like_option_frame.grid_rowconfigure(2, weight=1)
    like_option_right = tk.Frame(like_option_frame, bg=BG_CARD)
    like_option_right.grid(row=1, column=0, sticky="w")
    chk_like_enabled = ttk.Checkbutton(
        like_option_right,
        text="启用点赞功能",
        variable=like_enabled_var,
        command=on_like_enabled_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_like_enabled.pack(anchor="w", pady=(0, 2))
    chk_experiment_switch = ttk.Checkbutton(
        like_option_right,
        text="启用实验切换",
        variable=experiment_switch_enabled_var,
        command=on_experiment_switch_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_experiment_switch.pack(anchor="w", pady=2)
    chk_like_force = ttk.Checkbutton(
        like_option_right,
        text="结束后执行点赞-F3",
        variable=like_force_next_var,
        command=on_like_force_next_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_like_force.pack(anchor="w", pady=(2, 0))
    chk_single_cum_mode = ttk.Checkbutton(
        like_option_right,
        text="单高潮模式（仅三按钮）",
        variable=single_cum_mode_enabled_var,
        command=on_single_cum_mode_toggle,
        style="Like.Big.TCheckbutton",
        cursor="hand2",
    )
    chk_single_cum_mode.pack(anchor="w", pady=(2, 0))
    refresh_feature_option_states()

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
    column_count = 4 if win_w >= 820 else 3
    for key, label in CALIBRATION_ITEMS:
        idx = len(calib_buttons)
        row = idx // column_count
        col = idx % column_count
        btn = tk.Button(submenu_frame, text=label, width=12, command=lambda k=key: trigger_item(k))
        style_button(btn, normal_bg=BTN_DANGER, hover_bg="#dc2626")
        btn.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
        calib_buttons[key] = btn

    for col in range(column_count):
        submenu_frame.grid_columnconfigure(col, weight=1)

    def fit_collapsed_height():
        """
        未展开标定菜单时，将窗口高度收束到实际内容高度，消除底部大块留白。
        仅在折叠态调用；展开态由 win_h_expanded 控制。
        """
        nonlocal win_h_collapsed
        try:
            if submenu_host.winfo_ismapped():
                return
        except tk.TclError:
            return
        root.update_idletasks()
        h = max(200, int(root.winfo_reqheight()))
        win_h_collapsed = h
        cx, cy = clamp_window_pos(h, root.winfo_x(), root.winfo_y())
        root.geometry(f"{win_w}x{h}+{cx}+{cy}")

    root.bind("<Configure>", on_root_configure, add="+")
    root.protocol("WM_DELETE_WINDOW", on_exit)
    apply_overlay_mode(force=True)
    # overrideredirect 窗口在部分环境下需显式 deiconify/lift，避免启动后落在其它窗口后不可见
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.attributes("-topmost", is_pinned_topmost)
    # 首次布局完成后收紧折叠高度（标定菜单默认收起）
    root.after(80, fit_collapsed_height)

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
        cfg_experiment_switch = bool(config_store.data.get("experiment_switch_enabled", False))
        if experiment_switch_enabled_var.get() != cfg_experiment_switch:
            experiment_switch_enabled_var.set(cfg_experiment_switch)
        cfg_single_cum_mode = bool(config_store.data.get("single_cum_mode_enabled", False))
        if single_cum_mode_enabled_var.get() != cfg_single_cum_mode:
            single_cum_mode_enabled_var.set(cfg_single_cum_mode)
        cfg_overlay_dx = bool(config_store.data.get("overlay_dx_hook_enabled", False))
        if overlay_dx_var.get() != cfg_overlay_dx:
            overlay_dx_var.set(cfg_overlay_dx)
            apply_overlay_mode(force=True)
        refresh_feature_option_states()
        refresh_button_colors()
        if bool(getattr(state, "open_calibration_overlay_selector", False)):
            open_selector_window()
        elif selector_win is not None and selector_win.winfo_exists() and state.calibration_overlay_phase != "await_selection":
            # 避免状态与窗口不一致（例如外部重置状态）时残留选择窗。
            close_selector_window(reset_phase=False)
        if bool(getattr(state, "show_all_calibration_overlay", False)):
            if not all_overlay.is_open():
                all_overlay.open()
            all_overlay.redraw()
        else:
            if all_overlay.is_open():
                all_overlay.close()
        if state.pending_calibration is not None:
            key = state.pending_calibration
            state.pending_calibration = None
            overlay.open_for_item(key, label_map[key])
        root.after(250, refresh)

    refresh()
    root.mainloop()
