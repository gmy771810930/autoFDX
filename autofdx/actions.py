import pyautogui
from time import sleep


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
        规则：向右移动至少“标定按钮宽度的 2 倍”，并设置一个最小位移下限。
        """
        left, top, width, _height = self.window_service.get_window_region()
        regions = self.config.get("template_regions", {})
        region = regions.get(key)

        # cum1 通常未单独标定，兼容复用 cum2 的区域宽度估算。
        if region is None and key == "cum1":
            region = regions.get("cum2")

        if region is not None:
            template_w_px = max(1, int((region[2] - region[0]) * width))
            # 按你的要求：至少为标定图像宽度的 2 倍，同时设定最小值防止过小。
            move_x = max(120, template_w_px * 2)
        else:
            # 没有区域信息时退化为固定安全位移。
            move_x = 120

        # 只向右移动，不改纵向位置，减少额外抖动。
        pyautogui.moveRel(move_x, 0)

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
