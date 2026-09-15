# -*- coding: utf-8 -*-
"""从按天日志 logs/signin_YYYYMMDD.log 回溯导入历史记录到台账。

用途：台账（data/signin_history.csv）是 2026-09-15 才引入的，之前的成功/失败记录
只存在于按天日志里，而按天日志会随 keep_days 被清理。这个脚本把历史一次性固化进台账，
避免"以前的成功案例随日志消失"。

特性：
- **只读**按天日志，**只追加**台账；不修改、不删除任何日志。
- **幂等**：默认跳过台账里已存在的 (日期, 时刻) 组合，重复运行不会产生重复行。
- 从 run.log 抓取比按天日志更完整的信息（耗时、失败步骤等）。
- 任何异常只跳过该条，不影响其余记录。
"""
import os
import re
import sys
import csv
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import history as H

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

# 按天日志里的关键行
#   运行总结里的      最终结果=success  退出码=0
#   以及              开始=20:52:31 结束=20:54:29 总耗时=118.4 秒
_RE_RESULT = re.compile(r"最终结果=(\w+)\s+退出码=(\d+)")
_RE_COST = re.compile(r"总耗时=([\d.]+)\s*秒")


def _existing_keys(base_dir=None):
    """台账里已有的 (date, time) 集合，用于幂等跳过。"""
    keys = set()
    try:
        for r in H._read_rows(H._path(base_dir)):
            keys.add((r.get("date", ""), r.get("time", "")))
    except Exception:
        pass
    return keys


def parse_day_log(path):
    """从一个按天日志里解析出所有运行的 (结果, 耗时秒) 列表。

    按天日志是多次运行追加在一起的，每次运行以"运行总结"结尾。
    这里按"最终结果="的出现顺序切段，每段各自取耗时。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []

    out = []
    # 以每条"最终结果=..."为界，向前找同段内的耗时
    matches = list(_RE_RESULT.finditer(text))
    for i, m in enumerate(matches):
        seg_start = matches[i - 1].end() if i > 0 else 0
        seg = text[seg_start:m.end()]
        result = m.group(1)
        code = int(m.group(2))
        cost = None
        cm = _RE_COST.search(seg)
        if cm:
            try:
                cost = float(cm.group(1))
            except Exception:
                cost = None
        out.append((result, code, cost))
    return out


def main():
    dry = "--dry-run" in sys.argv
    base = None  # 用默认（项目根）

    files = sorted(f for f in os.listdir(LOG_DIR)
                   if f.startswith("signin_") and f.endswith(".log"))
    if not files:
        print("没找到任何 signin_*.log，无需导入。")
        return

    existing = _existing_keys(base)
    print("台账现有记录数：%d" % len(existing))
    print("将扫描 %d 个按天日志：" % len(files))
    print()

    plan = []   # (date, result, code, cost)
    for fn in files:
        date = fn[len("signin_"):-len(".log")]
        if len(date) != 8 or not date.isdigit():
            continue
        d = "%s-%s-%s" % (date[:4], date[4:6], date[6:])
        runs = parse_day_log(os.path.join(LOG_DIR, fn))
        for (result, code, cost) in runs:
            plan.append((d, result, code, cost))

    # 统计预览
    from collections import Counter
    c = Counter(r[1] for r in plan)
    print("共解析出 %d 条运行记录：成功 %d、失败 %d、不在时段 %d"
          % (len(plan), c.get("success", 0), c.get("fail", 0), c.get("not_time", 0)))
    print()

    # 时间戳缺失，用当天 20:55 作为占位（只为统计日期用；新记录才有精确时刻）
    written = 0
    for (d, result, code, cost) in plan:
        hhmmss = "20:55:00"     # 历史记录没有精确时刻，统一用签到任务时间占位
        if (d, hhmmss) in existing:
            continue             # 幂等：已有则跳过
        if dry:
            print("  [dry] %s %s code=%s cost=%s" % (d, result, code, cost))
            written += 1
            continue
        # 用 history 的底层接口写（append_record 会用"现在"的日期，不适合回溯）
        try:
            row = _write_historical(d, hhmmss, result, code, cost, base)
            if row:
                written += 1
        except Exception as e:
            print("  [!] %s 写入失败: %s" % (d, e))

    print()
    if dry:
        print("DRY-RUN：将写入 %d 条（未实际写入）" % written)
    else:
        print("已写入 %d 条历史记录。台账：%s" % (written, H._path(base)))
        print()
        s = H.stats(7, base)
        print("导入后近 7 天统计：%s" % s.get("streak_desc"))


def _write_historical(date_str, hhmmss, result, code, cost, base=None):
    """写一条历史记录（日期由调用方指定，不是"现在"）。"""
    path = H._path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None
    row = {
        "date": date_str,
        "time": hhmmss,
        "result": result,
        "code": code,
        "weekday": _WEEKDAY_CN[dt.weekday()],
        "cost_sec": "" if cost is None else "%.1f" % cost,
        "start_ts": "",
        "end_ts": "",
        "fail_step": "",
        "fail_code": "",
        "run_dir": "（历史导入）",
        "note": "由 import_history.py 从按天日志回溯",
    }
    new_file = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=H.FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return row


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
