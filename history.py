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
import sys
import time
from datetime import datetime, timedelta

HISTORY_DIR = "data"
HISTORY_FILE = "signin_history.csv"

# 落盘失败时的补偿队列：写入被占用（Excel/WPS 开着 CSV）时，先把行存到这里，
# 下次运行开头自动补齐。台账不能因为"你恰好用 Excel 打开了它"就长期静默丢失。
PENDING_FILE = "signin_history_pending.csv"

# append_record 最近一次的失败原因（供主程序日志展示；None 表示上次成功）
LAST_ERROR = None
LAST_ERROR_KIND = None

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


def _path(base_dir=None, name=None):
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, HISTORY_DIR, name or HISTORY_FILE)


def _append_row(path, row, need_header):
    """真正的落盘动作（会抛异常，由调用方决定怎么处理）。

    注意不能用 newline="" 之外的花样——保持与历史文件一致的 utf-8-sig，
    这样 Excel 双击台账不会乱码。
    """
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if need_header:
            w.writeheader()
        w.writerow(row)


def _is_file_busy(exc):
    """判断异常是不是"文件被别的程序占着"（Excel/WPS 打开 CSV 的典型情形）。

    Windows 上是 WinError 32（另一个程序正在使用此文件），也兼容 33。
    """
    if isinstance(exc, PermissionError):
        winerr = getattr(exc, "winerror", None)
        if winerr in (32, 33):
            return True
        # 非 WinError 的 PermissionError（如权限/只读）同样按"写不进去"处理
        return True
    return False


def _queue_pending(row, base_dir=None):
    """写不进主台账时，把这一行暂存到待补队列（同样失败就算了，绝不抛给调用方）。"""
    try:
        pend = _path(base_dir, PENDING_FILE)
        need_header = not os.path.isfile(pend)
        _append_row(pend, row, need_header)
        return True
    except Exception:
        return False


def flush_pending(base_dir=None):
    """把待补队列里的记录补写进主台账。返回 (补写成功数, 仍积压数)。

    每次运行开头调用一次：既能把上次因为文件被占用而暂存的行补上，
    又能顺手清空队列（补写成功的行从队列移除，补不上的继续留着）。
    """
    try:
        pend = _path(base_dir, PENDING_FILE)
        if not os.path.isfile(pend):
            return (0, 0)
        rows = []
        try:
            with open(pend, "r", encoding="utf-8-sig", newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("date")]
        except Exception:
            return (0, 0)
        if not rows:
            try:
                os.remove(pend)
            except Exception:
                pass
            return (0, 0)

        main = _path(base_dir)
        need_header = not os.path.isfile(main)
        done = 0
        for r in rows:
            try:
                _append_row(main, {k: r.get(k, "") for k in FIELDS}, need_header)
                need_header = False
                done += 1
            except Exception:
                break     # 一失败就停，剩下的继续留在队列里
        left = rows[done:]
        if not left:
            try:
                os.remove(pend)
            except Exception:
                pass
        else:
            try:
                tmp = pend + ".tmp"
                with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDS)
                    w.writeheader()
                    w.writerows(left)
                os.replace(tmp, pend)
            except Exception:
                pass
        return (done, len(left))
    except Exception:
        return (0, 0)


def append_record(result, code=0, cost_sec=None, start_ts=None, end_ts=None,
                  fail_step=None, fail_code=None, run_dir=None, note=None,
                  base_dir=None):
    """追加一条签到记录。返回写入的行 dict；彻底失败时返回 None。

    【2026-09-15 修复·静默失败】原实现"任何异常 return None"，
    而真实现场是 **WPS/Excel 打开着 data\\signin_history.csv** → 文件被独占锁定
    → WinError 32 → 每次运行都静默丢一行 → 台账长期为空
    → should_escalate() 永远 False → "连续失败告警"从未生效过。
    现在改成：主台账写不进就落进待补队列，下次运行开头补写；
    同时把失败原因写进 LAST_ERROR/写 stderr，绝不静默。
    """
    global LAST_ERROR, LAST_ERROR_KIND
    row = None
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
            except PermissionError:
                pass      # 被占用时不动它，后面照样尝试追加
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
        need_header = not os.path.isfile(path)
        _append_row(path, row, need_header)
        LAST_ERROR, LAST_ERROR_KIND = None, None
        return row
    except Exception as e:
        LAST_ERROR = "%s: %s" % (type(e).__name__, e)
        if row is not None and _is_file_busy(e):
            # 文件被 Excel/WPS 占着 → 暂存待补队列，下次运行开头补上
            if _queue_pending(row, base_dir):
                LAST_ERROR_KIND = "busy_queued"
                _warn("[台账] %s 被其他程序占用，本次记录已暂存待补队列"
                      "（下次运行会自动补写）" % os.path.basename(_path(base_dir)))
                return row
        LAST_ERROR_KIND = "failed"
        _warn("[台账] 记录写入失败：%s" % LAST_ERROR)
        return None


def _warn(msg):
    """台账出问题要被看见——主程序会把 stderr 收进 run.log。"""
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


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
      fail_streak  连续失败天数（从今天往前数，遇到非 fail 即停）
      streak_desc  连续失败的文字描述，如 "近 7 天：成功 5、失败 2；成功率 71%"
      success_rate 成功率（按"有明确结果的天数"算，not_time 不计入分母）

    同一天多次运行的合并取向（重要）：
      success > fail > crash > not_time
      ——出现过成功 → 当天算成功（重试成功不该记成失败）
      ——否则出现过失败 → 当天算失败（哪怕最后变成 not_time，问题也已发生）
      ——再否则才算 not_time
      这个顺序保证"那天其实反复失败过"不会被后来的 not_time 掩盖掉。
    """
    out = {"total": 0, "success": 0, "fail": 0, "not_time": 0, "other": 0,
           "fail_streak": 0, "streak_desc": "", "success_rate": None, "days": days}
    try:
        recs = recent_records(days, base_dir)
        out["total"] = len(recs)
        # 同一天多次运行：以当天"最好"的结果为准，避免重试把成功率拉低。
        # 但 rank 的排序有个关键取向——**遇到问题要暴露，不能掩盖**：
        #   success(3) > not_time(2) > fail(1) > crash(0) 是错的，会让
        #   "先失败几次、最后不在时段"这种一天被判成"不在时段"，把失败藏起来。
        # 正确取向：只要当天**出现过成功**就算成功（重试成功不该记成失败）；
        #   否则只要有**失败**就算失败（哪怕后来变成 not_time，问题也已发生）；
        #   再否则才算 not_time。
        _rank = {"success": 3, "fail": 2, "crash": 1, "not_time": 0}
        by_day = {}
        for r in recs:
            d = r.get("date") or ""
            res = (r.get("result") or "").strip()
            prev = by_day.get(d)
            if prev is None or _rank.get(res, 0) > _rank.get(prev, 0):
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
        # "成功 0、失败 0" 在只有 not_time 记录时是废话，还会让人以为出问题。
        # 所以只在真的有过成功或失败时才带这两个数字；否则只报"不在时段"。
        if out["success"] or out["fail"] or out["other"]:
            bits.append("近 %d 天：成功 %d、失败 %d" % (days, out["success"], out["fail"]))
        elif out["not_time"]:
            bits.append("近 %d 天：均不在签到时段" % days)
        if out["not_time"] and (out["success"] or out["fail"] or out["other"]):
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
