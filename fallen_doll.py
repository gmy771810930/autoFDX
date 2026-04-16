import atexit
import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

import keyboard

from autofdx.actions import GameActions
from autofdx.automation import AutomationEngine
from autofdx.config import ConfigStore
from autofdx.state import RuntimeState
from autofdx.ui import launch_floating_window
from autofdx.vision_service import VisionService
from autofdx.window_service import WindowService


def hide_console_window_if_needed():
    """
    Windows 双击启动时隐藏控制台窗口，避免出现黑色 CMD 框。
    """
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            # 0 = SW_HIDE
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        # 隐藏失败不影响主流程。
        pass


def setup_error_logging():
    """
    初始化错误日志：
    - 日志文件: logs/error.log
    - 记录未捕获异常与线程异常，便于排查用户现场问题。
    """
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "error.log"

    logging.basicConfig(
        filename=str(log_path),
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )

    def _excepthook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.exception("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    def _thread_excepthook(args):
        logging.exception(
            "Unhandled thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def _release_keyboard_hooks_safely():
    """
    进程退出前释放 keyboard 全局钩子。
    即使工作线程未跑完 finally，也避免热键残留导致“关了窗口进程还在”的错觉。
    """
    try:
        keyboard.unhook_all()
    except Exception:
        pass


def main():
    config_store = ConfigStore()
    config_store.load()

    state = RuntimeState()
    window_service = WindowService(config_store)
    vision_service = VisionService(config_store, state, window_service)
    actions = GameActions(config_store, state, window_service, vision_service)
    engine = AutomationEngine(config_store, state, window_service, vision_service, actions)

    # 使用 daemon 线程：关闭 UI 后主线程可结束进程；若 run_forever 未及时退出，不会无限挂死。
    # 热键释放由 run_forever.finally、atexit 与下方显式 unhook 三层兜底。
    worker = threading.Thread(target=engine.run_forever, daemon=True, name="autofdx-automation")
    worker.start()
    atexit.register(_release_keyboard_hooks_safely)

    launch_floating_window(config_store, state, window_service)

    # 悬浮窗 on_exit 已置 stop_requested；此处再写一次，防止其它路径关闭窗口时遗漏。
    state.stop_requested = True
    worker.join(timeout=5.0)
    _release_keyboard_hooks_safely()
    if worker.is_alive():
        logging.error(
            "后台自动化线程在 5s 内未结束，进程仍将退出（daemon）；若频繁出现请检查 loop_once 是否阻塞。"
        )


if __name__ == "__main__":
    setup_error_logging()
    hide_console_window_if_needed()
    try:
        main()
    except Exception:
        logging.exception("Fatal error in main()")
        raise
