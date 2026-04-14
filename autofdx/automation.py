import threading
from time import sleep, time

import keyboard
import pyautogui
import winsound


def compare_bars(v1, v2):
    if v1 > v2:
        return 1
    if v1 < v2:
        return 2
    return 0


class AutomationEngine:
    """自动流程主循环。"""

    def __init__(self, config_store, state, window_service, vision_service, actions):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service
        self.actions = actions
        self._f1_hotkey_handle = None
        # 滚轮副线程控制状态：
        # 主线程负责识别与点击；副线程仅根据主线程下发的滚轮指令持续滚动。
        self._scroll_stop_event = threading.Event()
        self._scroll_lock = threading.Lock()
        self._scroll_enabled = False
        self._scroll_amount = 0
        self._scroll_batch = 0
        self._scroll_thread = None
        # 降低 pyautogui 全局动作间隔，避免滚轮动作被库默认节流。
        # 调整为 0：进一步提升连续滚轮吞吐，解决“滚动距离/次数偏小”的问题。
        pyautogui.PAUSE = 0

    def _scroll_worker_loop(self):
        """
        滚轮副线程：
        - 不做识别、不做点击，只消费主线程下发的滚轮参数；
        - 任何时刻主线程都可通过 _scroll_enabled 快速启停。
        """
        while (not self.state.stop_requested) and (not self._scroll_stop_event.is_set()):
            if (not self._scroll_enabled) or self.state.manual_pause:
                sleep(0.01)
                continue

            with self._scroll_lock:
                amount = int(self._scroll_amount)
                batch = int(self._scroll_batch)

            if amount == 0 or batch <= 0:
                sleep(0.005)
                continue

            for _ in range(batch):
                if (not self._scroll_enabled) or self.state.manual_pause or self._scroll_stop_event.is_set():
                    break
                pyautogui.scroll(amount)

    def _start_scroll_worker(self):
        if self._scroll_thread is None or (not self._scroll_thread.is_alive()):
            self._scroll_stop_event.clear()
            self._scroll_thread = threading.Thread(target=self._scroll_worker_loop, daemon=True)
            self._scroll_thread.start()

    def _stop_scroll_worker(self):
        self._scroll_stop_event.set()
        if self._scroll_thread is not None:
            self._scroll_thread.join(timeout=1.0)
        self._scroll_thread = None

    def _set_scroll_command(self, amount, batch):
        with self._scroll_lock:
            self._scroll_amount = int(amount)
            self._scroll_batch = int(batch)

    def _clear_scroll_command(self):
        with self._scroll_lock:
            self._scroll_amount = 0
            self._scroll_batch = 0

    def play_sound(self):
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)

    def _toggle_pause_by_f1(self):
        """
        F1 紧急开关：
        - 按一次暂停自动流程
        - 再按一次恢复流程
        """
        # 标定层期间保持暂停，避免误恢复导致鼠标继续被脚本接管。
        if str(self.state.current_status).startswith("标定中"):
            print("\n[F1] 当前处于标定模式，忽略恢复请求。")
            return

        self.state.manual_pause = not self.state.manual_pause
        if self.state.manual_pause:
            self.state.set_status("F1紧急暂停")
            print("\n[F1] 已暂停自动流程。")
        else:
            self.state.set_status("F1恢复运行")
            print("\n[F1] 已恢复自动流程。")

    def _register_hotkeys(self):
        # suppress=False 保持 F1 原生行为不被拦截，仅增加脚本暂停能力。
        if self._f1_hotkey_handle is None:
            self._f1_hotkey_handle = keyboard.add_hotkey("f1", self._toggle_pause_by_f1, suppress=False)

    def _unregister_hotkeys(self):
        if self._f1_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f1_hotkey_handle)
            self._f1_hotkey_handle = None

    def loop_once(self):
        # 进度条平衡容差：
        # 两条进度条填充率差值在该范围内视为“已基本同步”，不再滚动。
        bar_balance_tolerance = 0.006
        balance_check_interval_sec = 0.4
        next_balance_check_at = time()
        # 强约束：默认关闭滚轮，仅在“点击开始后~点击高潮前”临时开启。
        self._scroll_enabled = False
        self._clear_scroll_command()

        while not self.actions.ready_to_start():
            if self.state.should_interrupt():
                return
            self.state.log("等待开始")
            sleep(0.2)

        while self.actions.ready_to_start():
            if self.state.should_interrupt():
                return
            self.actions.start()
            self.state.log("点击开始")
            x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
            pyautogui.moveTo(x, y)
            sleep(0.2)

        # 仅在“点击开始后~点击高潮前”阶段启用滚轮纠偏。
        self._scroll_enabled = True
        try:
            while not self.actions.ready_to_cum():
                if self.state.should_interrupt():
                    return
                self.state.log("等待高潮")
                now = time()
                # 高频闭环：约每 0.4 秒做一次纠偏，避免进度条差距扩散过快。
                # 约定固定为：
                # - b1 = 女进度条（原上方进度条）
                # - b2 = 男进度条（原下方进度条）
                if now >= next_balance_check_at:
                    b1, b2 = self.vision_service.detect_bars(self.vision_service.capture_screen())
                    diff = b1 - b2
                    if self.state.debug:
                        print(f"\n[bar] female(top)={b1:.4f} male(bottom)={b2:.4f} diff={diff:.4f}")

                    # 只要差值超出容差，就按差值大小进行“持续微调”。
                    # 宗旨：让两条进度条逐步趋同，而不是只在方向变化时调一次。
                    if abs(diff) > bar_balance_tolerance:
                        self.actions.move_to_scroll_region_center()

                        # 差值越大，滚动次数越多；按“更激进档”提升每轮滚动总量。
                        if abs(diff) > 0.15:
                            scroll_count = 36
                        elif abs(diff) > 0.10:
                            scroll_count = 28
                        elif abs(diff) > 0.06:
                            scroll_count = 22
                        elif abs(diff) > 0.03:
                            scroll_count = 16
                        else:
                            scroll_count = 10

                        # 临近满条时，轻微差值也会导致失败，额外加大纠偏力度。
                        if max(b1, b2) > 0.85 and abs(diff) > bar_balance_tolerance:
                            scroll_count += 8

                        if diff > 0:
                            # 女进度条(上) > 男进度条(下)：向上滚动，提高男进度条速度。
                            if self.state.debug:
                                print(f"[bar] action=scroll_up count={scroll_count}")
                            self._set_scroll_command(+360, scroll_count)
                        else:
                            # 男进度条(下) > 女进度条(上)：向下滚动，降低男进度条速度。
                            if self.state.debug:
                                print(f"[bar] action=scroll_down count={scroll_count}")
                            self._set_scroll_command(-360, scroll_count)
                    else:
                        # 差值已在容差内：暂停副线程滚轮输出，避免多余扰动。
                        self._clear_scroll_command()
                    next_balance_check_at = now + balance_check_interval_sec
                # 主线程优先响应按钮检测，轮询频率高于滚轮参数刷新频率。
                sleep(0.08)
        finally:
            # 无论正常进入高潮、手动中断或异常，都立即停滚轮。
            self._scroll_enabled = False
            self._clear_scroll_command()

        while self.actions.ready_to_cum():
            if self.state.should_interrupt():
                return
            self.actions.cum()
            self.state.log("点击高潮")
            # 高潮阶段点击优先速度，缩短间隔。
            self.actions.wait(0.1)

        while not self.actions.ready_to_finish():
            if self.state.should_interrupt():
                return
            self.state.log("等待结束")
            self.actions.wait(0.2)

        while self.actions.ready_to_finish():
            if self.state.should_interrupt():
                return
            self.actions.finish()
            self.state.log("点击结束")
            self.actions.wait(0.2)
            with self.state.lock:
                self.state.i += 1
                self.state.like_cycle_count += 1

            like_enabled = bool(self.config_store.data.get("like_enabled", True))
            force_next_like = bool(self.config_store.data.get("like_force_next", False))
            # 点赞触发规则：
            # 1) 功能开关开启
            # 2) 满足“每5次主流程一次”或“立即执行点赞（一次性）”
            should_like = like_enabled and (force_next_like or (self.state.like_cycle_count % 5 == 0))
            if should_like:
                self.actions.give()
                # 立即执行点赞为一次性触发：消费后自动清除并持久化。
                if force_next_like:
                    # 按需求：只有“本次流程结束后实际调起点赞”时才清零计数，
                    # 从而保证下一次点赞始终需要完整 5 个回合。
                    with self.state.lock:
                        self.state.like_cycle_count = 0
                    self.config_store.data["like_force_next"] = False
                    self.config_store.save()

    def run_forever(self):
        self._register_hotkeys()
        self._start_scroll_worker()
        sleep(2)
        self.state.set_status("初始化完成")
        try:
            while not self.state.stop_requested:
                if self.state.manual_pause:
                    if self.state.current_status not in (
                        "F1紧急暂停",
                        "标定中",
                        "取消标定",
                    ) and not self.state.current_status.startswith("已应用标定"):
                        self.state.set_status("手动暂停")
                    sleep(0.2)
                    continue

                if self.state.i >= 5:
                    self.state.set_status("循环暂停提示")
                    print("\n____________\n已暂停程序，三秒后自动恢复。\n你可以按下空格中止程序")
                    self.play_sound()
                    start_time = time()
                    space_pressed = False
                    while time() - start_time < 5:
                        if self.state.stop_requested or self.state.manual_pause or self.state.pending_calibration is not None:
                            break
                        if keyboard.is_pressed("space"):
                            space_pressed = True
                            print("____________\n已中止程序，等待按下回车键恢复程序")
                            break
                        sleep(0.1)

                    if space_pressed:
                        while True:
                            if self.state.stop_requested or self.state.manual_pause or self.state.pending_calibration is not None:
                                break
                            if keyboard.is_pressed("enter"):
                                print("____________\n已按下回车键，恢复程序")
                                with self.state.lock:
                                    self.state.i = 0
                                break
                            sleep(0.1)

                    if (not space_pressed) and (time() - start_time >= 5):
                        print("____________\n三秒内未按下空格键，继续执行程序")
                        with self.state.lock:
                            self.state.i = 0

                try:
                    self.loop_once()
                except Exception as exc:
                    self.state.set_status(f"异常: {exc}")
                    print(f"\n发生异常：{exc}")
                    sleep(1)
        finally:
            self._scroll_enabled = False
            self._clear_scroll_command()
            self._stop_scroll_worker()
            self._unregister_hotkeys()
