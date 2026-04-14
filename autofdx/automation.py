import threading
from time import sleep, time

import keyboard
import pyautogui


class AutomationEngine:
    """自动流程主循环。"""

    def __init__(self, config_store, state, window_service, vision_service, actions):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service
        self.actions = actions
        self._f1_hotkey_handle = None
        self._f2_hotkey_handle = None
        # 滚轮副线程控制状态：
        # 主线程负责识别与点击；副线程仅根据主线程下发的滚轮指令持续滚动。
        self._scroll_stop_event = threading.Event()
        self._scroll_lock = threading.Lock()
        self._scroll_enabled = False
        self._scroll_amount = 0
        self._scroll_batch = 0
        self._scroll_thread = None
        # “实验切换”流程状态：
        # - _experiment_switch_bootstrapped: 当前实验是否已“选定+部署完成”，可直接跑主流程。
        # - _experiment_first_stage_done: 是否已完成“首次启动运行”（E/实验分类/身体部位）阶段。
        # - _experiment_card_index: 当前准备点击的实验卡片索引（1-based，1~12）。
        self._experiment_switch_bootstrapped = False
        self._experiment_card_index = self._read_card_index_from_config()
        self._experiment_first_stage_done = False
        # 实验切换模式下的“当前实验已运行次数”计数器：
        # 每满 5 次自动顺延到下一个实验卡片，并重新进入“尝试选定实验”。
        self._experiment_cycle_count = 0
        # 标记“本轮已由其他流程提前按过 E 打开面板”，
        # 供 _run_experiment_switch_bootstrap 的重入路径去重使用，避免重复按 E。
        self._experiment_panel_preopened = False
        # F2 手动切换意图：
        # True 表示“用户已按 F2，且当前已暂停；当用户恢复后，应立即调起下一实验切换”。
        # 该标记只负责“恢复后立即切换”的一次性触发，执行后会立刻清零。
        self._f2_pending_switch_after_resume = False
        # 降低 pyautogui 全局动作间隔，避免滚轮动作被库默认节流。
        # 调整为 0：进一步提升连续滚轮吞吐，解决“滚动距离/次数偏小”的问题。
        pyautogui.PAUSE = 0

    def _read_card_index_from_config(self):
        """
        从 current_experiment([行,列])推导实验卡片一维索引（1~12）。
        非法配置自动回退到 1。
        """
        cur = self.config_store.data.get("current_experiment", [1, 1])
        if (not isinstance(cur, list)) or len(cur) != 2:
            return 1
        row, col = cur[0], cur[1]
        if (not isinstance(row, int)) or (not isinstance(col, int)):
            return 1
        if row < 1 or row > 3 or col < 1 or col > 4:
            return 1
        return (row - 1) * 4 + col

    def _save_card_index_to_config(self, idx_1based):
        """
        将实验卡片一维索引写回 current_experiment([行,列])，便于 UI 与配置同步展示。
        """
        idx = max(1, min(12, int(idx_1based)))
        row = (idx - 1) // 4 + 1
        col = (idx - 1) % 4 + 1
        cur = self.config_store.data.get("current_experiment", [1, 1])
        if cur != [row, col]:
            self.config_store.data["current_experiment"] = [row, col]
            self.config_store.save()

    def _missing_experiment_switch_calibrations(self):
        """
        返回“实验切换流程”缺失的标定项 key 列表。
        除 calibration_done 外，也会校验网格点数量是否完整。
        """
        done_map = self.config_store.data.get("calibration_done", {})
        missing = []
        required_flags = (
            "experiment_selected_flag",
            "experiment_switch",
            "experiment_hex_switch",
            "body_part_switch",
        )
        for key in required_flags:
            if not bool(done_map.get(key, False)):
                missing.append(key)

        if len(self.config_store.data.get("experiment_points", [])) != 12 and "experiment_switch" not in missing:
            missing.append("experiment_switch")
        if len(self.config_store.data.get("experiment_hex_points", [])) != 19 and "experiment_hex_switch" not in missing:
            missing.append("experiment_hex_switch")
        if len(self.config_store.data.get("body_part_points", [])) != 7 and "body_part_switch" not in missing:
            missing.append("body_part_switch")
        return missing

    def _ensure_experiment_switch_ready(self):
        """
        实验切换前置校验：
        开关开启时，必须先完成实验相关标定，否则强制保持暂停。
        """
        if not bool(self.config_store.data.get("experiment_switch_enabled", False)):
            return True

        missing = self._missing_experiment_switch_calibrations()
        if not missing:
            return True

        self.state.manual_pause = True
        self.state.set_status("实验切换缺少标定")
        print(f"\n[实验切换] 缺少标定项：{', '.join(missing)}，已自动暂停。")
        return False

    def _run_experiment_switch_bootstrap(self):
        """
        实验切换开启后的首次运行流程：
        1) 按 E -> 点实验分类 2 号 -> 点身体部位 2 号；
        2) 尝试选定实验卡片（从当前索引开始）；
        3) 选定后尝试部署（最多 5 轮）；
        4) 部署失败则右键一次并顺延实验卡片，回到步骤 2。
        """
        if self._experiment_switch_bootstrapped:
            return True

        if not self._ensure_experiment_switch_ready():
            return False

        # “首次启动运行”阶段只执行一次；
        # 后续自动切换实验时，直接回到“尝试选定实验”阶段，不重复按 E/点分类/点身体部位。
        if not self._experiment_first_stage_done:
            self.state.set_status("首次启动运行")
            # 按需求：首次进入时从实验卡片 1 号点开始。
            self._experiment_card_index = 1
            self._save_card_index_to_config(self._experiment_card_index)
            self.actions.press_experiment_switch_hotkey()
            # 按需求：按下 E 后等待 500ms，再点击实验分类。
            sleep(0.5)
            if not self.actions.click_experiment_category(2):
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 实验分类点位不可用")
                return False
            # 按需求：点击实验分类后等待 500ms，再点击身体部位。
            sleep(0.5)
            # 按当前需求，身体部位仅使用 2 号与 5 号；首次启动固定先点 2 号。
            if not self.actions.click_body_part(2):
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 身体部位点位不可用")
                return False
            self._experiment_first_stage_done = True
        else:
            # 非首次重入“尝试选定实验”：
            # - 若前序流程已提前按过 E（例如：5 回合切换/部署失败回退），这里不重复按；
            # - 否则按一次 E 打开实验面板。
            if self._experiment_panel_preopened:
                self._experiment_panel_preopened = False
            else:
                self.state.set_status("重新打开实验面板")
                self.actions.press_experiment_switch_hotkey()
                sleep(0.5)

        while (not self.state.stop_requested) and (self.state.pending_calibration is None):
            if self._wait_if_paused_or_interrupted():
                return False
            self.state.set_status("尝试选定实验")
            if self._experiment_card_index > 12:
                self.state.manual_pause = True
                self.state.set_status("实验已用尽（待实现）")
                print(
                    f"\n[实验切换] 已暂停：实验卡片索引={self._experiment_card_index} 超出上限12，"
                    "进入“实验已用尽（待实现）”。"
                )
                return False

            self._save_card_index_to_config(self._experiment_card_index)
            # 文档要求：点击实验卡片前先延时 500ms。
            sleep(0.5)
            print(f"\n[实验切换] 尝试选定实验：准备点击实验卡片索引={self._experiment_card_index}。")
            clicked = self.actions.click_experiment_card(self._experiment_card_index)
            if not clicked:
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 实验卡片点位不可用")
                print(
                    f"\n[实验切换] 已暂停：实验卡片点位不可用，当前索引={self._experiment_card_index}。"
                )
                return False

            # 文档要求：点击实验卡片后等待 1000ms 检测实验选定标志。
            if not self.actions.wait_experiment_selected_flag(timeout_sec=1.0):
                # 按你的规则：未出现“实验选定标志”直接进入“实验已用尽（待实现）”。
                self.state.manual_pause = True
                self.state.set_status("实验已用尽（待实现）")
                print(
                    f"\n[实验切换] 已暂停：点击卡片索引={self._experiment_card_index} 后，"
                    "1s内未检测到“实验选定标志”（按规则视为实验已用尽）。"
                )
                return False

            self.state.set_status("尝试部署实验")
            deployed = self.actions.deploy_experiment_with_retry(wait_start_sec=1.0)
            if deployed:
                self._experiment_switch_bootstrapped = True
                self._experiment_cycle_count = 0
                self.state.set_status("实验切换完成")
                print(f"\n[实验切换] 部署成功：实验卡片索引={self._experiment_card_index}。")
                return True

            # 部署失败（1s 内未出现开始按鈕）：直接执行「切换下一实验」。
            print(
                f"\n[实验切换] 部署失败：索引={self._experiment_card_index}，"
                "顺延到下一卡片并进入「切换下一实验」。"
            )
            self._experiment_card_index += 1
            # 「切换下一实验」：ESC → 延时1000ms → E → 等待1s。
            pyautogui.press("esc")
            sleep(1.0)
            self.actions.press_experiment_switch_hotkey()
            sleep(1.0)

        return False

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
            # 若此前由 F2 声明了“恢复后立即切换下一实验”，
            # 则恢复状态文案要明确提示“下一步会先切换”，避免用户误判脚本会先继续当前实验。
            if self._f2_pending_switch_after_resume:
                self.state.set_status("F1恢复运行，准备切换下一实验")
                print("\n[F1] 已恢复自动流程，将立即切换下一实验。")
            else:
                self.state.set_status("F1恢复运行")
                print("\n[F1] 已恢复自动流程。")

    def _wait_if_paused_or_interrupted(self):
        """
        在流程执行中统一处理“暂停/中断”：
        - stop_requested 或进入标定（pending_calibration）时，返回 True 让上层中断当前流程；
        - manual_pause 时阻塞等待，直到用户恢复；
        - 若恢复后存在 F2 的“立即切换下一实验”请求，返回 True 终止当前阶段，
          让主循环马上进入“切换下一实验”分支。
        """
        if self.state.stop_requested or self.state.pending_calibration is not None:
            return True

        while self.state.manual_pause:
            if self.state.stop_requested or self.state.pending_calibration is not None:
                return True
            sleep(0.1)
        # 关键点：F2 触发后，恢复时立即打断当前阶段，避免继续执行旧流程，
        # 从而保证“恢复后马上调起切换下一实验”。
        if self._f2_pending_switch_after_resume:
            return True
        return False

    def _pause_and_switch_next_experiment_by_f2(self):
        """
        F2 快捷操作（新规则）：
        - 立即进入暂停；
        - 打上“恢复后立即切换下一实验”的一次性标记；
        - 用户恢复（通常按 F1）后，主循环会优先执行切换分支。
        """
        experiment_switch_enabled = bool(self.config_store.data.get("experiment_switch_enabled", False))
        self.state.manual_pause = True
        if not experiment_switch_enabled:
            # 未开启实验切换时不保留待切换标记，避免恢复后触发无意义分支。
            self._f2_pending_switch_after_resume = False
            self.state.set_status("F2已暂停（实验切换未开启）")
            print("\n[F2] 已暂停；当前未开启实验切换，恢复后不会执行切换。")
            return
        self._f2_pending_switch_after_resume = True
        self.state.set_status("F2已暂停，恢复后切换下一实验")
        print("\n[F2] 已暂停，恢复后将立即切换下一实验。")

    def _switch_next_experiment_after_f2_resume(self):
        """
        仅用于 F2 场景下“恢复后立即切换下一实验”：
        - 实验卡片顺延一次；
        - 实验计数清零；
        - 不等待 2s，直接执行【切换下一实验】定义动作：
          ESC -> 延迟 1000ms -> E -> 等待 1s；
        - 标记面板已预开，避免 bootstrap 重复按 E。
        """
        self._experiment_cycle_count = 0
        self._experiment_card_index += 1
        self._experiment_switch_bootstrapped = False
        self.state.set_status("F2恢复后：切换下一实验")
        pyautogui.press("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)
        self._experiment_panel_preopened = True

    def _reopen_experiment_panel_with_esc(self):
        """
        仅用于“正常 5 回合切换实验”前的动作：
        点赞结束后，先等待 2s；
        再按 ESC 退出当前实验；
        延迟 1000ms 后按 E 打开实验面板；
        最后再等待 1s，交由后续流程进入“尝试选定实验”阶段。
        """
        sleep(2.0)
        pyautogui.press("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)

    def _register_hotkeys(self):
        # suppress=False 保持 F1 原生行为不被拦截，仅增加脚本暂停能力。
        if self._f1_hotkey_handle is None:
            self._f1_hotkey_handle = keyboard.add_hotkey("f1", self._toggle_pause_by_f1, suppress=False)
        if self._f2_hotkey_handle is None:
            self._f2_hotkey_handle = keyboard.add_hotkey("f2", self._pause_and_switch_next_experiment_by_f2, suppress=False)

    def _unregister_hotkeys(self):
        if self._f1_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f1_hotkey_handle)
            self._f1_hotkey_handle = None
        if self._f2_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f2_hotkey_handle)
            self._f2_hotkey_handle = None

    def loop_once(self):
        experiment_switch_enabled = bool(self.config_store.data.get("experiment_switch_enabled", False))
        # F2 一次性“恢复后切换”入口：
        # 该分支优先级最高，确保恢复后先切换，再决定是否进入主流程。
        if self._f2_pending_switch_after_resume:
            if not experiment_switch_enabled:
                # 若恢复前用户关闭了开关，则取消这次待切换请求，避免误动作。
                self._f2_pending_switch_after_resume = False
                self.state.set_status("F2待切换取消：实验切换未开启")
                sleep(0.2)
                return
            if not self._ensure_experiment_switch_ready():
                sleep(0.2)
                return
            self._switch_next_experiment_after_f2_resume()
            self._f2_pending_switch_after_resume = False
            return

        if not experiment_switch_enabled:
            # 开关关闭时重置“首次启动运行”状态；下次再开启会重新走首次流程。
            self._experiment_switch_bootstrapped = False
            self._experiment_card_index = self._read_card_index_from_config()
            self._experiment_first_stage_done = False
            self._experiment_cycle_count = 0
        else:
            if not self._ensure_experiment_switch_ready():
                sleep(0.2)
                return
            if not self._run_experiment_switch_bootstrap():
                sleep(0.2)
                return

        # 进度条平衡容差（作用于平滑后的 diff，见下方 EMA）：
        # 单帧 HSV 掩码易抖动，若死区过小会在阈值两侧来回穿越 → 滚轮方向频繁反转“抽风”。
        bar_balance_tolerance = 0.014
        balance_check_interval_sec = 0.45
        next_balance_check_at = time()
        # 双进度条填充率的指数平滑：降低单帧噪声对 diff 的影响。
        bar_fill_ema_b1 = None
        bar_fill_ema_b2 = None
        bar_fill_ema_alpha = 0.38
        # 强约束：默认关闭滚轮，仅在“点击开始后~点击高潮前”临时开启。
        self._scroll_enabled = False
        self._clear_scroll_command()

        while not self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return
            self.state.log("等待开始")
            sleep(0.2)

        while self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
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
                if self._wait_if_paused_or_interrupted():
                    return
                self.state.log("等待高潮")
                now = time()
                # 高频闭环：约每 0.4 秒做一次纠偏，避免进度条差距扩散过快。
                # 约定固定为：
                # - b1 = 女进度条（原上方进度条）
                # - b2 = 男进度条（原下方进度条）
                if now >= next_balance_check_at:
                    b1, b2 = self.vision_service.detect_bars(self.vision_service.capture_screen())
                    # EMA：用平滑后的填充率算 diff，抑制单帧跳变导致的纠偏方向抖动。
                    if bar_fill_ema_b1 is None:
                        bar_fill_ema_b1, bar_fill_ema_b2 = b1, b2
                    else:
                        a = bar_fill_ema_alpha
                        bar_fill_ema_b1 = a * b1 + (1.0 - a) * bar_fill_ema_b1
                        bar_fill_ema_b2 = a * b2 + (1.0 - a) * bar_fill_ema_b2
                    diff = bar_fill_ema_b1 - bar_fill_ema_b2
                    if self.state.debug:
                        print(
                            f"\n[bar] raw f={b1:.4f} m={b2:.4f} | "
                            f"ema f={bar_fill_ema_b1:.4f} m={bar_fill_ema_b2:.4f} diff={diff:.4f}"
                        )

                    # 仅当平滑后的 |diff| 超出死区才纠偏；力度随 |diff| 分档，并整体压低批次避免过冲。
                    if abs(diff) > bar_balance_tolerance:
                        self.actions.move_to_scroll_region_center()

                        ad = abs(diff)
                        # 分档略收敛：原先单档最高 36+8 易过冲振荡。
                        if ad > 0.15:
                            scroll_count = 22
                        elif ad > 0.10:
                            scroll_count = 18
                        elif ad > 0.06:
                            scroll_count = 14
                        elif ad > 0.03:
                            scroll_count = 10
                        else:
                            scroll_count = 6

                        # 临近满条时略加大纠偏，但增量减半以减轻末端抖动。
                        if max(bar_fill_ema_b1, bar_fill_ema_b2) > 0.85 and abs(diff) > bar_balance_tolerance:
                            scroll_count += 4

                        scroll_count = max(4, min(26, scroll_count))

                        # pyautogui.scroll 的正负与部分游戏/引擎（含本游戏）对滚轮方向的约定相反：
                        # 此前女落后男时本应“向下”纠偏却表现为向上，故在此对纠偏方向取反。
                        if diff > 0:
                            # 女进度条(上) > 男进度条(下)：需提高男侧相对速度 → 向游戏内“下滚”一侧纠偏。
                            if self.state.debug:
                                print(f"[bar] action=scroll_down (female ahead) count={scroll_count}")
                            self._set_scroll_command(-360, scroll_count)
                        else:
                            # 男进度条(下) > 女进度条(上)：女落后 → 向游戏内“上滚”一侧纠偏。
                            if self.state.debug:
                                print(f"[bar] action=scroll_up (female behind) count={scroll_count}")
                            self._set_scroll_command(+360, scroll_count)
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
            if self._wait_if_paused_or_interrupted():
                return
            self.actions.cum()
            self.state.log("点击高潮")
            # 高潮阶段点击优先速度，缩短间隔。
            self.actions.wait(0.1)

        while not self.actions.ready_to_finish():
            if self._wait_if_paused_or_interrupted():
                return
            self.state.log("等待结束")
            self.actions.wait(0.2)

        while self.actions.ready_to_finish():
            if self._wait_if_paused_or_interrupted():
                return
            self.actions.finish()
            self.state.log("点击结束")
            self.actions.wait(0.2)
            with self.state.lock:
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

            # 新规则（按流程文档）：
            # 在实验切换模式下，正常运行满 5 回合后，
            # 先按“原有逻辑”处理点赞，再执行 ESC -> 500ms -> E，回到尝试选定实验。
            if experiment_switch_enabled:
                self._experiment_cycle_count += 1
                if self._experiment_cycle_count >= 5:
                    self._experiment_cycle_count = 0
                    self._experiment_card_index += 1
                    self._experiment_switch_bootstrapped = False
                    self.state.set_status("实验5次完成，准备切换实验")
                    self._reopen_experiment_panel_with_esc()
                    # 标记本轮已按过 E，避免下次进入 bootstrap 重复按键。
                    self._experiment_panel_preopened = True
                    return

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
