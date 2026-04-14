import pyautogui

from .config import WINDOW_TITLE


class WindowService:
    """负责窗口定位和坐标转换。"""

    def __init__(self, config_store):
        self.config_store = config_store

    @property
    def config(self):
        return self.config_store.data

    def get_game_window(self):
        title = self.config.get("window_title", WINDOW_TITLE)
        wins = pyautogui.getWindowsWithTitle(title)
        if not wins:
            raise RuntimeError(f"未找到窗口：{title}")
        return wins[0]

    def get_window_region(self):
        win = self.get_game_window()
        return win.left, win.top, win.width, win.height

    def denormalize_region(self, region):
        _, _, width, height = self.get_window_region()
        x1 = int(region[0] * width)
        y1 = int(region[1] * height)
        x2 = int(region[2] * width)
        y2 = int(region[3] * height)
        x1, x2 = sorted((max(0, x1), min(width, x2)))
        y1, y2 = sorted((max(0, y1), min(height, y2)))
        return x1, y1, x2, y2

    def denormalize_point(self, point):
        left, top, width, height = self.get_window_region()
        return int(left + point[0] * width), int(top + point[1] * height)
