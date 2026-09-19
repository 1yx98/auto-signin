# -*- coding: utf-8 -*-
"""离线验证 ③（僵尸窗口过滤）与 ④（进程检测）的核心函数。

③ `_win_is_zombie(hwnd)`  —— 需要一个**真正的僵尸窗口**才验得了"True 那一侧"。
   做法：起一个带窗口的进程，`taskkill /F` 强杀它，然后**立刻**枚举 ——
   进程已死但窗口对象可能还没被系统回收，那就是僵尸。
   （若系统回收太快抓不到，会如实说明"没抓到"，不硬凑。）

④ `_tasklist_has(name)` —— 验三件事：
   · 真存在的进程 -> (True, True)
   · 真不存在的   -> (False, True)
   · 查成 / 没查成 必须能区分（这正是 ④ 要修的那个"返回值表达力不足"）
"""
import os, sys, time, subprocess, ctypes
from ctypes import wintypes

os.environ.setdefault("SIGNIN_LOG_DIR_OVERRIDE",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_probe_logs"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signin

u = ctypes.windll.user32


def top_windows():
    out = []
    def cb(h, l):
        n = ctypes.create_unicode_buffer(512); u.GetWindowTextW(h, n, 512)
        c = ctypes.create_unicode_buffer(256); u.GetClassNameW(h, c, 256)
        r = wintypes.RECT(); u.GetWindowRect(h, ctypes.byref(r))
        out.append((h, n.value, c.value, r.right - r.left, r.bottom - r.top, bool(u.IsWindowVisible(h))))
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return out


def main():
    print("=" * 74)
    print("  离线验证 ③（僵尸窗口过滤）/ ④（进程检测）")
    print("=" * 74)
    print()

    ok3 = ok4 = True

    # ---------------- ④ _tasklist_has ----------------
    print("【④ 进程检测】`_tasklist_has()`")
    print("-" * 74)
    found, okcall = signin._tasklist_has("Weixin.exe")
    print("  _tasklist_has('Weixin.exe') = (%s, %s)" % (found, okcall))
    print("     期望：okcall=True（查成了）；found 取决于微信是否在跑")
    if okcall is not True:
        ok4 = False
        print("     ❌ 查询没成功 —— 不该")
    found2, okcall2 = signin._tasklist_has("绝对不存在的进程名_XYZ_123.exe")
    print("  _tasklist_has('绝对不存在的进程名_XYZ_123.exe') = (%s, %s)" % (found2, okcall2))
    print("     期望：(False, True) —— **查成了、且确实没有**")
    if not (found2 is False and okcall2 is True):
        ok4 = False
        print("     ❌ 不符合期望")
    print()
    print("  ⇒ ④ 核心函数 %s" % ("✅ 通过（能区分『查成没有』与『没查成』）" if ok4 else "❌ 未通过"))
    print()

    # ---------------- ③ _win_is_zombie ----------------
    print("【③ 僵尸窗口过滤】`_win_is_zombie()`")
    print("-" * 74)
    wins = top_windows()
    live = [w for w in wins if w[5] and w[1].strip()]
    fp = [w for w in live if signin._win_is_zombie(w[0])]
    print("  当前可见带标题窗口 %d 个，其中被判为僵尸的：%d 个" % (len(live), len(fp)))
    print("     期望：0 个（**不能误杀活窗口** —— 这是 ③ 的安全底线）")
    if fp:
        ok3 = False
        for w in fp[:5]:
            print("     ❌ 误判: hwnd=%s %r" % (w[0], w[1][:40]))
    else:
        print("     ✅ 无误判")
    print()

    print("  --- 尝试造一个真僵尸（起进程 -> 强杀 -> 立刻枚举）---")
    got_zombie = False
    try:
        p = subprocess.Popen(["notepad.exe"])
        time.sleep(1.5)
        before = [w for w in top_windows() if w[1].strip() == "无标题 - 记事本" or "记事本" in w[1]]
        if not before:
            before = [w for w in top_windows() if w[1].strip() == "Untitled - Notepad" or "Notepad" in w[1]]
        print("     记事本窗口：%d 个" % len(before))
        subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                       capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=10)
        # 强杀后立刻多次探测
        for _ in range(12):
            for w in before:
                if signin._win_is_zombie(w[0]):
                    print("     ✅ 抓到僵尸: hwnd=%s %r  -> _win_is_zombie=True" % (w[0], w[1][:30]))
                    got_zombie = True
                    break
            if got_zombie:
                break
            time.sleep(0.25)
        if not got_zombie:
            print("     ⚠ 没抓到（系统回收太快，或记事本窗口已随进程消失）")
    except Exception as e:
        print("     造僵尸失败: %s: %s" % (type(e).__name__, e))
    print()
    if got_zombie:
        print("  ⇒ ③ 核心函数 ✅ 通过（真僵尸判 True、活窗口判 False，两侧都对）")
    else:
        print("  ⇒ ③ 核心函数 ✅ **安全侧已验证**（活窗口 0 误判）")
        print("     ⚠ '真僵尸 -> True' 这一侧仍未验到 —— 需等真实场景（微信退出后残留）")
    print()

    print("=" * 74)
    print("  ④ %s ；③ 安全侧 %s" % ("通过" if ok4 else "未通过", "通过" if ok3 else "未通过"))
    print("=" * 74)
    return 0 if (ok3 and ok4) else 1


if __name__ == "__main__":
    sys.exit(main())
