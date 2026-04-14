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
            # 默认搜索框：标定区域 + 全局冗余边距。
            sx1 = max(0, x1 - margin)
            sy1 = max(0, y1 - margin)
            sx2 = min(width, x2 + margin)
            sy2 = min(height, y2 + margin)

            # 特殊规则（按需求）：
            # “实验选定标志”在标定后不再严格限制在原区域，
            # 允许相对“标定框尺寸”发生更大偏移：
            # - 横向允许偏移 2x 本身宽度（左右都放宽）；
            # - 纵向允许偏移 0.5x 本身高度（上下都放宽）。
            #
            # 说明：
            # 1) 这里用的是“标定区域本身”的宽高，而非整窗宽高，符合“本身宽/高”的语义；
            # 2) 采用在默认搜索框基础上的并集扩张（取更宽边界），
            #    保留原有 margin 机制，同时实现更宽容的匹配范围；
            # 3) 对其他模板不生效，避免引入额外误检风险。
            if template_name == "experiment_selected_flag":
                region_w = max(1, x2 - x1)
                region_h = max(1, y2 - y1)
                extra_x = int(region_w * 2.0)
                extra_y = int(region_h * 0.5)
                sx1 = max(0, min(sx1, x1 - extra_x))
                sy1 = max(0, min(sy1, y1 - extra_y))
                sx2 = min(width, max(sx2, x2 + extra_x))
                sy2 = min(height, max(sy2, y2 + extra_y))

                if self.runtime_state.debug:
                    print(
                        template_name,
                        "expanded-region",
                        f"base=({x1},{y1},{x2},{y2})",
                        f"extra=({extra_x},{extra_y})",
                        f"search=({sx1},{sy1},{sx2},{sy2})",
                    )
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

    def build_hsv_mask(self, hsv_img, hsv_center, hsv_tol):
        h, s, v = hsv_center
        th, ts, tv = hsv_tol
        low_h, high_h = h - th, h + th
        low_s, high_s = max(0, s - ts), min(255, s + ts)
        low_v, high_v = max(0, v - tv), min(255, v + tv)
        if low_h < 0:
            return cv2.inRange(hsv_img, np.array([0, low_s, low_v]), np.array([high_h, high_s, high_v])) + cv2.inRange(
                hsv_img, np.array([180 + low_h, low_s, low_v]), np.array([179, high_s, high_v])
            )
        if high_h > 179:
            return cv2.inRange(hsv_img, np.array([low_h, low_s, low_v]), np.array([179, high_s, high_v])) + cv2.inRange(
                hsv_img, np.array([0, low_s, low_v]), np.array([high_h - 180, high_s, high_v])
            )
        return cv2.inRange(hsv_img, np.array([low_h, low_s, low_v]), np.array([high_h, high_s, high_v]))

    def detect_bars(self, image):
        bars = self.config["bar_regions"]
        prof = self.config["bar_profiles"]
        x1a, y1a, x1b, y1b = self.window_service.denormalize_region(bars["bar1"])
        x2a, y2a, x2b, y2b = self.window_service.denormalize_region(bars["bar2"])
        bar1 = image[y1a:y1b, x1a:x1b]
        bar2 = image[y2a:y2b, x2a:x2b]
        if bar1.size == 0 or bar2.size == 0:
            return 0, 0
        m1 = self.build_hsv_mask(cv2.cvtColor(bar1, cv2.COLOR_BGR2HSV), prof["bar1"]["hsv"], prof["bar1"]["tol"])
        m2 = self.build_hsv_mask(cv2.cvtColor(bar2, cv2.COLOR_BGR2HSV), prof["bar2"]["hsv"], prof["bar2"]["tol"])
        return self._bar_fill_score(m1), self._bar_fill_score(m2)

    def _bar_fill_score(self, mask):
        """
        进度条填充估计（比纯面积占比更稳定）：
        1) area_ratio: 红色像素占比
        2) length_ratio: 红色连续填充长度（按列）
        最终以 length_ratio 为主，area_ratio 为辅，减少“底色噪声导致方向反了”的问题。
        """
        if mask.size == 0:
            return 0.0

        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        bin_mask = (mask > 0).astype(np.uint8)

        # 先开运算去噪点，再闭运算连接断裂的填充区域，减轻单帧闪烁导致的填充率跳变。
        kernel = np.ones((3, 3), np.uint8)
        clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)

        # 每列“填充像素占比”，阈值略提高可减少边缘噪声列被误判为已填充。
        col_ratio = np.mean(clean, axis=0)
        active_cols = np.where(col_ratio > 0.30)[0]
        if active_cols.size == 0:
            return float(np.clip(area_ratio * 0.5, 0.0, 1.0))

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

        # 以长度为主、面积为辅；结果限制在 [0,1]，避免异常帧拉高/拉低 diff。
        score = 0.65 * length_ratio + 0.35 * area_ratio
        return float(np.clip(score, 0.0, 1.0))


def sample_hsv_profile(crop_bgr):
    """校准区域颜色采样：取 HSV 中值，增强抗噪声能力。"""
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    valid = hsv[(hsv[:, 1] > 20) & (hsv[:, 2] > 20)]
    if valid.size == 0:
        valid = hsv
    center = np.median(valid, axis=0).astype(int).tolist()
    return {"hsv": center, "tol": [15, 80, 80]}
