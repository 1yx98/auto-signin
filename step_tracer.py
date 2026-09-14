# -*- coding: utf-8 -*-
"""
step_tracer.py — 签到流程的纯观察层步骤轨迹记录器

记录一次签到运行中每一步的耗时、结果与失败码，供事后定位用。

设计原则：
  1. 纯观察：只记录，不改变任何签到动作、等待时间、点击顺序、成功/失败判定。
  2. 零副作用：所有方法 try/except 包裹，任何记录异常只静默忽略，绝不抛出、绝不影响主流程。
  3. 零业务依赖：不 import signin / pyautogui / cv2，可被后续自愈系统、AI 诊断独立 import 复用。
  4. 原子写盘：临时文件 + os.replace，避免写一半被读到。

输出：每次签到运行目录下的 step_trace.json（机器可读），与 run.log（人工排查）并存。
"""

import json
import os
import time
from datetime import datetime


class StepTracer:
    SCHEMA_VERSION = "1.0"

    def __init__(self, run_id, run_dir, source_sha256=None):
        self.run_id = run_id
        self.run_dir = run_dir
        self.source_sha256 = source_sha256
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self._t0 = time.time()
        self.rounds = []
        self._current_round = None
        self._current_step = None
        self._round_t0 = None
        self._step_t0 = None

    # ---------- round ----------
    def begin_round(self, rnd):
        try:
            self._round_t0 = time.time()
            self._current_round = {
                "round": rnd,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": None,
                "elapsed_ms": None,
                "status": "running",
                "steps": [],
            }
            self.rounds.append(self._current_round)
        except Exception:
            pass

    def end_round(self, status):
        try:
            if self._current_round is None:
                return
            self._current_round["ended_at"] = datetime.now().isoformat(timespec="seconds")
            if self._round_t0 is not None:
                self._current_round["elapsed_ms"] = int((time.time() - self._round_t0) * 1000)
            self._current_round["status"] = status
            self._current_round = None
            self._current_step = None
        except Exception:
            pass

    # ---------- step ----------
    def begin_step(self, step_id, name=None):
        try:
            self._step_t0 = time.time()
            self._current_step = {
                "step_id": step_id,
                "name": name or step_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": None,
                "elapsed_ms": None,
                "status": "running",
                "failure_code": None,
                "signals": {},
                "recoveries": [],
            }
            if self._current_round is not None:
                self._current_round["steps"].append(self._current_step)
        except Exception:
            pass

    def end_step(self, status, failure_code=None):
        try:
            if self._current_step is None:
                return
            self._current_step["ended_at"] = datetime.now().isoformat(timespec="seconds")
            if self._step_t0 is not None:
                self._current_step["elapsed_ms"] = int((time.time() - self._step_t0) * 1000)
            self._current_step["status"] = status
            if failure_code:
                self._current_step["failure_code"] = failure_code
            self._current_step = None
        except Exception:
            pass

    # ---------- 观察数据（只读已有变量，不触发任何动作）----------
    def signal(self, key, value):
        try:
            if self._current_step is not None:
                self._current_step["signals"][key] = value
        except Exception:
            pass

    def recovery(self, action, count=0, result=None, at_ms=None):
        try:
            if self._current_step is not None:
                self._current_step["recoveries"].append({
                    "action": action,
                    "count": count,
                    "result": result,
                    "at_ms": at_ms,
                })
        except Exception:
            pass

    # ---------- 收尾与写盘 ----------
    def _finalize_running(self):
        """flush 时把仍处于 running 的 round/step 标记为 exception，避免 JSON 里留 running。"""
        try:
            if self._current_step is not None and self._current_step.get("status") == "running":
                self._current_step["status"] = "exception"
                if not self._current_step.get("ended_at"):
                    self._current_step["ended_at"] = datetime.now().isoformat(timespec="seconds")
                if self._step_t0 is not None:
                    self._current_step["elapsed_ms"] = int((time.time() - self._step_t0) * 1000)
        except Exception:
            pass
        try:
            if self._current_round is not None and self._current_round.get("status") == "running":
                self._current_round["status"] = "exception"
                if not self._current_round.get("ended_at"):
                    self._current_round["ended_at"] = datetime.now().isoformat(timespec="seconds")
                if self._round_t0 is not None:
                    self._current_round["elapsed_ms"] = int((time.time() - self._round_t0) * 1000)
        except Exception:
            pass

    def flush(self, final_result=None, exit_code=None, config_sha256=None):
        try:
            self._finalize_running()
            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "run_id": self.run_id,
                "started_at": self.started_at,
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_ms": int((time.time() - self._t0) * 1000),
                "final_result": final_result,
                "exit_code": exit_code,
                "source_sha256": self.source_sha256,
                "config_sha256": config_sha256,
                "rounds": self.rounds,
            }
            path = os.path.join(self.run_dir, "step_trace.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return path
        except Exception:
            return None
