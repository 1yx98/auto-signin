# -*- coding: utf-8 -*-
"""签到界面样本采集工具（为将来的 OCR 评估/模板排查积累素材）。

**它做什么**：把当前屏幕整个存进 logs/samples/，并让你用键盘给这张图打一个标签。
**它不做什么**：不点击、不按键注入微信、不改动任何签到逻辑、不动 templates/。
    —— 全程只读屏幕，对签到主流程零影响。

为什么需要它：
    想评估"用 OCR 区分'已签到'和'已结束'"这件事值不值得做，前提是**手里得有这两种
    状态的真实截图**。现在 logs/ 里只有 1 次成功运行的样本，且一张「已结束」都没有，
    任何 OCR 方案都没法验证。这个工具就是为了把样本攒起来。

怎么用（签到后手动跑一次，10 秒）：
    1. 在微信里把油学通翻到你想记录的那个界面（如"已结束"的列表、已签到详情页）；
    2. 双击 采集样本.bat；
    3. 给这张图选一个标签（按数字键），完成。

标签会写进 logs/samples/labels.csv，文件名自带标签与时间，便于日后直接筛选统计。
"""
import os
import sys
import csv
import time
import ctypes
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(SCRIPT_DIR, "logs", "samples")
LABEL_CSV = os.path.join(SAMPLE_DIR, "labels.csv")


def _enable_dpi_awareness():
    """与主程序一致：按物理像素工作，否则高缩放屏上截出来的图和实际看到的不一样。"""
    cands = [
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ]
    for fn in cands:
        try:
            fn()
            return
        except Exception:
            continue


# 标签表：数字键 → (标签, 说明)。标签是英文/ASCII，避免 CSV 与文件名在不同编码下出问题。
LABELS = [
    ("signed_detail",   "详情页 - 灰色'已签到'（确认成功的硬依据）"),
    ("signed_list",     "列表卡片 - 绿色'已签到'"),
    ("ended_detail",    "详情页 - 灰色'已结束'（最想要！OCR 要靠它区分）"),
    ("ended_list",      "列表卡片 - '已结束'"),
    ("not_signed",      "详情页 - 蓝色'签到' + 绿色'请假'（未签状态）"),
    ("map_page",        "地图定位页（底部'完成签到'）"),
    ("other",           "其他 / 说不清"),
]


def _grab():
    """截当前屏幕。优先用项目自带的 PIL（与主程序同一套依赖）。"""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception as e:
        print("[错误] 截图失败：%s" % e)
        return None


def _save(img, tag):
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = "%s_%s.png" % (ts, tag)
    path = os.path.join(SAMPLE_DIR, name)
    img.save(path)

    # 记录到 labels.csv（追加；首次写表头）
    new = not os.path.isfile(LABEL_CSV)
    try:
        with open(LABEL_CSV, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "label", "file", "width", "height"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tag, name,
                        img.size[0], img.size[1]])
    except Exception as e:
        print("[警告] 标签写入失败（图片已保存）：%s" % e)
    return path


def main():
    _enable_dpi_awareness()
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    print("=" * 62)
    print("  签到界面样本采集（只截屏，不操作微信）")
    print("=" * 62)
    print()
    print("  采集目录：%s" % SAMPLE_DIR)
    print()

    # 现有样本统计，让用户知道还缺什么
    try:
        have = {}
        if os.path.isfile(LABEL_CSV):
            with open(LABEL_CSV, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    have[r.get("label", "")] = have.get(r.get("label", ""), 0) + 1
    except Exception:
        have = {}
    print("  已有样本：" + ("、".join("%s×%d" % (k, v) for k, v in sorted(have.items()))
                          if have else "（还没有）"))
    missing = [t for t, _ in LABELS if t not in have and t != "other"]
    if missing:
        print("  还缺：" + "、".join(missing))
    print()

    while True:
        print("-" * 62)
        print("  请先切到微信、翻到要记录的那个界面，然后回到本窗口按回车截屏")
        print("  （直接输入 q 回车退出）")
        print("-" * 62)
        try:
            cmd = input("  回车=截屏 / q=退出 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd in ("q", "quit", "exit"):
            break

        img = _grab()
        if img is None:
            continue

        # 缩略提示：高分辨率屏幕整图很大，这里只报尺寸，不弹窗
        print()
        print("  已截屏 %dx%d，请选择这张图是哪个界面：" % img.size)
        for i, (tag, desc) in enumerate(LABELS, 1):
            print("    [%d] %-16s %s" % (i, tag, desc))
        print("    [s] 跳过（不保存这张）")
        print()
        try:
            pick = input("  输入数字 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if pick in ("s", "skip", ""):
            print("  已跳过，未保存。")
            continue
        if not pick.isdigit() or not (1 <= int(pick) <= len(LABELS)):
            print("  输入无效，已跳过（未保存）。")
            continue

        tag, desc = LABELS[int(pick) - 1]
        path = _save(img, tag)
        print("  ✓ 已保存：%s" % os.path.basename(path))
        print("    （%s）" % desc)
        print()

    print()
    print("=" * 62)
    print("  采集结束。样本在 %s" % SAMPLE_DIR)
    print("  标签清单：labels.csv")
    print("=" * 62)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
