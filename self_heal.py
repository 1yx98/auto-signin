# -*- coding: utf-8 -*-
"""
self_heal.py — 轻量纯规则自愈模块（不依赖 LLM，也不调外部 API）

核心思路：
  1. failure_key：对失败步骤+错误码去重，相同故障识别为同一类
  2. 预热动作库：已知 failure_code → 下次运行前自动执行对应清理/延长等待
  3. 冷却：同一 failure_code 连续 3 次失败后暂停预热，避免无效循环
  4. 成功后清零：签到成功则重置连续失败计数

设计取舍：
  - 不调用 LLM（不依赖外部 API，离线可用）
  - 不自动改源码（风险高，由人工判断）
  - 预热动作仅限安全操作：杀进程、延长等待、清理小程序引擎
  - 所有动作 try/except，异常绝不影响主流程

状态文件：.agent/heal_state.json
信号文件：.agent/_extend_locate_wait、.agent/_wait_longer_detail（signin.py 读取）
"""

import json
import os
import subprocess
import sys
import time
import hashlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
STATE_DIR = ROOT / ".agent"
STATE_PATH = STATE_DIR / "heal_state.json"
SIGNAL_EXTEND_LOCATE = STATE_DIR / "_extend_locate_wait"
SIGNAL_WAIT_DETAIL = STATE_DIR / "_wait_longer_detail"

# 已知 failure_code → 预热动作（动作名在 _do_action 里实现）
FAILURE_ACTIONS = {
    "SEARCH_OPEN_FAIL":   ["kill_wechat_restart"],
    "WX_ENTER_TIMEOUT":   ["kill_wechat_restart"],
    "MAP_LOCATE_TIMEOUT": ["extend_locate_wait", "kill_miniprogram_engine"],
    "MAP_CANNOT_ENTER":   ["kill_miniprogram_engine"],
    "DETAIL_NO_BUTTON":   ["wait_longer_detail"],
    "NAV_ENTRY_FAIL":     ["kill_miniprogram_engine"],
    "CONFIRM_TIMEOUT":    [],  # 已有退出重进处理，无需预热
    "PRE_LOCKED":         [],
    "LIST_ALREADY_SIGNED":[],  # 这是成功短路，不是失败
    "DETAIL_ALREADY_SIGNED": [],
}

COOLDOWN_THRESHOLD = 3  # 同一 failure_code 连续失败 N 次后暂停预热


def _load():
    state = None
    try:
        if STATE_PATH.exists():
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
    except Exception:
        state = None
    if not isinstance(state, dict):
        state = {}
    # 补齐缺键：旧版本或手工改过的 heal_state.json 可能没有这几个键，
    # 而 record_result() 里是 state["history"].append(...) 直接取键——
    # 一旦 KeyError 就被外层 except 吞掉，状态从此永远写不进去，自愈静默停摆。
    state.setdefault("last_failure", None)
    if not isinstance(state.get("consecutive"), dict):
        state["consecutive"] = {}
    if not isinstance(state.get("history"), list):
        state["history"] = []
    return state


def _save(state):
    """保存自愈状态。

    【2026-09-16 修复·P1-9】两处改动：

    1) **写失败不再完全静默**（原来 `except Exception: pass`）。
       后果链条（已实测确认）：_save 静默失败 → state 不落盘 →
       下次 _load 读到旧的/空 state → preheat() 认为"无上次失败记录"跳过预热 →
       **自愈永久停摆，而日志里一行提示都没有**。
       这与本项目"失败要暴露不能掩盖"的铁律直接冲突。
       现在往 stderr 说一句（不给 logger：本模块要保持可独立 import）。

    2) **原子写盘**（临时文件 + os.replace）。
       原来直接 `open(STATE_PATH, "w")` 覆盖写：进程正好在此刻被强杀，
       会留下**半截 JSON**。下次 _load 解析失败 → state={} →
       连续失败计数（consecutive）被清零 → 冷却机制失效，
       同一个故障会被无限预热。用临时文件 + os.replace 就不会有半截文件。
       这与 _guard_mark_in_progress() / step_tracer.flush() 的做法保持一致。
    """
    tmp = str(STATE_PATH) + ".tmp"
    try:
        STATE_DIR.mkdir(exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        # 清掉可能的半截临时文件（失败也无所谓）
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        try:
            sys.stderr.write("[自愈] 状态写入失败（自愈将退化为'无上次失败记录'，"
                             "预热不再生效）: %s: %s\n" % (type(e).__name__, e))
            sys.stderr.flush()
        except Exception:
            pass


def _kill_process(name):
    """安全杀进程，返回是否杀到了。

    【2026-09-16 修复·编码】补 encoding="gbk", errors="ignore"。
    中文 Windows 上 taskkill 的输出是 GBK，而 text=True 默认按 UTF-8 解码，
    会**在 subprocess 的 reader 子线程里**抛 UnicodeDecodeError。
    这个异常不会传到本函数的 `except Exception`（它在另一个线程里），
    而是被 Python 直接打到 stderr 成一段 traceback。

    危害（实测复现过）：
      · 每次预热杀进程，stderr 就多两段 traceback；
      · signin.py 的 run.log **收集 stderr** → 日志被 traceback 淹没，
        真正的失败信息被埋掉，排查方向被带偏。
    signin.py 里同类调用（L573/L662/L897）早就加了 encoding，这里是遗漏。
    """
    try:
        r = subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, text=True,
                           encoding="gbk", errors="ignore", timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _do_action(action, log):
    """执行单个预热动作，返回描述。"""
    try:
        if action == "kill_wechat_restart":
            killed = _kill_process("Weixin.exe")
            log.append(f"kill_wechat_restart: 杀微信进程 {'成功' if killed else '未运行'}")
            time.sleep(2.5)
        elif action == "kill_miniprogram_engine":
            killed = _kill_process("WeChatAppEx.exe")
            log.append(f"kill_miniprogram_engine: 杀小程序引擎 {'成功' if killed else '未运行'}")
            time.sleep(1)
        elif action == "extend_locate_wait":
            SIGNAL_EXTEND_LOCATE.write_text("1", encoding="utf-8")
            log.append("extend_locate_wait: 已设置信号，下次定位等待延长到120秒")
        elif action == "wait_longer_detail":
            SIGNAL_WAIT_DETAIL.write_text("1", encoding="utf-8")
            log.append("wait_longer_detail: 已设置信号，下次详情页等待延长到20秒")
        else:
            log.append(f"未知动作: {action}")
    except Exception as e:
        log.append(f"{action} 异常（忽略）: {e}")


def preheat():
    """运行前预热：根据上次失败的 failure_code 执行对应动作。
    在 main() 最开头调用。返回预热日志列表（供 signin.py 记录）。"""
    log = []
    try:
        state = _load()
        last = state.get("last_failure")
        if not last:
            log.append("无上次失败记录，跳过预热")
            return log
        fcode = last.get("failure_code", "")
        consec = state.get("consecutive", {}).get(fcode, 0)
        if consec >= COOLDOWN_THRESHOLD:
            log.append(f"故障 {fcode} 已连续失败 {consec} 次，进入冷却，跳过预热（请人工检查）")
            return log
        actions = FAILURE_ACTIONS.get(fcode, [])
        if not actions:
            log.append(f"上次故障 {fcode} 无对应预热动作")
            return log
        log.append(f"[自愈预热] 上次故障={fcode}（连续{consec}次），执行 {len(actions)} 个预热动作")
        for a in actions:
            _do_action(a, log)
    except Exception as e:
        log.append(f"预热异常（忽略）: {e}")
    return log


def consume_signal(name):
    """signin.py 读取并消费信号文件（读一次即删，避免永久生效）。
    name: 'extend_locate_wait' 或 'wait_longer_detail'"""
    try:
        p = SIGNAL_EXTEND_LOCATE if name == "extend_locate_wait" else SIGNAL_WAIT_DETAIL
        if p.exists():
            p.unlink()
            return True
    except Exception:
        pass
    return False


def clear_signals():
    """【2026-09-16 新增】清掉所有残留的自愈信号。

    为什么需要：信号的语义是"**上次失败后**这一次运行的补偿"。
    但 consume_signal() 只在流程真正走到那一步时才被调用：

        DETAIL_WAIT = 20 if self_heal.consume_signal("wait_longer_detail") else 12
        LOCATE_WAIT = 120 if self_heal.consume_signal("extend_locate_wait") else 75

    于是就有了残留窗口（实测确认）：
        · 上次失败了，preheat 设了 extend_locate_wait
        · 这次运行因为**已经签到成功 / 不在时段**而快速返回，
          根本没走到定位步骤 → consume_signal 没被调用
        · 信号文件留在磁盘上，**下次运行会继续按"延长等待"跑**
    代价：每轮定位多等 45 秒（120 vs 75），而系统其实早已恢复正常 ——
    纯属把一次性的补偿变成了永久性的拖慢。

    现在由 signin.py 在收尾（finally）调用一次，无条件清干净。
    放在收尾而不是开头，是因为开头的 preheat 正要写这些信号 ——
    在开头清会把刚设的补偿也清掉。
    """
    cleared = []
    for nm, p in (("extend_locate_wait", SIGNAL_EXTEND_LOCATE),
                  ("wait_longer_detail", SIGNAL_WAIT_DETAIL)):
        try:
            if p.exists():
                p.unlink()
                cleared.append(nm)
        except Exception:
            pass
    return cleared


def record_result(exit_code, failure_step=None, failure_code=None):
    """运行结束后记录结果。exit_code: 0=成功 3=非时段 1/2=失败。
    在 main() finally 里调用。"""
    try:
        state = _load()
        now = datetime.now().isoformat(timespec="seconds")
        if exit_code == 0:
            # 成功：清零连续失败
            state["last_failure"] = None
            state["consecutive"] = {}
            state["history"].append({"time": now, "result": "success", "exit_code": 0})
            log_msg = "[自愈] 签到成功，已清零连续失败计数"
        elif exit_code == 3:
            state["history"].append({"time": now, "result": "not_time", "exit_code": 3})
            log_msg = "[自愈] 非签到时段，不更新故障状态"
        else:
            # 失败：记录
            fkey = f"{failure_step or 'unknown'}:{failure_code or 'unknown'}"
            state["last_failure"] = {
                "time": now, "failure_step": failure_step,
                "failure_code": failure_code, "failure_key": fkey,
            }
            consec = state.get("consecutive", {})
            consec[failure_code or "unknown"] = consec.get(failure_code or "unknown", 0) + 1
            state["consecutive"] = consec
            state["history"].append({
                "time": now, "result": "fail", "exit_code": exit_code,
                "failure_step": failure_step, "failure_code": failure_code,
            })
            log_msg = f"[自愈] 记录失败 {failure_step}/{failure_code}（连续{consec.get(failure_code or 'unknown', 0)}次）"
        # 只保留最近 50 条历史
        state["history"] = state["history"][-50:]
        _save(state)
        return log_msg
    except Exception as e:
        return f"[自愈] 记录结果异常（忽略）: {e}"
