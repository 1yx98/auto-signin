# -*- coding: utf-8 -*-
"""签到历史台账（纯新增，零副作用）。

每次运行结束后追加一行到 data/signin_history.csv，让"这学期漏了几次签到"
这种问题不用翻日志就能回答。

设计原则（与主程序一致：安全优先于功能）：
- 任何异常都吞掉，绝不能影响签到主流程。
- 只追加、不改写历史行（文件损坏时另起一个带时间戳的新文件，不丢旧数据）。
- CSV 用 utf-8-sig 编码，Excel 双击直接能看，不会乱码。
"""
import csv
import os
import time
from datetime import datetime, timedelta

HISTORY_DIR = "data"
HISTORY_FILE = "signin_history.csv"

# 列顺序固定：新增列一律往后加，保证老文件的读取兼容（按表头名取值）。
FIELDS = [
    "date",        # 签到日期 YYYY-MM-DD
    "time",        # 运行结束时刻 HH:MM:SS
    "result",      # success / fail / not_time / crash
    "code",        # 退出码
    "weekday",     # 星期几（中文）
    "cost_sec",    # 耗时（秒）
    "start_ts",    # 开跑时间戳
    "end_ts",      # 结束时间戳
    "fail_step",   # 失败步骤 ID（失败时才有）
    "fail_code",   # 失败原因码（失败时才有）
    "run_dir",     # 运行目录名
    "note",        # 备注
]

_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]


def _path(base_dir=None):
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, HISTORY_DIR, HISTORY_FILE)


def _read_rows(path):
    """读回全部历史行（含表头判断）。文件不存在/损坏都返回空列表。"""
    if not os.path.isfile(path):
        return []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return [r for r in csv.DictReader(f) if r.get("date")]
        except Exception:
            continue
    return []


def append_record(result, code=0, cost_sec=None, start_ts=None, end_ts=None,
                  fail_step=None, fail_code=None, run_dir=None, note=None,
                  base_dir=None):
    """追加一条签到记录。返回写入的行 dict；任何失败返回 None（静默）。"""
    try:
        path = _path(base_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = datetime.now()
        end_ts = float(end_ts) if end_ts else time.time()
        start_ts = float(start_ts) if start_ts else None

        # 已有文件但表头与当前 FIELDS 不一致（老版本写的）→ 换新文件，绝不覆盖旧数据
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    header = next(csv.reader(f), [])
                if [h for h in header if h] != FIELDS:
                    alt = path.replace(".csv", "_%s.csv" % now.strftime("%Y%m%d%H%M%S"))
                    os.rename(path, alt)
            except Exception:
                pass

        row = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "result": result,
            "code": "" if code is None else code,
            "weekday": _WEEKDAY_CN[now.weekday()],
            "cost_sec": "" if cost_sec is None else "%.1f" % float(cost_sec),
            "start_ts": "" if not start_ts else "%.1f" % start_ts,
            "end_ts": "%.1f" % end_ts,
            "fail_step": fail_step or "",
            "fail_code": fail_code or "",
            "run_dir": run_dir or "",
            "note": note or "",
        }
        new_file = not os.path.isfile(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        return row
    except Exception:
        return None


def recent_records(days=7, base_dir=None):
    """取最近 N 天（含今天）的记录，按时间升序。"""
    try:
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return [r for r in _read_rows(_path(base_dir)) if (r.get("date") or "") >= cutoff]
    except Exception:
        return []


def stats(days=7, base_dir=None):
    """统计最近 N 天的成功/失败/不在时段次数与连续失败数。

    返回 dict：
      total / success / fail / not_time / other
      fail_streak  连续失败天数（从今天往前数，遇到成功或不在时段即停止计数）
      streak_desc  连续失败的文字描述，如 "近 7 天失败 3 次、连续 2 天"
      success_rate 成功率（按"有明确结果的天数"算，not_time 不计入分母）
    """
    out = {"total": 0, "success": 0, "fail": 0, "not_time": 0, "other": 0,
           "fail_streak": 0, "streak_desc": "", "success_rate": None, "days": days}
    try:
        recs = recent_records(days, base_dir)
        out["total"] = len(recs)
        # 同一天多次运行：以当天"最好"的结果为准（成功优先），避免重试把成功率拉低
        by_day = {}
        for r in recs:
            d = r.get("date") or ""
            res = (r.get("result") or "").strip()
            prev = by_day.get(d)
            rank = {"success": 3, "not_time": 2, "fail": 1, "crash": 0}
            if prev is None or rank.get(res, 0) > rank.get(prev, 0):
                by_day[d] = res
        for res in by_day.values():
            if res == "success":
                out["success"] += 1
            elif res == "fail":
                out["fail"] += 1
            elif res == "not_time":
                out["not_time"] += 1
            else:
                out["other"] += 1

        # 连续失败：从最近一天往前，遇到非 fail 即停
        for d in sorted(by_day.keys(), reverse=True):
            if by_day[d] == "fail":
                out["fail_streak"] += 1
            else:
                break

        decided = out["success"] + out["fail"] + out["other"]
        if decided:
            out["success_rate"] = out["success"] * 100.0 / decided

        bits = []
        bits.append("近 %d 天：成功 %d、失败 %d" % (days, out["success"], out["fail"]))
        if out["not_time"]:
            bits.append("不在时段 %d" % out["not_time"])
        if out["fail_streak"] >= 2:
            bits.append("⚠️ 连续失败 %d 天" % out["fail_streak"])
        if out["success_rate"] is not None:
            bits.append("成功率 %.0f%%" % out["success_rate"])
        out["streak_desc"] = "；".join(bits)
    except Exception:
        pass
    return out


def should_escalate(days=7, base_dir=None):
    """是否需要升级告警：连续失败 ≥2 天，或近 7 天失败 ≥3 次。"""
    try:
        s = stats(days, base_dir)
        return bool(s["fail_streak"] >= 2 or s["fail"] >= 3), s
    except Exception:
        return False, {}


if __name__ == "__main__":
    import pprint
    print("历史台账：", _path())
    pprint.pprint(stats(7))
