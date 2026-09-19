# -*- coding: utf-8 -*-
r"""手动跑结果速查 —— 跑完这个脚本，一眼看出这次跑得怎么样。

用法：runtime\python.exe _watch_manual_run.py
（纯只读，不改任何东西）
"""
import os, glob, time, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")

runs = sorted(glob.glob(os.path.join(LOGS, "run_*")), key=os.path.getmtime, reverse=True)
if not runs:
    print("没有找到任何 run 目录"); raise SystemExit(1)

newest = runs[0]
print(f"最近一次运行目录：{os.path.basename(newest)}")
print(f"  创建时间：{datetime.datetime.fromtimestamp(os.path.getmtime(newest)):%Y-%m-%d %H:%M:%S}")
print()

# 读 run.log 尾部
rl = os.path.join(newest, "run.log")
if os.path.exists(rl):
    txt = open(rl, "rb").read().decode("utf-8", "replace")
    lines = txt.splitlines()
    print(f"run.log 共 {len(lines)} 行，尾部 30 行：")
    print("-" * 60)
    for l in lines[-30:]:
        print("  " + l)
    print("-" * 60)
    print()
    # 关键判据
    print("=== 关键判据 ===")
    keys = {
        "最终结果": [l for l in lines if "最终结果=" in l],
        "退出码":   [l for l in lines if "退出码=" in l],
        "总耗时":   [l for l in lines if "总耗时=" in l],
        "P2预检命中": [l for l in lines if "滚动前预检" in l],
        "页面未变化": [l for l in lines if "页面未变化" in l],
        "OCR统计":  [l for l in lines if "OCR统计" in l],
        "异常/错误": [l for l in lines if ("ERROR" in l or "Traceback" in l)],
        "预检失败": [l for l in lines if "预检失败" in l],
    }
    for k, v in keys.items():
        print(f"  【{k}】共 {len(v)} 条")
        for l in v[:4]:
            print(f"      {l.strip()}")
    print()

# 失败现场
fails = glob.glob(os.path.join(newest, "FAIL_*.png"))
print(f"失败现场截图：{len(fails)} 张" + ("  ← 有 FAIL 说明出问题了" if fails else "  （没有，好）"))
print()

# 最新按天日志尾部（看台账）
day = os.path.join(LOGS, f"signin_{datetime.date.today():%Y%m%d}.log")
if os.path.exists(day):
    lines = open(day, "rb").read().decode("utf-8", "replace").splitlines()
    print(f"按天日志 {os.path.basename(day)}，尾部 6 行：")
    for l in lines[-6:]:
        print("  " + l)
