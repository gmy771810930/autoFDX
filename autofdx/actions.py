import pyautogui
from time import monotonic, sleep


class GameActions:
    """负责点击与滚动行为，不关心识别细节。"""

    def __init__(self, config_store, state, window_service, vision_service):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service

    @property
    def config(self):
        return self.config_store.data

    def wait(self, sec):
        sleep(sec / 2)

    def ready_to_start(self):
        return self.vision_service.match("start")

    def ready_to_cum(self):
        return self.vision_service.match(f"cum{self.state.cum_mode}")

    def ready_to_finish(self):
        return self.vision_service.match("finish")

    def _move_mouse_right_after_click(self, key):
        """
        点击后避让鼠标，避免鼠标覆盖按钮区域影响下一帧模板匹配。
        规则：向右移动至少“标定按钮宽度的 3 倍”，并设置更大的最小位移下限。
        同时在移动前后加入短延时，降低连续动作抖动导致的误触发概率。
        """
        left, top, width, _height = self.window_service.get_window_region()
        regions = self.config.get("template_regions", {})
        region = regions.get(key)

        # cum1 通常未单独标定，兼容复用 cum2 的区域宽度估算。
        if region is None and key == "cum1":
            region = regions.get("cum2")

        if region is not None:
            template_w_px = max(1, int((region[2] - region[0]) * width))
            # 右移距离进一步加大：
            # - 至少为模板宽度 3 倍；
            # - 同时设定更高最小值，避免模板较小时位移仍偏小。
            move_x = max(220, template_w_px * 3)
        else:
            # 没有区域信息时退化为固定安全位移。
            move_x = 220

        # 去抖：移动前稍等，给上一轮点击留出稳定时间窗口。
        sleep(0.12)
        # 右移采用“分段 + 带时长”方式，避免一次瞬移过快导致游戏内视角变化不明显。
        # 这里分两段完成，同步增强可见性与稳定性。
        first_step = int(move_x * 0.65)
        second_step = max(1, move_x - first_step)
        pyautogui.moveRel(first_step, 0, duration=0.14)
        sleep(0.06)
        pyautogui.moveRel(second_step, 0, duration=0.14)
        # 去抖：移动后再稍等，避免“刚移动就点击”造成落点不稳定。
        sleep(0.15)

    def start(self):
        pos = self.vision_service.match("start")
        if pos:
            pyautogui.moveTo(pos[0], pos[1])
            self.wait(0.12)
            pyautogui.leftClick()
            self.wait(0.14)
            pyautogui.leftClick()
            self.wait(0.08)
            self._move_mouse_right_after_click("start")

    def cum(self):
        key = f"cum{self.state.cum_mode}"
        pos = self.vision_service.match(key)
        if pos:
            pyautogui.moveTo(pos[0], pos[1])
            self.wait(0.12)
            # 略微拉开双击间隔，降低与游戏输入竞争导致的漏触发。
            pyautogui.click(clicks=2, interval=0.08)
            self.wait(0.08)
            self._move_mouse_right_after_click(key)

    def finish(self):
        pos = self.vision_service.match("finish")
        if pos:
            pyautogui.moveTo(pos[0], pos[1])
            self.wait(0.12)
            pyautogui.leftClick()
            self.wait(0.14)
            pyautogui.leftClick()
            self.wait(0.08)
            self._move_mouse_right_after_click("finish")

    def move_to_scroll_region_center(self):
        r = self.config["scroll_region"]
        left, top, width, height = self.window_service.get_window_region()
        pyautogui.moveTo(int(left + (r[0] + r[2]) * width / 2), int(top + (r[1] + r[3]) * height / 2))

    def _point_by_1based_index(self, points, index_1based):
        """
        将“1-based 点位索引”转换为屏幕绝对坐标。
        返回 None 表示点位不存在（例如未标定或索引越界）。
        """
        if not isinstance(points, list):
            return None
        if (not isinstance(index_1based, int)) or index_1based < 1 or index_1based > len(points):
            return None
        point = points[index_1based - 1]
        if (not isinstance(point, list)) or len(point) != 2:
            return None
        return self.window_service.denormalize_point(point)

    def _click_point(self, abs_point):
        """
        对绝对坐标执行一次稳定左键点击。
        统一加入极短停顿，降低“移动与点击竞争”导致的漏触发。
        """
        if abs_point is None:
            return False
        pyautogui.moveTo(abs_point[0], abs_point[1])
        sleep(0.08)
        pyautogui.leftClick()
        sleep(0.08)
        return True

    def press_experiment_switch_hotkey(self):
        """实验切换入口热键：按下 E。"""
        pyautogui.press("e")
        sleep(0.12)

    def click_experiment_category(self, index_1based):
        """
        点击“实验分类”网格点（6-6-6-1，共 19 点）。
        index_1based 为 1-based 索引。
        """
        point = self._point_by_1based_index(self.config.get("experiment_hex_points", []), index_1based)
        return self._click_point(point)

    def click_body_part(self, index_1based):
        """
        点击“身体部位”网格点（单行 7 点）。
        index_1based 为 1-based 索引。
        """
        point = self._point_by_1based_index(self.config.get("body_part_points", []), index_1based)
        return self._click_point(point)

    def click_experiment_card(self, index_1based):
        """
        点击“实验卡片”网格点（3x4，共 12 点）。
        index_1based 为 1-based 索引。
        """
        point = self._point_by_1based_index(self.config.get("experiment_points", []), index_1based)
        return self._click_point(point)

    def has_experiment_selected_flag(self):
        """
        检测“实验选定标志”是否出现。
        该标志用于区分“卡片可选中”与“实验已用尽”等分支。
        """
        return self.vision_service.match("experiment_selected_flag") is not None

    def wait_experiment_selected_flag(self, timeout_sec=0.5, poll_interval_sec=0.06):
        """
        在给定超时时间内轮询“实验选定标志”是否出现。
        默认超时 500ms，满足你提出的时序要求。
        """
        deadline = monotonic() + max(0.0, float(timeout_sec))
        while monotonic() < deadline:
            if self.has_experiment_selected_flag():
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def wait_start_button(self, timeout_sec=1.0, poll_interval_sec=0.08):
        """
        在给定超时时间内轮询“开始按钮”是否出现。
        返回 True 表示出现；False 表示超时未出现。
        """
        # 统一下限到 1 秒：
        # 你要求“至少 500ms，若原本已是 500ms 则提高到 1s”，
        # 因此这里直接强制最短等待窗口为 1s。
        timeout = max(1.0, float(timeout_sec))
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self.ready_to_start():
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def deploy_experiment_with_retry(self, wait_start_sec=1.0):
        """
        尝试部署实验（对齐流程.md 第4条）：
        左键点击一次，1s 内出现开始按鈕 → 成功；否则直接切换下一个实验（上层处理）。
        """
        print("\n[部署实验] 左键尝试部署，等待开始按鈕...")
        pyautogui.leftClick()
        if self.wait_start_button(timeout_sec=wait_start_sec):
            print("\n[部署实验] 成功：检测到开始按鈕。")
            return True
        print("\n[部署实验] 失败：1s 内未出现开始按鈕，切换下一个实验。")
        return False

    def _click_with_interval(self, x, y, count, interval_sec):
        """
        在固定坐标执行重复点击，并在两次点击之间保持给定间隔。
        该方法用于“点赞用户按钮”的节奏控制，避免点击过快导致漏触发。
        """
        pyautogui.moveTo(x, y)
        # 光标落点后稍等一拍，再点击，降低“移动与点击竞争”导致的漏点。
        sleep(0.08)
        for i in range(count):
            pyautogui.leftClick()
            # 末次点击后不需要再等待，避免引入额外尾部延迟。
            if i < count - 1:
                sleep(interval_sec)

    def give(self):
        points = self.config.get("like_points", [])
        # 点位顺序约定：
        # [0..2] = 用户1/2/3
        # [3..5] = 点赞用户1/2/3
        # 若点位未完整标定，直接跳过，避免点错位置。
        if len(points) < 6:
            return

        # 按业务固定流程执行三组：
        # 用户1 -> 点赞用户1，用户2 -> 点赞用户2，用户3 -> 点赞用户3
        for idx in range(3):
            user_x, user_y = self.window_service.denormalize_point(points[idx])
            like_x, like_y = self.window_service.denormalize_point(points[idx + 3])

            # 第一步：点击“用户N”一次（按你的要求去掉去抖双击）。
            self._click_with_interval(user_x, user_y, count=1, interval_sec=0.12)
            sleep(0.15)

            # 第二步：点击“点赞用户N”三次（略增到 120ms 间隔）。
            self._click_with_interval(like_x, like_y, count=3, interval_sec=0.12)
            sleep(0.15)

            # 第三步：继续点击“点赞用户N”三次（略增到 220ms 间隔）。
            self._click_with_interval(like_x, like_y, count=3, interval_sec=0.22)
            sleep(0.18)
