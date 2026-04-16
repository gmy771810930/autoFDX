import threading
from time import sleep, time

import keyboard
import pyautogui


def _sleep_interruptible(total_sec, state, step_sec=0.05):
    """
    可中断睡眠：在关闭程序时尽快跳出，避免长时间 sleep 阻塞 run_forever 退出。
    """
    total = max(0.0, float(total_sec))
    step = max(0.01, float(step_sec))
    deadline = time() + total
    while time() < deadline:
        if state.stop_requested:
            return
        remain = deadline - time()
        if remain <= 0:
            break
        sleep(min(step, remain))


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
        self._f3_hotkey_handle = None
        self._f11_hotkey_handle = None
        self._f12_hotkey_handle = None
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
        # - _experiment_first_stage_done: 是否已完成“首次启动运行”（E/身体部位）阶段。
        # - _experiment_card_index: 当前准备点击的实验卡片索引（1-based，1~12）。
        self._experiment_switch_bootstrapped = False
        self._experiment_card_index = self._read_card_index_from_config()
        self._experiment_first_stage_done = False
        # 当前使用的身体部位点位号（仅取 2 或 5）；首次启动固定使用 2 号。
        self._current_body_part_index = 2
        # 实验切换模式下的“当前实验已运行次数”计数器：
        # 每满 5 次自动顺延到下一个实验卡片，并重新进入“尝试选定实验”。
        self._experiment_cycle_count = 0
        # 满 5 次后的延迟切换请求：
        # 置位后在“开始按钮再次出现”时，先等待 2s 再切换下一实验。
        self._switch_after_five_on_start_pending = False
        # 标记“本轮已由其他流程提前按过 E 打开面板”，
        # 供 _run_experiment_switch_bootstrap 的重入路径去重使用，避免重复按 E。
        self._experiment_panel_preopened = False
        # F2 手动切换意图：
        # True 表示“用户已按 F2，且当前已暂停；当用户恢复后，应立即调起下一实验切换”。
        # 该标记只负责“恢复后立即切换”的一次性触发，执行后会立刻清零。
        self._f2_pending_switch_after_resume = False
        # 女进度条停滞监测（流程.md 第8条）：
        # 独立线程在正常运行期间持续采样 b1，如超时窗口内始终不增加则置位标志。
        self._female_bar_stall_flag = False
        self._female_bar_monitor_active = False
        self._female_bar_monitor_stop = threading.Event()
        self._female_bar_monitor_thread = None
        # 女进度条停滞检测“可恢复最早时间戳”（秒）：
        # - 正常开局时：用于“点击开始后延时3秒再允许停滞判断”；
        # - 特殊动作后：在“特殊动作按键重新出现”后，再把该值设为“当前+3秒”。
        self._female_bar_stall_suspend_until = 0.0
        # 特殊动作恢复状态：
        # True 表示“已触发主键盘1，正在等待特殊动作按键重新出现”。
        self._female_bar_stall_wait_special_reappear = False
        # 该标记用于确保“重新出现”语义成立：
        # 必须先检测到按键消失，再检测到按键出现，才允许进入3秒恢复倒计时。
        self._female_bar_stall_special_hidden_once = False
        # 特殊动作状态机（按新规则）：
        # - 当“敏感进度条<60% 且 特殊动作按钮红色>60%”时，延迟 500ms 触发主键盘“1”；
        # - 每次触发后，先暂停女进度条停滞检测；
        #   等“特殊动作按键”重新出现后，再延迟 3s 自动恢复判断。
        # 连续触发节流时间戳：避免条件持续满足时每帧都触发按键。
        self._special_action_last_trigger_ts = 0.0
        self._special_action_monitor_active = False
        self._special_action_monitor_stop = threading.Event()
        self._special_action_monitor_thread = None
        # 特殊动作阶段令牌：主线程进入/离开「开始~高潮」区间时更新，防止子线程在
        # press 的 0.5s 延迟后仍发送「1」（阶段已结束或已暂停时偶发误触）。
        self._special_action_phase_token = 0
        self._special_action_expected_token = 0
        # 降低 pyautogui 全局动作间隔，避免滚轮动作被库默认节流。
        # 调整为 0：进一步提升连续滚轮吞吐，解决“滚动距离/次数偏小”的问题。
        pyautogui.PAUSE = 0
        # PyAutoGUI 默认 failsafe：光标停在屏幕四角时，下一次任意操作会抛异常以防失控。
        # 本脚本会把鼠标移到窗口边角安全位（如 safe_move_point）、全屏/无边框下也易贴近物理屏幕角，
        # 与用户手操或特殊动作恢复流程叠加时易误触，导致主循环异常中断（例如特殊动作日志刚打印后崩溃）。
        # 自动化场景关闭 failsafe；紧急停止仍依赖 F1 暂停与悬浮窗「退出脚本」。
        pyautogui.FAILSAFE = False

    def _special_action_should_abort(self):
        """
        若应放弃本次「按 1」，返回 True（用于延迟等待期间与按键前二次校验）。
        覆盖：已暂停、已请求停止/标定、阶段已结束、高潮按钮已出现（主循环即将退出）。
        """
        if not self._special_action_monitor_active:
            return True
        if self._special_action_phase_token != self._special_action_expected_token:
            return True
        if self.state.manual_pause:
            return True
        if self.state.stop_requested or self.state.pending_calibration is not None:
            return True
        try:
            if self.actions.ready_to_cum():
                return True
        except Exception:
            return True
        return False

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
            "recover_stamina_button",
            "experiment_switch",
            "body_part_switch",
        )
        for key in required_flags:
            if not bool(done_map.get(key, False)):
                missing.append(key)

        if len(self.config_store.data.get("experiment_points", [])) != 12 and "experiment_switch" not in missing:
            missing.append("experiment_switch")
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
        1) 按 E -> 点身体部位 2 号（不再点击实验分类）；
        2) 尝试选定实验卡片（从当前索引开始）；
        3) 选定后尝试部署；
        4) 部署失败则切换下一实验并回到步骤 2。
        """
        if self._experiment_switch_bootstrapped:
            return True

        if not self._ensure_experiment_switch_ready():
            return False

        # “首次启动运行”阶段只执行一次；
        # 后续自动切换实验时，直接回到“尝试选定实验”阶段，不重复按 E/点身体部位。
        if not self._experiment_first_stage_done:
            self.state.set_status("首次启动运行")
            # 按需求：首次进入时从实验卡片 1 号点开始。
            self._experiment_card_index = 1
            self._save_card_index_to_config(self._experiment_card_index)
            self.actions.press_experiment_switch_hotkey()
            # 按下 E 后延时 1s，再点击身体部位（流程.md 第2条：延时1秒）。
            sleep(1.0)
            # 身体部位仅使用 2 号与 5 号；首次启动固定先点 2 号。
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
                # 流程.md 第5条：打开实验面板后等待 1s 再进入尝试选定实验阶段。
                sleep(1.0)

        while (not self.state.stop_requested) and (self.state.pending_calibration is None):
            if self._wait_if_paused_or_interrupted():
                return False
            self.state.set_status("尝试选定实验")
            if self._experiment_card_index > 12:
                # 【实验已用尽、12张卡片已全部尝试完毕。
                if self._current_body_part_index != 5:
                    self.state.set_status("实验已用尽：切换到身体部位5号")
                    print(
                        f"\n[实验已用尽] 12张卡片已全部尝试，"
                        f"切换身体部位 {self._current_body_part_index}号→5号，重置实验点位为1。"
                    )
                    self.actions.click_body_part(2)
                    sleep(0.5)
                    self.actions.click_body_part(5)
                    self._current_body_part_index = 5
                    self._experiment_card_index = 1
                    self._save_card_index_to_config(self._experiment_card_index)
                    continue
                # 5号身体部位实验也已用尽，全郠实验完成。
                self.state.manual_pause = True
                self.state.set_status("全部实验已完成，程序暂停")
                print("\n[实验已用尽] 身体部位2号与5号的实验均已用尽，全部实验完成，程序已暂停。")
                return False

            self._save_card_index_to_config(self._experiment_card_index)
            # 流程.md 第3条：进入【尝试选定实验】后延时 1s 再点击实验卡片。
            sleep(1.0)
            print(f"\n[实验切换] 尝试选定实验：准备点击实验卡片索引={self._experiment_card_index}。")
            clicked = self.actions.click_experiment_card(self._experiment_card_index)
            if not clicked:
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 实验卡片点位不可用")
                print(
                    f"\n[实验切换] 已暂停：实验卡片点位不可用，当前索引={self._experiment_card_index}。"
                )
                return False

            # 流程.md 第3条：点击实验卡片后 2s 内检测"实验选定标志"。
            _sel_flag_sec = 2.0
            if not self.actions.wait_experiment_selected_flag(timeout_sec=_sel_flag_sec):
                # 【实验已用尽】当前卡片无法选中（未出现实验选定标志）。
                if self._current_body_part_index != 5:
                    self.state.set_status("实验已用尽：切换到身体部位5号")
                    print(
                        f"\n[实验已用尽] 卡片{self._experiment_card_index}号无法选中，"
                        f"切换身体部位 {self._current_body_part_index}号→5号，重置实验点位为1。"
                    )
                    self.actions.click_body_part(2)
                    sleep(0.5)
                    self.actions.click_body_part(5)
                    self._current_body_part_index = 5
                    self._experiment_card_index = 1
                    self._save_card_index_to_config(self._experiment_card_index)
                    continue
                # 5号身体部位实验也已用尽，全部实验完成。
                self.state.manual_pause = True
                self.state.set_status("全部实验已完成，程序暂停")
                print("\n[实验已用尽] 身体部位2号与5号的实验均已用尽，全部实验完成，程序已暂停。")
                return False

            # ── 流程.md 第4条：【尝试部署实验】──
            self.state.set_status("尝试部署实验")
            # 流程第1步：进入部署阶段后先延时1秒，再点击鼠标左键。
            sleep(1.0)
            start_seen, both_ready = self.actions.deploy_and_check_start_recover(timeout_sec=2.0)
            if both_ready:
                # 部署成功后先等待 1s，再进入正常运行阶段。
                sleep(1.0)
                self._experiment_switch_bootstrapped = True
                self._experiment_cycle_count = 0
                self.state.set_status("实验切换完成")
                print(f"\n[实验切换] 部署成功：实验卡片索引={self._experiment_card_index}。")
                return True
            if start_seen:
                # 开始按钮出现后，必须再满足“恢复体力按钮存在”才算部署成功。
                # 规则：若“恢复体力按钮”不存在，直接判定部署失败并切换下一实验，不做移动视角重试。
                print(
                    f"\n[实验切换] 部署失败：索引={self._experiment_card_index}，"
                    "原因=恢复体力按钮不存在，直接进入【切换下一实验】。"
                )
                self._experiment_card_index += 1
                pyautogui.press("esc")
                sleep(1.0)
                self.actions.press_experiment_switch_hotkey()
                sleep(1.0)
                continue

            # ── 流程.md 第5条：开始按钮未出现 → 尝试【移动视角部署】最多 5 次 ──
            # 注意：每一次“移动视角”后都立即执行一次“左键部署 + 开始按钮判断 + 恢复体力按钮判断”。
            if not start_seen:
                for retry_idx in range(1, 6):
                    if self.state.stop_requested or self.state.pending_calibration is not None:
                        return False
                    if self._wait_if_paused_or_interrupted():
                        return False
                    print(f"\n[移动视角部署] 第{retry_idx}/5 次尝试...")
                    self.state.set_status(f"移动视角部署 ({retry_idx}/5)")
                    self.actions.move_camera_right_sendinput()
                    # 按流程要求：每次移动后等待 0.5s 稳定，再尝试部署。
                    sleep(0.5)
                    print(f"\n[移动视角部署] 第{retry_idx}/5 次：移动完成，立即左键尝试部署。")
                    start_seen, both_ready = self.actions.deploy_and_check_start_recover(timeout_sec=2.0)
                    if both_ready:
                        print(f"\n[移动视角部署] 第{retry_idx}/5 次成功：开始按钮与恢复体力按钮均通过。")
                        # 部署成功后先等待 1s，再进入正常运行阶段。
                        sleep(1.0)
                        self._experiment_switch_bootstrapped = True
                        self._experiment_cycle_count = 0
                        self.state.set_status("实验切换完成")
                        print(f"\n[实验切换] 部署成功：实验卡片索引={self._experiment_card_index}。")
                        return True
                    if start_seen:
                        # 规则：恢复体力按钮不存在 -> 直接切换下一实验，不继续重试。
                        print(
                            f"\n[移动视角部署] 第{retry_idx}/5 次失败：开始按钮已出现但恢复体力按钮不存在，"
                            "直接进入【切换下一实验】。"
                        )
                        self._experiment_card_index += 1
                        pyautogui.press("esc")
                        sleep(1.0)
                        self.actions.press_experiment_switch_hotkey()
                        sleep(1.0)
                        break
                    print(f"\n[移动视角部署] 第{retry_idx}/5 次失败。")
                else:
                    # 5 次移动视角均失败（开始按钮始终未出现）→ 【切换下一实验】。
                    print(
                        f"\n[实验切换] 部署全部失败：索引={self._experiment_card_index}，"
                        "顺延到下一卡片并进入【切换下一实验】。"
                    )
                    self._experiment_card_index += 1
                    pyautogui.press("esc")
                    sleep(1.0)
                    self.actions.press_experiment_switch_hotkey()
                    sleep(1.0)
                    continue

                # 走到这里代表“恢复体力按钮缺失”分支已提前执行切换。
                continue

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

    def _toggle_like_force_next_by_f3(self):
        """
        F3 快捷操作：
        - 切换“结束后执行点赞（一次性）”开关；
        - 与 UI 勾选保持同一配置项（like_force_next）。
        """
        like_enabled = bool(self.config_store.data.get("like_enabled", True))
        if not like_enabled:
            # 点赞功能关闭时，不允许置位“结束后执行点赞”，避免出现看不见的无效状态。
            if bool(self.config_store.data.get("like_force_next", False)):
                self.config_store.data["like_force_next"] = False
                self.config_store.save()
            self.state.set_status("F3忽略：点赞功能未开启")
            print("\n[F3] 已忽略：当前未开启点赞功能。")
            return

        next_value = not bool(self.config_store.data.get("like_force_next", False))
        self.config_store.data["like_force_next"] = next_value
        self.config_store.save()
        if next_value:
            self.state.set_status("F3已开启：结束后执行点赞")
            print("\n[F3] 已开启：结束后执行点赞（一次性）。")
        else:
            self.state.set_status("F3已关闭：结束后执行点赞")
            print("\n[F3] 已关闭：结束后执行点赞。")

    def _toggle_all_calibration_overlay_by_f12(self):
        """
        F12 调试三段式流程：
        1) 首次按下：弹出标定项选择窗口；
        2) 选择完成后再按一次：显示所选标定叠加；
        3) 再按一次：收起叠加并结束本轮。
        """
        phase = str(getattr(self.state, "calibration_overlay_phase", "idle"))
        if phase == "idle":
            self.state.show_all_calibration_overlay = False
            self.state.open_calibration_overlay_selector = True
            self.state.calibration_overlay_phase = "await_selection"
            print("\n[F12] 请选择要显示的标定项。")
            return
        if phase == "await_selection":
            print("\n[F12] 等待完成标定项选择。")
            return
        if phase == "ready_to_show":
            selected = list(getattr(self.state, "calibration_overlay_selected_keys", []))
            if not selected:
                # 防御：无选择时回到第一步
                self.state.open_calibration_overlay_selector = True
                self.state.calibration_overlay_phase = "await_selection"
                print("\n[F12] 未选择任何标定项，请先选择。")
                return
            self.state.show_all_calibration_overlay = True
            self.state.calibration_overlay_phase = "showing"
            print("\n[F12] 已显示选定标定叠加层。")
            return
        if phase == "showing":
            self.state.show_all_calibration_overlay = False
            self.state.calibration_overlay_phase = "idle"
            self.state.calibration_overlay_selected_keys = []
            print("\n[F12] 已收起标定叠加层。")
            return
        # 异常状态兜底
        self.state.show_all_calibration_overlay = False
        self.state.open_calibration_overlay_selector = False
        self.state.calibration_overlay_phase = "idle"
        self.state.calibration_overlay_selected_keys = []
        print("\n[F12] 调试状态已重置。")

    def _replay_pull_new_experiment_scroll_by_f11(self):
        """F11 调试：随时重播“拉出新实验滚动”标定动作。"""
        self.actions.replay_pull_new_experiment_scroll_action(delay_sec=1.0)

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
        仅用于“正常 5 回合切换实验”前的动作（流程.md 的6、5条）：
        按 ESC 退出当前实验；
        延迟 1s 后按 E 打开实验面板；
        最后再等待 1s，交由后续流程进入“尝试选定实验”阶段。
        """
        pyautogui.press("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)

    def _female_bar_stall_monitor_loop(self):
        """
        流程.md 第8条：正常运行时独立线程监测女进度条是否停滞。
        每 ~0.3s 采样一次 b1；若 5s 内 b1 未增加超过 epsilon，则置位停滞标志。
        """
        # 放宽“有增长”的判定门槛：更小的增长也视为有效增长，降低误判停滞概率。
        epsilon = 0.003
        baseline_b1 = None
        baseline_time = None
        while not self._female_bar_monitor_stop.is_set():
            if not self._female_bar_monitor_active or self.state.manual_pause:
                # 未激活或暂停时重置基线，避免恢复后立即误判。
                baseline_b1 = None
                baseline_time = None
                sleep(0.1)
                continue
            # 额外条件：特殊动作触发后暂停停滞判断；
            # 直到“特殊动作按键重新出现 + 延时3秒”后才恢复。
            now = time()
            if now < self._female_bar_stall_suspend_until or self._female_bar_stall_wait_special_reappear:
                baseline_b1 = None
                baseline_time = None
                sleep(0.1)
                continue
            try:
                screen = self.vision_service.capture_screen()
                b1, _ = self.vision_service.detect_bars(screen)
            except Exception:
                sleep(0.3)
                continue
            if baseline_b1 is None:
                baseline_b1 = b1
                baseline_time = now
            elif b1 > baseline_b1 + epsilon:
                # 有增长：刷新基线
                baseline_b1 = b1
                baseline_time = now
            elif now - baseline_time >= 5.0:
                print(f"\n[女进度条监测] 5s 内未增加（b1={b1:.4f}），触发停滞切换。")
                self._female_bar_stall_flag = True
                self._female_bar_monitor_active = False
            sleep(0.3)

    def _start_female_bar_monitor(self):
        """启动女进度条停滞监测线程（守护线程，生命周期跟随主循环）。"""
        if self._female_bar_monitor_thread is None or not self._female_bar_monitor_thread.is_alive():
            self._female_bar_monitor_stop.clear()
            self._female_bar_stall_flag = False
            self._female_bar_monitor_active = False
            self._female_bar_monitor_thread = threading.Thread(
                target=self._female_bar_stall_monitor_loop, daemon=True
            )
            self._female_bar_monitor_thread.start()

    def _stop_female_bar_monitor(self):
        """停止女进度条停滞监测线程。"""
        self._female_bar_monitor_active = False
        self._female_bar_monitor_stop.set()
        if self._female_bar_monitor_thread is not None:
            self._female_bar_monitor_thread.join(timeout=1.0)
        self._female_bar_monitor_thread = None

    def _special_action_monitor_loop(self):
        """
        特殊动作线程：
        仅在“点击开始后~点击高潮前”激活，持续循环判断并触发主键盘“1”。
        """
        while not self._special_action_monitor_stop.is_set():
            if (not self._special_action_monitor_active) or self.state.manual_pause:
                sleep(0.1)
                continue

            sensitive_ratio = self.actions.get_sensitive_progress_bar_ratio()
            if sensitive_ratio is None:
                sleep(0.1)
                continue

            # 恢复顺序（按需求）：
            # 1) 按下“1”后，先等待特殊动作按键“消失 -> 重新出现”；
            # 2) 重新出现后，再额外延迟 3 秒；
            # 3) 最后恢复女进度条停滞判断。
            if self._female_bar_stall_wait_special_reappear:
                if self._special_action_should_abort():
                    sleep(0.1)
                    continue
                # “按键重新出现”用红色占比做可见性近似判断：
                # 阈值取 0.20（低于触发阈值 0.60），尽量放宽“出现”识别，降低误漏检。
                button_visible = self.actions.is_special_action_button_red(threshold=0.20)
                if not self._female_bar_stall_special_hidden_once:
                    # 第一步：等待按键先消失，确保后续“出现”是“重新出现”而非持续可见。
                    if not button_visible:
                        self._female_bar_stall_special_hidden_once = True
                        print("\n[特殊动作] 检测到按键已消失，继续等待按键重新出现。")
                elif button_visible:
                    # 第二步：检测到重新出现后，启动 3 秒恢复倒计时并解除“等待重新出现”状态。
                    self._female_bar_stall_wait_special_reappear = False
                    self._female_bar_stall_special_hidden_once = False
                    self._female_bar_stall_suspend_until = time() + 3.0
                    print("\n[特殊动作] 检测到按键重新出现，开始延时3秒后恢复女进度条停滞判断。")

            # 条件：敏感进度条<60% 且 特殊动作按钮红色占比>60%。
            # 与点击逻辑保持“循环判断”，条件持续满足时可重复触发“1”。
            if sensitive_ratio < 0.60 and self.actions.is_special_action_button_red(threshold=0.60):
                if self._special_action_should_abort():
                    sleep(0.1)
                    continue
                # 触发节流：最多约每 0.8s 触发一次，避免按键洪泛。
                now_ts = time()
                if (now_ts - self._special_action_last_trigger_ts) >= 0.8:
                    if self.actions.press_main_keyboard_one_after_delay(
                        delay_sec=0.5, abort_check=self._special_action_should_abort
                    ):
                        self._special_action_last_trigger_ts = now_ts
                        # 每次触发“1”后，先暂停停滞检测并等待“特殊动作按键重新出现”，
                        # 再额外延迟3秒，最后恢复女进度条停滞检测。
                        self._female_bar_stall_flag = False
                        self._female_bar_stall_suspend_until = 0.0
                        self._female_bar_stall_wait_special_reappear = True
                        self._female_bar_stall_special_hidden_once = False
                        print(
                            f"\n[特殊动作] 已触发“1”：敏感进度条={sensitive_ratio:.3f}，"
                            "暂停女进度条停滞检测，等待特殊动作按键重新出现后再延时3秒恢复。"
                        )
            sleep(0.1)

    def _start_special_action_monitor(self):
        """启动特殊动作线程（守护线程，生命周期跟随主循环）。"""
        if self._special_action_monitor_thread is None or (not self._special_action_monitor_thread.is_alive()):
            self._special_action_monitor_stop.clear()
            self._special_action_monitor_active = False
            self._special_action_last_trigger_ts = 0.0
            self._female_bar_stall_suspend_until = 0.0
            self._female_bar_stall_wait_special_reappear = False
            self._female_bar_stall_special_hidden_once = False
            self._special_action_monitor_thread = threading.Thread(
                target=self._special_action_monitor_loop, daemon=True
            )
            self._special_action_monitor_thread.start()

    def _stop_special_action_monitor(self):
        """停止特殊动作线程。"""
        self._special_action_monitor_active = False
        self._special_action_monitor_stop.set()
        if self._special_action_monitor_thread is not None:
            self._special_action_monitor_thread.join(timeout=1.0)
        self._special_action_monitor_thread = None

    def _recover_after_female_bar_stall(self, bar_balance_tolerance):
        """
        按“女进度条停滞恢复流程”执行恢复并重启当前实验：
        1) 先按一次 ESC，等待开始按钮；
        2) 若 3 秒内未出现开始按钮，再补按一次 ESC 后继续等待；
        3) 开始按钮出现后等待 2 秒；
        4) 持续检测女/男进度条占比，满足以下任一条件后循环点击开始按钮：
           - 两者差值 <= 20%（近似相等）；
           - 两者占比都为 0（视为相等）；
           - 女进度条 > 男进度条 且 女进度条 < 60%（允许继续运行）。
        """
        print("\n[女进度条停滞] 执行恢复：ESC → 等待开始按钮（3秒内未出现则再按一次ESC）→ 等待2秒 → 双条近似相等后点击开始。")
        self.state.set_status("女进度条停滞：恢复中")
        # 停滞恢复场景单独放宽判定：按需求固定使用 20% 容差。
        near_equal_tolerance = max(float(bar_balance_tolerance), 0.20)

        pyautogui.press("esc")
        # 首次等待窗口：3 秒内若开始按钮未出现，按需求补按一次 ESC。
        if not self.actions.wait_start_button(timeout_sec=3.0, poll_interval_sec=0.10):
            pyautogui.press("esc")
            self.state.set_status("女进度条停滞：二次ESC后等待开始按钮")
            while not self.actions.ready_to_start():
                if self._wait_if_paused_or_interrupted():
                    return False
                sleep(0.2)

        # 保险等待：即便 3 秒内已出现，也统一进入“开始按钮稳定后再操作”节奏。
        self.state.set_status("女进度条停滞：开始按钮已出现，等待2秒")
        while not self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return False
            sleep(0.2)
        sleep(2.0)

        # 按需求：在开始按钮可见阶段，循环等待“双条近似相等/可放行”。
        self.state.set_status("女进度条停滞：等待双进度条近似相等")
        while True:
            if self._wait_if_paused_or_interrupted():
                return False
            if not self.actions.ready_to_start():
                # 若过程中开始按钮短暂消失，回到等待，避免误触发。
                sleep(0.2)
                continue
            try:
                b1, b2 = self.vision_service.detect_bars(self.vision_service.capture_screen())
            except Exception:
                sleep(0.15)
                continue
            # 条件1：常规“近似相等”判定（差值 <= 20%）。
            near_equal = abs(b1 - b2) <= near_equal_tolerance
            # 条件2：两者都为 0 视为相等；用极小阈值兼容浮点噪声。
            both_zero = (b1 <= 0.001) and (b2 <= 0.001)
            # 条件3：开始按钮出现 2 秒后，若女条略高但女条本身 <60%，也允许继续。
            female_ahead_but_low = (b1 > b2) and (b1 < 0.60)
            if near_equal or both_zero or female_ahead_but_low:
                break
            sleep(0.12)

        # “循环点击开始按钮”：按钮还在就持续点击，直到进入正常运行阶段。
        while self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return False
            clicked = self.actions.start()
            if not clicked:
                # 去抖点击未达成“按钮稳定消失”时，不推进后续动作，避免误进入下一阶段。
                self.state.log("停滞恢复：开始按钮点击未确认，重试")
                sleep(0.12)
                continue
            self.state.log("停滞恢复：点击开始")
            x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
            pyautogui.moveTo(x, y)
            sleep(0.2)

        return True

    def _register_hotkeys(self):
        # suppress=False 保持 F1 原生行为不被拦截，仅增加脚本暂停能力。
        if self._f1_hotkey_handle is None:
            self._f1_hotkey_handle = keyboard.add_hotkey("f1", self._toggle_pause_by_f1, suppress=False)
        if self._f2_hotkey_handle is None:
            self._f2_hotkey_handle = keyboard.add_hotkey("f2", self._pause_and_switch_next_experiment_by_f2, suppress=False)
        if self._f3_hotkey_handle is None:
            self._f3_hotkey_handle = keyboard.add_hotkey("f3", self._toggle_like_force_next_by_f3, suppress=False)
        if self._f11_hotkey_handle is None:
            self._f11_hotkey_handle = keyboard.add_hotkey("f11", self._replay_pull_new_experiment_scroll_by_f11, suppress=False)
        if self._f12_hotkey_handle is None:
            self._f12_hotkey_handle = keyboard.add_hotkey("f12", self._toggle_all_calibration_overlay_by_f12, suppress=False)

    def _unregister_hotkeys(self):
        if self._f1_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f1_hotkey_handle)
            self._f1_hotkey_handle = None
        if self._f2_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f2_hotkey_handle)
            self._f2_hotkey_handle = None
        if self._f3_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f3_hotkey_handle)
            self._f3_hotkey_handle = None
        if self._f11_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f11_hotkey_handle)
            self._f11_hotkey_handle = None
        if self._f12_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f12_hotkey_handle)
            self._f12_hotkey_handle = None
        # 兜底：移除本进程注册的其余钩子，避免关闭后热键仍驻留导致“像没退出”。
        try:
            keyboard.unhook_all()
        except Exception:
            pass

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
            self._current_body_part_index = 2
            self._switch_after_five_on_start_pending = False
        else:
            if not self._ensure_experiment_switch_ready():
                sleep(0.2)
                return
            if not self._run_experiment_switch_bootstrap():
                sleep(0.2)
                return
            # 满 5 次后不立刻切换，等"开始按钮出现"再等待 2s，然后按点位号分支处理。
            if self._switch_after_five_on_start_pending:
                self.state.set_status("5次完成：等待开始按钮后切换")
                while not self.actions.ready_to_start():
                    if self._wait_if_paused_or_interrupted():
                        return
                    sleep(0.2)
                sleep(2.0)
                self._switch_after_five_on_start_pending = False
                self._experiment_switch_bootstrapped = False

                if self._experiment_card_index == 12:
                    # 流程.md 【当页实验全部完成】：
                    # 当前实验是本页最后一个（点位号=12），需先退出实验面板，
                    # 拉出新实验滚动，再将点位号置为9，重新进入【尝试选定实验】。
                    self.state.set_status("当页实验全部完成：拉出新实验")
                    print("\n[实验切换] 点位号=12，执行【当页实验全部完成】：ESC → E → 拉出新实验滚动 → 点位置9。")
                    pyautogui.press("esc")
                    sleep(1.0)
                    self.actions.press_experiment_switch_hotkey()
                    sleep(1.0)
                    # 拉出新实验滚动，将下一页实验列表推入视野。
                    self.actions.replay_pull_new_experiment_scroll_action(delay_sec=0.0)
                    # 下次从第9个实验卡片开始选定。
                    self._experiment_card_index = 9
                    self._experiment_cycle_count = 0
                    self._save_card_index_to_config(self._experiment_card_index)
                    # 面板已在上方按 E 打开，告知 bootstrap 不要重复按 E。
                    self._experiment_panel_preopened = True
                else:
                    # 流程.md 【切换下一实验】（点位号≠12）：
                    # 点赞流程已在 loop_once 末尾的 give() 调用中完成；此处直接执行切换。
                    self.state.set_status("实验5次完成，执行切换实验")
                    print(f"\n[实验切换] 点位号={self._experiment_card_index}，执行【切换下一实验】。")
                    self._experiment_card_index += 1
                    self._experiment_cycle_count = 0
                    self._reopen_experiment_panel_with_esc()
                    # 标记本轮已按过 E，避免下次进入 bootstrap 重复按键。
                    self._experiment_panel_preopened = True
                return

        # 进度条平衡容差（作用于平滑后的 diff，见下方 EMA）：
        # 调整为更细更及时：减小死区、提高采样频率。
        bar_balance_tolerance = 0.010
        balance_check_interval_sec = 0.30
        bar_fill_ema_alpha = 0.52
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
            clicked = self.actions.start()
            if not clicked:
                # 关键：若本轮未确认成功（按钮未稳定消失），留在当前阶段继续重试。
                # 这样可抑制“模板瞬时抖动 -> 误判已点击 -> 流程跳转”的问题。
                self.state.log("开始按钮点击未确认，重试")
                sleep(0.12)
                continue
            self.state.log("点击开始")
            x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
            pyautogui.moveTo(x, y)
            sleep(0.2)

        # “点击开始后~点击高潮前”阶段：
        # 若发生女进度条停滞，则按新规则执行恢复，并在恢复后继续留在当前实验。
        while True:
            # 每轮“等待高潮”前重置平滑状态，避免停滞恢复后沿用旧轮次数据。
            next_balance_check_at = time()
            bar_fill_ema_b1 = None
            bar_fill_ema_b2 = None
            # 流程.md 第8条：正常运行时启动女进度条停滞监测（仅实验切换模式）。
            if experiment_switch_enabled:
                self._female_bar_stall_flag = False
                self._female_bar_monitor_active = True
                # 新规则：女进度条停滞判断仅在“点击开始按钮后延迟3秒”才生效。
                self._female_bar_stall_suspend_until = time() + 3.0
            self._female_bar_stall_wait_special_reappear = False
            self._female_bar_stall_special_hidden_once = False
            # 新一轮「开始~高潮」阶段：刷新令牌，使旧线程中待发送的「1」全部失效。
            self._special_action_phase_token += 1
            self._special_action_expected_token = self._special_action_phase_token
            self._special_action_monitor_active = True

            # 仅在“点击开始后~点击高潮前”阶段启用滚轮纠偏。
            self._scroll_enabled = True
            stall_detected = False
            try:
                while not self.actions.ready_to_cum():
                    if self._wait_if_paused_or_interrupted():
                        return
                    # 流程.md 第8条：检查停滞标志
                    if experiment_switch_enabled and self._female_bar_stall_flag:
                        stall_detected = True
                        break
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
                            # 更细分档：在中小差值区间提供更细腻纠偏。
                            if ad > 0.18:
                                scroll_count = 24
                            elif ad > 0.12:
                                scroll_count = 20
                            elif ad > 0.08:
                                scroll_count = 16
                            elif ad > 0.05:
                                scroll_count = 12
                            elif ad > 0.025:
                                scroll_count = 9
                            else:
                                scroll_count = 6

                            # 临近满条时略加大纠偏，但增量减半以减轻末端抖动。
                            if max(bar_fill_ema_b1, bar_fill_ema_b2) > 0.85 and abs(diff) > bar_balance_tolerance:
                                scroll_count += 4

                            scroll_count = max(4, min(28, scroll_count))

                            # 速度纠偏：滚轮方向相对上一版整体取反（pyautogui.scroll 正数为常见“向上滚”语义）。
                            if diff > 0:
                                # 女进度条 > 男进度条：原方向取反后使用 +360。
                                if self.state.debug:
                                    print(f"[bar] action=scroll_up sign=+ (female ahead) count={scroll_count}")
                                self._set_scroll_command(+360, scroll_count)
                            else:
                                # 女进度条 < 男进度条：原方向取反后使用 -360。
                                if self.state.debug:
                                    print(f"[bar] action=scroll_down sign=- (female behind) count={scroll_count}")
                                self._set_scroll_command(-360, scroll_count)
                        else:
                            # 差值已在容差内：暂停副线程滚轮输出，避免多余扰动。
                            self._clear_scroll_command()
                        next_balance_check_at = now + balance_check_interval_sec
                    # 主线程优先响应按钮检测，轮询频率高于滚轮参数刷新频率。
                    sleep(0.06)
            finally:
                # 无论正常进入高潮、手动中断或异常，都立即停滚轮并停用停滞监测。
                self._scroll_enabled = False
                self._clear_scroll_command()
                self._female_bar_monitor_active = False
                self._special_action_monitor_active = False
                # 离开阶段：递增令牌，使特殊动作线程内任何未完成的延迟按键被取消。
                self._special_action_phase_token += 1

            if not stall_detected:
                break

            if not self._recover_after_female_bar_stall(bar_balance_tolerance=bar_balance_tolerance):
                return

        while self.actions.ready_to_cum():
            if self._wait_if_paused_or_interrupted():
                return
            clicked = self.actions.cum()
            if not clicked:
                self.state.log("高潮按钮点击未确认，重试")
                self.actions.wait(0.12)
                continue
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
            clicked = self.actions.finish()
            if not clicked:
                self.state.log("结束按钮点击未确认，重试")
                self.actions.wait(0.12)
                continue
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
            # 先按“原有逻辑”处理点赞，再等待“开始按钮出现后 2s”执行切换。
            if experiment_switch_enabled:
                self._experiment_cycle_count += 1
                if self._experiment_cycle_count >= 5:
                    self._experiment_cycle_count = 0
                    self._switch_after_five_on_start_pending = True
                    self.state.set_status("实验5次完成，等待开始按钮后切换")
                    return

    def run_forever(self):
        self._register_hotkeys()
        self._start_scroll_worker()
        self._start_female_bar_monitor()
        self._start_special_action_monitor()
        # 原固定 sleep(2) 会在用户立刻关闭窗口时仍阻塞 2 秒，延迟释放热键与退出。
        _sleep_interruptible(2.0, self.state)
        if not self.state.stop_requested:
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
                    # 分段睡眠，便于 stop_requested 后尽快结束循环
                    _sleep_interruptible(0.2, self.state)
                    continue

                try:
                    self.loop_once()
                except Exception as exc:
                    self.state.set_status(f"异常: {exc}")
                    print(f"\n发生异常：{exc}")
                    _sleep_interruptible(1.0, self.state)
        finally:
            self._scroll_enabled = False
            self._clear_scroll_command()
            self._stop_scroll_worker()
            self._stop_female_bar_monitor()
            self._stop_special_action_monitor()
            self._unregister_hotkeys()
