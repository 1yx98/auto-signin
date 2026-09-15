# -*- coding: utf-8 -*-
"""签到台账报表——把 data/signin_history.csv 变成一眼能看懂的出勤概览。

回答三个问题：
    1. 这学期一共签了多少天、漏了多少天、成功率多少？
    2. **具体哪几天漏了**（最实用——直接拿去补签或申诉）
    3. 最近状态如何（连续成功/连续失败）

用法：
    runtime\\python.exe report.py           # 最近 30 天
    runtime\\python.exe report.py 7         # 最近 7 天
    runtime\\python.exe report.py 999       # 全部历史

纯只读：只读台账、只打印，不改任何文件、不联网。
"""
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import history as H
except Exception as e:
    print("[ERROR] 无法导入 history.py：%s" % e)
    sys.exit(2)

_CN = {"success": "成功", "fail": "失败", "crash": "崩溃", "not_time": "不在时段"}


def _bar(n, total, width=24):
    """一个朴素的文本进度条。"""
    if not total:
        return ""
    filled = int(round(width * n / float(total)))
    return "#" * filled + "." * (width - filled)


def main():
    days = 30
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print("用法：python report.py [天数]")
            return 2

    recs = H.recent_records(days)
    if not recs:
        print("台账里最近 %d 天没有记录。" % days)
        print("（台账文件：%s）" % H._path())
        print("提示：每次运行签到后会自动追加记录；历史日志可用 import_history.py 回溯导入。")
        return 0

    # ---- 按天合并（与 history.stats 同一取向：出现成功算成功，否则失败优先）----
    rank = {"success": 3, "fail": 2, "crash": 1, "not_time": 0}
    by_day = {}
    for r in recs:
        d = r.get("date") or ""
        res = (r.get("result") or "").strip()
        if d not in by_day or rank.get(res, 0) > rank.get(by_day[d], 0):
            by_day[d] = res

    s = H.stats(days)
    all_days = sorted(by_day.keys())

    print("=" * 62)
    print("签到出勤概览 · 最近 %d 天" % days)
    print("台账：%s" % H._path())
    print("=" * 62)
    print()
    print("时间范围 : %s ~ %s（共 %d 天有记录）" % (all_days[0], all_days[-1], len(all_days)))
    print("原始记录 : %d 条（含同一天多次运行/重试）" % len(recs))
    print()
    print("按天统计")
    print("  成功      %2d 天   %s" % (s["success"], _bar(s["success"], len(all_days))))
    print("  失败      %2d 天   %s" % (s["fail"], _bar(s["fail"], len(all_days))))
    if s["not_time"]:
        print("  不在时段  %2d 天   %s" % (s["not_time"], _bar(s["not_time"], len(all_days))))
    if s["other"]:
        print("  其他      %2d 天" % s["other"])
    print("  ------------------------------")
    if s["success_rate"] is not None:
        print("  成功率    %.0f%%（按有明确结果的天数算，不在时段不计入）" % s["success_rate"])
    print()

    # ---- 连续状态 ----
    ordered = sorted(by_day.keys(), reverse=True)
    streak_res, streak_n = None, 0
    for d in ordered:
        if streak_res is None:
            streak_res = by_day[d]
            streak_n = 1
        elif by_day[d] == streak_res:
            streak_n += 1
        else:
            break
    if streak_n:
        label = _CN.get(streak_res, streak_res)
        if streak_res == "success":
            print("当前状态 : 连续成功 %d 天（最近一次 %s）" % (streak_n, ordered[0]))
        elif streak_res in ("fail", "crash"):
            print("当前状态 : [!] 连续失败 %d 天（最近一次 %s）——建议检查" % (streak_n, ordered[0]))
        else:
            print("当前状态 : 最近 %d 天%s" % (streak_n, label))
    print()

    # ---- 哪些天失败了（最实用的部分）----
    fail_days = [d for d in all_days if by_day[d] in ("fail", "crash")]
    if fail_days:
        print("-" * 62)
        print("漏签 / 失败的日子（共 %d 天）" % len(fail_days))
        print("-" * 62)
        for d in fail_days:
            # 找当天的失败详情
            same = [r for r in recs if r.get("date") == d]
            wk = same[0].get("weekday", "") if same else ""
            step = ""
            for r in same:
                if r.get("fail_step") or r.get("fail_code"):
                    step = "（卡在 %s %s）" % (r.get("fail_step") or "?", r.get("fail_code") or "")
                    break
            print("  %s 周%s  %s%s" % (d, wk, _CN.get(by_day[d], by_day[d]), step))
        print()
    else:
        print("没有失败记录 —— 全部成功。")
        print()

    # ---- 耗时 ----
    costs = []
    for r in recs:
        try:
            if r.get("cost_sec"):
                costs.append(float(r["cost_sec"]))
        except Exception:
            pass
    if costs:
        print("耗时     : 平均 %.0f 秒，最快 %.0f 秒，最慢 %.0f 秒" % (
            sum(costs) / len(costs), min(costs), max(costs)))
        print()

    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
