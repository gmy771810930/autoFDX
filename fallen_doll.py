import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

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


def main():
    config_store = ConfigStore()
    config_store.load()

    state = RuntimeState()
    window_service = WindowService(config_store)
    vision_service = VisionService(config_store, state, window_service)
    actions = GameActions(config_store, state, window_service, vision_service)
    engine = AutomationEngine(config_store, state, window_service, vision_service, actions)

    # 使用非 daemon 线程，主窗口关闭后等待后台线程结束，避免调试器里出现“退出后像重启”。
    worker = threading.Thread(target=engine.run_forever, daemon=False)
    worker.start()

    launch_floating_window(config_store, state, window_service)

    state.stop_requested = True
    worker.join(timeout=2.0)


if __name__ == "__main__":
    setup_error_logging()
    hide_console_window_if_needed()
    try:
        main()
    except Exception:
        logging.exception("Fatal error in main()")
        raise
