# -*- coding: utf-8 -*-
"""用**真实失败截图**离线验证 ⑥ / ⑦ 两个补丁的核心函数。

【为什么这样做】
补丁"没被触发过"不等于"没验证过"。③④⑤⑥⑦ 等的是偶发场景，可能要等很久。
但只要手里有**真实样本**，就可以离线把函数本身验掉 ——
这比"等它自然出现"靠谱得多，也符合本项目铁律 R11（真实样本才能验"到底读不读得出"）。

本脚本用到的真实样本（全部来自真实运行，不是合成）：
  · logs/run_20260917_205504/205657_详情页按钮扫描.jpg        —— 按钮文字「不在区域内」
  · logs/run_20260918_205502/205822_签到成功_已签到.jpg        —— 按钮文字「已签到」
  · logs/run_20260917_215517/215706_详情页_时段外已签_疑历史记录.jpg —— 今天(09-17)的签到记录
"""
import os, sys, cv2, numpy as np

os.environ.setdefault("SIGNIN_LOG_DIR_OVERRIDE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_probe_logs"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signin

ROOT = os.path.dirname(os.path.abspath(__file__))


def load(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def btn(cx, cy, w, h, kind="gray"):
    return dict(kind=kind, cx=cx, cy=cy, x=cx - w // 2, y=cy - h // 2, w=w, h=h, fill=1.0)


def main():
    print("=" * 74)
    print("  用真实样本离线验证补丁 ⑥ / ⑦ 的核心函数")
    print("=" * 74)
    print()

    # ---------------- ⑥ _is_not_in_area ----------------
    print("【⑥ 不在区域内重跑】`_is_not_in_area()`")
    print("-" * 74)

    cases = [
        ("logs/run_20260917_205504/205657_详情页按钮扫描.jpg",
         btn(1442, 1379, 790, 93), True,
         "真实失败现场：按钮文字=「不在区域内」"),
        ("logs/run_20260918_205502/205822_签到成功_已签到.jpg",
         btn(1442, 1206, 764, 90), False,
         "真实成功现场：按钮文字=「已签到」（必须判 False，否则会误转自愈）"),
    ]
    ok6 = True
    for rel, b, expect, desc in cases:
        img = load(rel)
        if img is None:
            print("  [SKIP] 样本不存在: %s" % rel)
            ok6 = False
            continue
        got = signin._is_not_in_area(None, [b], full=img)
        flag = "✅" if got == expect else "❌"
        print("  %s %s" % (flag, desc))
        print("       样本=%s 尺寸=%dx%d" % (os.path.basename(rel), img.shape[1], img.shape[0]))
        print("       期望=%s  实际=%s" % (expect, got))
        if got != expect:
            ok6 = False
    print()
    print("  ⇒ ⑥ 核心函数 %s" % ("✅ 通过（真/假两侧都对）" if ok6 else "❌ 未通过"))
    print()

    # ---------------- ⑦ _detail_record_is_today ----------------
    print("【⑦ 记录日期判定】`_detail_record_is_today()`")
    print("-" * 74)
    rel = "logs/run_20260917_215517/215706_详情页_时段外已签_疑历史记录.jpg"
    img = load(rel)
    if img is None:
        print("  [SKIP] 样本不存在")
        ok7 = False
    else:
        # 该样本是 09-17 的记录；今天是 09-18 -> 期望 False（正确识别"不是今天"）
        # 把 win_rect 指到该截图里小程序窗口的位置，以走真实代码路径
        _orig = signin.win_rect
        signin.win_rect = lambda h: (1012, 64, 1872, 1642)
        try:
            got = signin._detail_record_is_today(1, full=img)
        finally:
            signin.win_rect = _orig
        print("  样本=%s 尺寸=%dx%d" % (os.path.basename(rel), img.shape[1], img.shape[0]))
        print("  该样本的页面日期 = 2026-09-17（真实签到时间 21:25）")
        print("  期望：False（今天已是 09-18，不是同一天）")
        print("  实际：%s  %s" % (got, "✅" if got is False else "❌"))
        ok7 = (got is False)

        # 再看它到底读出了什么日期（证明 OCR 真的读到了，而不是"读不到就返回 False"）
        eng = signin._ocr_get_engine()
        txt = (signin._ocr_read(eng, img[64:1642, 1012:1872]) or "").replace(" ", "")
        import re
        seg = txt.split("签到时间", 1)[1] if "签到时间" in txt else ""
        digits = "".join(ch for ch in seg if ch.isdigit())
        print()
        print("  ★ 关键：确认它是「读到了日期但不是今天」，而不是「读不到才返回 False」")
        print("     OCR 原文片段：%r" % txt[-60:])
        print("     签到时间后的数字：%s" % digits[:12])
        print("     取前 8 位 = %s  %s" % (digits[:8], "✅ 读到了 20260917" if digits[:8] == "20260917" else "❌"))
        if digits[:8] != "20260917":
            ok7 = False
    print()
    print("  ⇒ ⑦ 核心函数 %s" % ("✅ 通过（能正确读出日期并判定）" if ok7 else "❌ 未通过"))
    print()

    print("=" * 74)
    print("  结论：%s" % ("⑥⑦ 核心逻辑已用真实样本验证通过" if (ok6 and ok7) else "有未通过项，见上"))
    print("=" * 74)
    return 0 if (ok6 and ok7) else 1


if __name__ == "__main__":
    sys.exit(main())
