from pathlib import Path

import cv2
import numpy as np
import pyautogui

from .config import PROJECT_ROOT, TEMPLATES_DIR


class VisionService:
    """负责模板匹配与进度条识别。"""

    def __init__(self, config_store, runtime_state, window_service):
        self.config_store = config_store
        self.runtime_state = runtime_state
        self.window_service = window_service
        # 模板缓存：避免高频 match 时重复磁盘读取/解码导致卡顿。
        # 结构: {abs_path: (mtime, gray_image)}
        self._template_cache = {}

    @property
    def config(self):
        return self.config_store.data

    def get_template_path(self, template_name):
        custom_name = self.config.get("custom_templates", {}).get(template_name)
        if custom_name:
            custom_path = PROJECT_ROOT / custom_name
            if custom_path.exists():
                return str(custom_path)
            # 兼容旧配置: custom_templates 仅存文件名
            legacy_custom = TEMPLATES_DIR / Path(custom_name).name
            if legacy_custom.exists():
                return str(legacy_custom)
        default_path = TEMPLATES_DIR / f"{template_name}.png"
        if default_path.exists():
            return str(default_path)
        # 兜底兼容旧目录
        return str(PROJECT_ROOT / f"{template_name}.png")

    def _match_with_template(self, img_gray, template, ac, offset_x=0, offset_y=0):
        """
        对给定模板执行多尺度匹配，返回中心点和最大匹配值。
        该函数只负责“计算”，不负责模板来源和回退策略。
        """
        best_val = -1.0
        best_center = None
        left, top, _, _ = self.window_service.get_window_region()

        for scale in self.config.get("template_match", {}).get("scales", [1.0]):
            x, y = template.shape[0:2]
            scaled = cv2.resize(template, (max(1, int(y * scale)), max(1, int(x * scale))))
            if scaled.shape[0] > img_gray.shape[0] or scaled.shape[1] > img_gray.shape[1]:
                continue
            res = cv2.matchTemplate(img_gray, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                w, h = scaled.shape[::-1]
                best_val = max_val
                best_center = (left + offset_x + max_loc[0] + w // 2, top + offset_y + max_loc[1] + h // 2)

        if best_val < ac:
            return None, best_val
        return best_center, best_val

    def _load_template_gray_cached(self, template_path):
        """
        按文件修改时间缓存模板灰度图：
        - 路径未命中：首次读取并缓存
        - 文件被重新标定覆盖后：mtime 变化，自动失效重载
        这样可以在不牺牲“热更新模板”的前提下，显著减少 IO 与解码开销。
        """
        p = Path(template_path)
        if not p.exists():
            return None

        mtime = p.stat().st_mtime
        cached = self._template_cache.get(str(p))
        if cached and cached[0] == mtime:
            return cached[1]

        img = cv2.imread(str(p), 0)
        if img is None:
            return None
        self._template_cache[str(p)] = (mtime, img)
        return img

    def match(self, template_name, ac=None):
        left, top, width, height = self.window_service.get_window_region()
        template_path = self.get_template_path(template_name)
        template = self._load_template_gray_cached(template_path)
        if template is None:
            raise FileNotFoundError(f"模板不存在：{template_name}")

        if ac is None:
            ac = self.config.get("template_thresholds", {}).get(
                template_name, self.config.get("template_match", {}).get("default_threshold", 0.95)
            )

        # 如果用户已标定该模板区域，则仅在“标定区域 + 冗余边距”做局部截图匹配。
        # 这样可避免每次都全窗口截图，显著降低截图与匹配开销。
        template_regions = self.config.get("template_regions", {})
        if template_name in template_regions:
            nx1, ny1, nx2, ny2 = template_regions[template_name]
            margin = int(self.config.get("template_search_margin", 40))
            x1 = int(nx1 * width)
            y1 = int(ny1 * height)
            x2 = int(nx2 * width)
            y2 = int(ny2 * height)
            x1, x2 = sorted((max(0, x1), min(width, x2)))
            y1, y2 = sorted((max(0, y1), min(height, y2)))
            sx1 = max(0, x1 - margin)
            sy1 = max(0, y1 - margin)
            sx2 = min(width, x2 + margin)
            sy2 = min(height, y2 + margin)
            if sx2 > sx1 and sy2 > sy1:
                local_img = pyautogui.screenshot(region=(left + sx1, top + sy1, sx2 - sx1, sy2 - sy1))
                local_gray = cv2.cvtColor(np.array(local_img), cv2.COLOR_RGB2GRAY)
                best_center, best_val = self._match_with_template(local_gray, template, ac, sx1, sy1)
                if best_center is not None:
                    if self.runtime_state.debug:
                        print(template_name, "phase1-region-shot", best_val, best_center)
                    return best_center

                # 区域模式下仍保留“默认模板兜底”，但同样限定在局部截图内，不回退全屏。
                custom_map = self.config.get("custom_templates", {})
                if template_name in custom_map:
                    default_template_path = TEMPLATES_DIR / f"{template_name}.png"
                    if default_template_path.exists():
                        fallback_template = self._load_template_gray_cached(str(default_template_path))
                        if fallback_template is not None:
                            fallback_center, fallback_val = self._match_with_template(
                                local_gray,
                                fallback_template,
                                max(0.72, ac * 0.9),
                                sx1,
                                sy1,
                            )
                            if fallback_center is not None:
                                if self.runtime_state.debug:
                                    print(template_name, "phase2-region-default-fallback", fallback_val, fallback_center)
                                return fallback_center

                if self.runtime_state.debug:
                    print(template_name, "region-match-failed", best_val, template_path)
                return None

        # 未标定区域时，才回退到全窗口截图匹配（兼容首次使用）。
        img = pyautogui.screenshot(region=(left, top, width, height))
        img_gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        global_center, global_val = self._match_with_template(img_gray, template, max(0.75, ac * 0.95), 0, 0)
        if global_center is not None:
            if self.runtime_state.debug:
                print(template_name, "phase-global", global_val, global_center)
            return global_center

        # 如果用了 custom_ 模板仍失败，再用原始模板兜底（未标定区域场景）。
        custom_map = self.config.get("custom_templates", {})
        if template_name in custom_map:
            default_template_path = TEMPLATES_DIR / f"{template_name}.png"
            if default_template_path.exists():
                fallback_template = self._load_template_gray_cached(str(default_template_path))
                if fallback_template is not None:
                    fallback_center, fallback_val = self._match_with_template(
                        img_gray,
                        fallback_template,
                        max(0.72, ac * 0.9),
                        0,
                        0,
                    )
                    if fallback_center is not None:
                        if self.runtime_state.debug:
                            print(template_name, "phase-fallback-default", fallback_val, fallback_center)
                        return fallback_center

        if self.runtime_state.debug:
            print(template_name, "match-failed", global_val, template_path)
        return None

    def capture_screen(self):
        return cv2.cvtColor(
            np.array(pyautogui.screenshot(region=self.window_service.get_window_region())),
            cv2.COLOR_RGB2BGR,
        )

    def _build_red_mask_bgr(self, bgr_img):
        """
        与 GameActions._build_red_mask 同口径：红色跨 H=0/179，双区间合并。
        女/男快感条在游戏内均为「红色填充」，用固定红掩膜比「整图 HSV 中值 + 容差」更稳：
        标定截图里若混入灰底、描边或邻近 UI，sample_hsv_profile 的中值易偏离真红，导致两掩膜系统性偏一侧。
        """
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        mask1 = cv2.inRange(hsv, np.array([0, 55, 50]), np.array([12, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([168, 55, 50]), np.array([179, 255, 255]))
        return cv2.bitwise_or(mask1, mask2)

    def detect_bars(self, image):
        """
        从 bar_regions 裁切女(bar1)、男(bar2)区域，估计红色填充进度。
        注意：与自动化里敏感条(蓝)无关；仅女/男两条参与差分纠偏。
        """
        bars = self.config["bar_regions"]
        x1a, y1a, x1b, y1b = self.window_service.denormalize_region(bars["bar1"])
        x2a, y2a, x2b, y2b = self.window_service.denormalize_region(bars["bar2"])
        bar1 = image[y1a:y1b, x1a:x1b]
        bar2 = image[y2a:y2b, x2a:x2b]
        if bar1.size == 0 or bar2.size == 0:
            return 0, 0
        m1 = self._build_red_mask_bgr(bar1)
        m2 = self._build_red_mask_bgr(bar2)
        if m1 is None or m2 is None:
            return 0, 0
        return self._bar_fill_score(m1), self._bar_fill_score(m2)

    def _bar_fill_score(self, mask):
        """
        进度条填充估计（比纯面积占比更稳定）：
        1) area_ratio: 前景像素占比
        2) length_ratio: 按列连续填充长度（从左向右）
        最终以 length_ratio 为主，area_ratio 为辅。
        掩膜来源为红色区间时，即表示红条填充进度。
        """
        if mask.size == 0:
            return 0.0

        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        bin_mask = (mask > 0).astype(np.uint8)

        # 先做一次形态学开运算，去掉零散噪点。
        kernel = np.ones((3, 3), np.uint8)
        clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)

        # 每列红色占比，超过阈值视为“该列已填充”。
        col_ratio = np.mean(clean, axis=0)
        active_cols = np.where(col_ratio > 0.25)[0]
        if active_cols.size == 0:
            return area_ratio * 0.5

        # 找连续段，优先取“最靠左且足够长”的段，符合进度条从左往右填充的特点。
        splits = np.where(np.diff(active_cols) > 1)[0]
        starts = [active_cols[0]] + [active_cols[i + 1] for i in splits]
        ends = [active_cols[i] for i in splits] + [active_cols[-1]]
        min_len = max(3, int(mask.shape[1] * 0.05))

        candidate = None
        for s, e in zip(starts, ends):
            if (e - s + 1) >= min_len:
                candidate = (s, e)
                break
        if candidate is None:
            # 没有足够长的连续段时，退化为最右活跃列估计。
            rightmost = int(active_cols.max())
            length_ratio = float(rightmost + 1) / float(mask.shape[1])
        else:
            _, e = candidate
            length_ratio = float(e + 1) / float(mask.shape[1])

        # 以长度为主、面积为辅。
        return 0.65 * length_ratio + 0.35 * area_ratio


def sample_hsv_profile(crop_bgr):
    """校准区域颜色采样：取 HSV 中值，增强抗噪声能力。"""
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    valid = hsv[(hsv[:, 1] > 20) & (hsv[:, 2] > 20)]
    if valid.size == 0:
        valid = hsv
    center = np.median(valid, axis=0).astype(int).tolist()
    return {"hsv": center, "tol": [15, 80, 80]}
