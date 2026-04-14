import json
import shutil
from pathlib import Path

WINDOW_TITLE = "FallenDoll"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "user_config.json"
ASSETS_DIR = PROJECT_ROOT / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"

CALIBRATION_ITEMS = [
    ("start", "开始按钮"),
    ("cum2", "高潮按钮"),
    ("finish", "再来一次按钮"),
    ("bar_female", "女进度条"),
    ("bar_male", "男进度条"),
    ("scroll_area", "调速区域"),
    ("like1", "用户1"),
    ("like2", "用户2"),
    ("like3", "用户3"),
    ("like4", "点赞用户1"),
    ("like5", "点赞用户2"),
    ("like6", "点赞用户3"),
]


def build_default_done_flags():
    return {key: False for key, _ in CALIBRATION_ITEMS}


def build_default_config():
    # 配置集中在一个函数内，便于后续迁移/版本升级。
    return {
        "window_title": WINDOW_TITLE,
        "template_match": {
            "default_threshold": 0.95,
            "scales": [0.8, 0.9, 1.0, 1.1, 1.2],
        },
        "template_thresholds": {
            "start": 0.95,
            "finish": 0.95,
            "cum1": 0.95,
            "cum2": 0.95,
        },
        "template_search_margin": 40,
        "template_regions": {},
        "bar_regions": {
            "bar1": [0.07, 0.75, 0.18, 0.79],
            "bar2": [0.07, 0.79, 0.18, 0.83],
        },
        "bar_profiles": {
            "bar1": {"hsv": [0, 200, 200], "tol": [15, 80, 80]},
            "bar2": {"hsv": [0, 200, 200], "tol": [15, 80, 80]},
        },
        "scroll_region": [0.46, 0.42, 0.54, 0.58],
        "safe_move_point": [0.95, 0.92],
        "ui_window_pos": [20, 20],
        "custom_templates": {},
        "like_points": [],
        "like_enabled": True,
        "like_force_next": False,
        # 叠加层模式开关（预留）：
        # False=常规悬浮窗；True=用户选择 DX Hook/Overlay 路径（实验项）。
        "overlay_dx_hook_enabled": False,
        "calibration_done": build_default_done_flags(),
        "calibration_rects": {
            "start": [0.35, 0.82, 0.45, 0.89],
            "cum2": [0.45, 0.82, 0.55, 0.89],
            "finish": [0.55, 0.82, 0.65, 0.89],
            "bar_female": [0.07, 0.75, 0.18, 0.79],
            "bar_male": [0.07, 0.79, 0.18, 0.83],
            "scroll_area": [0.46, 0.42, 0.54, 0.58],
            "like1": [0.05, 0.15, 0.12, 0.20],
            "like2": [0.05, 0.22, 0.12, 0.27],
            "like3": [0.05, 0.29, 0.12, 0.34],
            "like4": [0.05, 0.36, 0.12, 0.41],
            "like5": [0.05, 0.43, 0.12, 0.48],
            "like6": [0.05, 0.50, 0.12, 0.55],
        },
    }


class ConfigStore:
    """配置读写与默认值补齐。"""

    def __init__(self):
        self.data = {}

    def load(self):
        self._ensure_asset_dirs()
        # 兼容历史路径：若根目录存在旧 user_config.json，则迁移到 data 目录。
        legacy_config = PROJECT_ROOT / "user_config.json"
        if (not CONFIG_PATH.exists()) and legacy_config.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_config), str(CONFIG_PATH))
        default = build_default_config()
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data = self._merge(default, loaded)
        else:
            self.data = default
            self.save()
        self._ensure_calibration_keys()
        self._migrate_legacy_assets()
        self.save()
        return self.data

    def save(self):
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def _merge(self, base, override):
        merged = json.loads(json.dumps(base))
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def _ensure_calibration_keys(self):
        self.data.setdefault("calibration_done", {})
        self.data.setdefault("calibration_rects", {})
        defaults = build_default_config()

        # 兼容旧配置：若历史存在 bar_area，则自动拆为女/男两个进度条区域。
        old_bar_area = self.data["calibration_rects"].get("bar_area")
        if old_bar_area and ("bar_female" not in self.data["calibration_rects"] or "bar_male" not in self.data["calibration_rects"]):
            x1, y1, x2, y2 = old_bar_area
            mid = (y1 + y2) / 2.0
            self.data["calibration_rects"]["bar_female"] = [x1, y1, x2, mid]
            self.data["calibration_rects"]["bar_male"] = [x1, mid, x2, y2]
            old_done = bool(self.data["calibration_done"].get("bar_area", False))
            self.data["calibration_done"]["bar_female"] = old_done
            self.data["calibration_done"]["bar_male"] = old_done

        for key, _ in CALIBRATION_ITEMS:
            self.data["calibration_done"].setdefault(key, False)
            self.data["calibration_rects"].setdefault(key, defaults["calibration_rects"][key])

        # 清理已废弃的旧标定项，避免后续维护混淆。
        self.data["calibration_done"].pop("bar_area", None)
        self.data["calibration_rects"].pop("bar_area", None)
        # 清理已废弃点赞重复配置：点赞流程已改为固定节奏，不再使用该字段。
        self.data.pop("like_click_repeat", None)
        # 点赞开关与“下一次立即点赞”标记补默认值。
        self.data.setdefault("like_enabled", True)
        self.data.setdefault("like_force_next", False)
        # DX Hook/Overlay 选择项补默认值。
        self.data.setdefault("overlay_dx_hook_enabled", False)

    def _ensure_asset_dirs(self):
        # 固定数据/素材目录，避免散落在项目根目录。
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy_assets(self):
        # 自动迁移历史素材文件到 assets/templates。
        for name in ("start.png", "cum2.png", "finish.png"):
            src = PROJECT_ROOT / name
            dst = TEMPLATES_DIR / name
            self._move_if_needed(src, dst)

        for src in PROJECT_ROOT.glob("custom_*.png"):
            dst = TEMPLATES_DIR / src.name
            self._move_if_needed(src, dst)

        # 兼容旧配置：custom_templates 里只存文件名时，自动改成素材目录相对路径。
        custom_map = self.data.get("custom_templates", {})
        for key, rel in list(custom_map.items()):
            p = Path(rel)
            if p.parent == Path("."):
                custom_map[key] = str(Path("assets") / "templates" / p.name)
        self.data["custom_templates"] = custom_map

    def _move_if_needed(self, src, dst):
        if not src.exists():
            return
        if dst.exists():
            src.unlink(missing_ok=True)
            return
        shutil.move(str(src), str(dst))
