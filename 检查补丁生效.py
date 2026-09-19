# -*- coding: utf-8 -*-
"""补丁生效核查 —— 扫描 run.log，报告每个补丁"有没有真的被触发过"。

【为什么要有这个工具】
补丁"装上了"不等于"生效了"。装了只是代码在文件里；
**只有那条新代码路径真的被执行到、并在日志里留下痕迹，才算验证过。**

真实教训（2026-09-17）：装完 3 个补丁后手跑一次，脚本成功进入微信 ——
但事后一查，`跳过僵尸窗口` 触发 **0 次**（那次微信是完全关闭的，
根本没有僵尸窗口可过滤）。**那次成功不能归功于补丁。**
光看"跑成功了"会得出错误结论，必须逐条查证据。

【为什么必须分"补丁前/补丁后"】
"旧病"那些串（如『激活后微信仍不可见』）在**补丁安装之前**的旧 run 里
本来就大量存在。不分开统计，会让人误以为补丁没生效。
本工具按各包 `_state.json` 的 `applied_at` 自动划一条分界线。

用法：
    runtime\\python.exe 检查补丁生效.py            # 默认最近 15 次运行
    runtime\\python.exe 检查补丁生效.py 30         # 最近 30 次
"""
import os, sys, glob, json

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")

PACKAGES = [
    "补丁包", "跳过无效滚动补丁包", "僵尸窗口过滤补丁包", "进程检测补丁包",
    "进入微信判定补丁包", "不在区域内重跑补丁包", "记录日期判定补丁包",
]

# hit  ：只有该补丁的**新代码路径被执行到**才会出现的日志片段
EVIDENCE = [
    ("① 滚动等页面变化 (P1a/P1b)", ["补丁P1b", "页面已滚动"],
     "『页面未变化』条数应明显少于改动前（改动前是 6 轮全白滚）"),
    ("② 跳过无效滚动 (P2a/P2b)", ["滚动前预检"],
     "已签到态下应看到『跳过 6 轮无效滚动』；正常签到态不出现属正常"),
    ("③ 僵尸窗口过滤 (Z1/Z2)", ["跳过僵尸窗口"],
     "★ 只有『微信退出后残留窗口』时才触发。没触发 != 失效，只是没遇到"),
    ("④ 进程检测 (P1/P2)", ["tasklist 查不到微信", "无法确定微信是否在运行"],
     "只在 tasklist 查询失败时触发；正常情况不出现属正常"),
    ("⑤ 进入微信判定 (E1)", ["不能据此断定"],
     "只在『进入微信』按钮匹配失败(conf<0)时触发"),
    ("⑥ 不在区域内重跑 (N1~N4)", ["不在区域内", "走定位自愈"],
     "★ 定位漂到校外时才触发。出现即说明修好了『误报 not_time』"),
    ("⑦ 记录日期判定 (D1+D2)", ["页面「签到时间」读到"],
     "★ 时段外看到灰色已签到时触发。读到『== 今天』即说明修好了误报"),
]

# 旧病：修好后**在补丁之后的运行里**应该消失/明显减少
AVOID = [
    ("激活后微信仍不可见（僵尸窗口被选中）", "激活后微信仍不可见"),
    ("重启微信以恢复窗口状态（任务栏弹窗）", "重启微信以恢复窗口状态"),
    ("点击后复核按钮始终存在（点击无效）", "按钮始终存在"),
    ("只有灰色宽按钮->未到时段/已结束（可能是定位漂移被误判）",
     "未到签到时段/已结束，不点击不关机"),
]


def cutoff():
    """返回所有包 `applied_at` 里最晚的那个，格式 YYYYMMDD_HHMMSS。"""
    best = ""
    for pkg in PACKAGES:
        p = os.path.join(ROOT, pkg, "_state.json")
        if not os.path.exists(p):
            continue
        try:
            st = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for info in (st.get("installed") or {}).values():
            t = (info or {}).get("applied_at") or ""
            if t and t > best:
                best = t
    if not best:
        return ""
    return best.replace("-", "").replace(":", "").replace(" ", "_")


def read(path):
    if not os.path.exists(path):
        return ""
    return open(path, "rb").read().decode("utf-8", "replace")


def main():
    n = 15
    if len(sys.argv) > 1:
        try:
            n = int(sys.argv[1])
        except ValueError:
            pass

    ds = sorted(glob.glob(os.path.join(LOGS, "run_*")), key=os.path.getmtime, reverse=True)[:n]
    if not ds:
        print("没有找到任何 run 目录")
        return 1

    cut = cutoff()
    before, after = [], []
    for d in ds:
        name = os.path.basename(d)
        stamp = name.replace("run_", "")
        (after if (cut and stamp > cut) else before).append(d)

    data = {os.path.basename(d): read(os.path.join(d, "run.log")) for d in ds}

    def cnt(name, keys, subset):
        return sum(sum(data[os.path.basename(d)].count(k) for k in keys) for d in subset)

    print("=" * 76)
    print("  补丁生效核查")
    print("=" * 76)
    print("  扫描范围：最近 %d 次运行（%s .. %s）"
          % (len(ds), os.path.basename(ds[-1]), os.path.basename(ds[0])))
    print("  分界线（最晚一次补丁安装时间）：%s" % (cut or "（未找到安装记录）"))
    print("  补丁前 %d 次 / 补丁后 %d 次" % (len(before), len(after)))
    if after:
        print("  补丁后的运行：%s" % "  ".join(os.path.basename(d)[-6:] for d in after))
    print()

    print("【一】每个补丁被触发过几次 —— **只有『补丁后』的次数才算验证**")
    print("-" * 76)
    for name, keys, note in EVIDENCE:
        b, a = cnt(name, keys, before), cnt(name, keys, after)
        flag = "✅ 已验证" if a else "⬜ 尚未验证"
        print("  %-26s %s   （补丁后 %d 次 / 补丁前 %d 次）" % (name, flag, a, b))
        print("       %s" % note)
    print()

    print("【二】旧病反证 —— 只看『补丁后』那一列")
    print("-" * 76)
    for name, key in AVOID:
        b = cnt(name, [key], before)
        a = cnt(name, [key], after)
        flag = "✅ 补丁后 0 次" if a == 0 else "⚠ 补丁后仍有 %d 次" % a
        print("  %-42s %s   （补丁前 %d 次）" % (name, flag, b))
    print()

    print("=" * 76)
    print("  ★ 判定规则：**『补丁后有触发』才算验证过。**")
    print("     某条补丁一次都没触发 → 只是**还没遇到那个场景**，不代表失效，")
    print("     但也**不能**因为『这次跑通了』就认为它生效。")
    print("     要验证它，得等那个场景真的出现（例如僵尸窗口、定位漂移）。")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
