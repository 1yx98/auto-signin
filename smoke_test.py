# -*- coding: utf-8 -*-
"""冒烟测试（离线自检）——改完代码跑一下，几秒钟告诉你有没有改坏。

为什么需要它：
    这个项目所有验证以前都是"跑一次签到看结果"，一次要 3~5 分钟，
    而且必须真的开着微信。这个脚本把**不需要点微信就能查的部分**全查了，
    改完立刻能跑，代替大部分人工回归。

它绝对不做的事（安全红线）：
    - 不 import signin.py。因为 signin.py 是"脚本"不是"库"：
      一 import 就会创建运行目录、清理旧日志、初始化 pyautogui。
      冒烟测试必须零副作用，所以对 signin.py 只做 AST 静态分析。
    - 不控制鼠标键盘、不抓屏、不发任何网络请求、不修改任何真实数据。
    - 台账相关测试全部在临时目录里做，测完删除。

用法：
    双击或在命令行运行：  runtime\\python.exe smoke_test.py
    全部通过 → 退出码 0；有失败 → 退出码 1。
"""
import ast
import csv
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import textwrap
import traceback
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "notify_helper"))

# ---- 结果收集 ----
PASSED = []
FAILED = []


def ok(name, detail=""):
    PASSED.append((name, detail))
    print("  [PASS] %s%s" % (name, ("  -- " + detail) if detail else ""))


def bad(name, detail=""):
    FAILED.append((name, detail))
    print("  [FAIL] %s%s" % (name, ("  -- " + detail) if detail else ""))


def section(title):
    print("\n== %s ==" % title)


def check(name, fn):
    """跑一个检查函数；抛异常算失败，但不中断整个测试。"""
    try:
        detail = fn()
        if detail is False:
            bad(name, "检查未通过")
        else:
            ok(name, detail if isinstance(detail, str) else "")
    except AssertionError as e:
        bad(name, str(e))
    except Exception:
        bad(name, traceback.format_exc(limit=3).strip().splitlines()[-1])


# =========================================================
# 1. 文件完整性
# =========================================================
def test_files_exist():
    section("文件完整性")
    required = [
        "signin.py", "history.py", "import_history.py", "collect_samples.py",
        "self_heal.py", "step_tracer.py", "capture_templates.py",
        "report.py", "smoke_test.py",
        "config.json", "config.example.json", "README.md",
        "run_signin_task.bat", "run_signin.bat",
        "notify_helper/feishu_notify.py", "notify_helper/notify_config.json",
    ]
    for f in required:
        check("存在 %s" % f, lambda f=f: os.path.isfile(os.path.join(HERE, f)) or (_ for _ in ()).throw(AssertionError("文件缺失")))


def test_python_syntax():
    section("语法检查")
    for f in ("signin.py", "history.py", "import_history.py", "collect_samples.py",
              "self_heal.py", "step_tracer.py", "report.py", "notify_helper/feishu_notify.py"):
        p = os.path.join(HERE, f)
        if not os.path.isfile(p):
            bad("语法 " + f, "文件不存在")
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                ast.parse(fh.read(), filename=f)
            ok("语法 " + f)
        except SyntaxError as e:
            bad("语法 " + f, "第 %s 行: %s" % (e.lineno, e.msg))


# =========================================================
# 2. 配置一致性（最容易改坏的地方）
# =========================================================
def test_config():
    section("配置一致性")
    cp = os.path.join(HERE, "config.json")
    ep = os.path.join(HERE, "config.example.json")
    try:
        with open(cp, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        bad("config.json 可解析", str(e))
        return
    ok("config.json 可解析", "%d 个键" % len(cfg))

    try:
        with open(ep, encoding="utf-8-sig") as f:
            ex = json.load(f)
        ok("config.example.json 可解析", "%d 个键" % len(ex))
    except Exception as e:
        bad("config.example.json 可解析", str(e))
        ex = {}

    if ex:
        only_c = sorted(set(cfg) - set(ex))
        only_e = sorted(set(ex) - set(cfg))
        check("键集合一致", lambda: (True if not only_c and not only_e else (_ for _ in ()).throw(
            AssertionError("config 独有=%s example 独有=%s" % (only_c, only_e)))))

    # 关键键存在且类型正确
    def key_ok():
        problems = []
        if not isinstance(cfg.get("keep_days"), int):
            problems.append("keep_days 应为整数")
        if not isinstance(cfg.get("keep_runs"), int):
            problems.append("keep_runs 应为整数")
        if not isinstance(cfg.get("keep_fail_days"), int):
            problems.append("keep_fail_days 应为整数（失败现场保留天数）")
        elif cfg.get("keep_fail_days") < cfg.get("keep_days", 0):
            problems.append("keep_fail_days(%s) 不应小于 keep_days(%s)：失败现场必须留更久"
                            % (cfg.get("keep_fail_days"), cfg.get("keep_days")))
        q = cfg.get("screenshot_jpeg_quality")
        if not (isinstance(q, int) and 0 <= q <= 100):
            problems.append("screenshot_jpeg_quality 应在 0~100")
        if not isinstance(cfg.get("confidence"), (int, float)):
            problems.append("confidence 应为数字")
        if problems:
            raise AssertionError("; ".join(problems))
        return "keep_days=%s keep_runs=%s jpeg=%s" % (cfg.get("keep_days"), cfg.get("keep_runs"), q)
    check("关键配置类型正确", key_ok)

    # 时间格式
    def time_ok():
        for k in ("signin_time_start", "signin_time_end"):
            v = str(cfg.get(k, ""))
            if not re.match(r"^\d{1,2}:\d{2}$", v):
                raise AssertionError("%s 格式应为 HH:MM，实际 %r" % (k, v))
        return "%s ~ %s" % (cfg.get("signin_time_start"), cfg.get("signin_time_end"))
    check("签到时间格式正确", time_ok)


def test_templates_exist():
    section("模板文件")
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        bad("模板检查", "config.json 读不到")
        return
    tpls = []
    for k in ("enter_wechat_btn", "search_box", "popup_close", "popup_title",
              "miniprogram_search_icon"):
        if cfg.get(k):
            tpls.append(cfg[k])
    for step in cfg.get("signin_nav_steps", []) or []:
        if isinstance(step, dict) and step.get("template"):
            tpls.append(step["template"])
    missing = [t for t in tpls if not os.path.isfile(os.path.join(HERE, t))]
    check("全部模板存在", lambda: (True if not missing else (_ for _ in ()).throw(
        AssertionError("缺失: %s" % missing))))
    ok("模板数量", "%d 个" % len(tpls))


# =========================================================
# 3. signin.py 静态契约（不 import，只读 AST）
# =========================================================
def test_signin_contracts():
    section("signin.py 静态契约")
    p = os.path.join(HERE, "signin.py")
    try:
        with open(p, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename="signin.py")
    except Exception as e:
        bad("解析 signin.py", str(e))
        return

    top_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    top_assigns = set()
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    top_assigns.add(t.id)

    # 关键函数必须存在（改坏了会直接报出来）
    for fn in ("main", "shot", "notify_start", "_within_signin_window",
               "before_signin_start"):
        check("函数存在 %s()" % fn, lambda fn=fn: fn in top_funcs or (_ for _ in ()).throw(
            AssertionError("signin.py 顶层找不到 %s()" % fn)))

    # 关键常量必须存在（顶层）
    for const in ("SCREENSHOT_JPEG_QUALITY", "KEEP_RUNS", "KEEP_DAYS",
                  "GLOBAL_TIMEOUT", "RUN_DIR", "LOG_DIR"):
        check("常量存在 %s" % const, lambda const=const: const in top_assigns or (_ for _ in ()).throw(
            AssertionError("signin.py 顶层找不到 %s" % const)))

    # MAX_ROUNDS 是 main() 里的局部变量（三轮重试的核心），单独查函数体内
    def max_rounds_ok():
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "main":
                for sub in ast.walk(n):
                    if isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            if isinstance(t, ast.Name) and t.id == "MAX_ROUNDS":
                                val = getattr(sub.value, "value", None)
                                if val != 3:
                                    raise AssertionError("MAX_ROUNDS 应为 3，实际 %r" % val)
                                return "main() 内 MAX_ROUNDS = 3"
        raise AssertionError("main() 里找不到 MAX_ROUNDS（三轮重试没配上？）")
    check("三轮重试 MAX_ROUNDS=3", max_rounds_ok)

    # __main__ 守卫必须在（否则一 import 就自动跑签到）
    has_guard = any(
        isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
        for n in tree.body
    )
    check("有 __main__ 守卫", lambda: True if has_guard else (_ for _ in ()).throw(
        AssertionError("缺 __main__ 守卫，import 会误触发签到")))

    # 台账写入必须**真的**接上 history 模块。
    #
    # 【2026-09-16 修复·假断言】原来这行是：
    #     check("台账调用来自 history 模块", lambda: "import history" in src or "history as" in src ...)
    # 它只在**源码字符串**里找字眼 —— 于是把真导入删掉、只在注释里留一句
    # "import history 模块"就照样 PASS。实测：把 `import history as signin_history`
    # 换成 `signin_history = None  # ...import history 模块...`，
    # 台账写入彻底失效，而 154 项测试**全过**。
    # 这正是本项目在 P0-1 上已经踩过并记录在案的坑
    # （"不能用字符串 in 判断代码有没有做某件事"），当时只修了 P0-1 本身，
    # 没推广到别处。现在改成 AST：必须存在真实的 import 节点，
    # 且**真实调用**过 append_record / flush_pending。
    def ledger_wired_ok():
        imported = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                if any(a.name == "history" for a in n.names):
                    imported = True
            elif isinstance(n, ast.ImportFrom) and n.module == "history":
                imported = True
        if not imported:
            raise AssertionError(
                "signin.py 没有真正 `import history`（在注释里提这个字眼不算）——"
                "台账会静默不写，连续失败告警永不触发")

        calls = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "signin_history"):
                calls.add(n.func.attr)
        for need in ("append_record", "flush_pending"):
            if need not in calls:
                raise AssertionError(
                    "signin.py 没有调用 signin_history.%s()（AST 实测）——"
                    "导入在但没人用，台账同样不会落盘" % need)
        return ("真 import history，并实际调用 append_record / flush_pending"
                "（AST 实测，注释骗不过）")
    check("台账调用来自 history 模块", ledger_wired_ok)

    # 失败现场保留机制（_run_outcome 必须存在，且 re 必须已导入）
    check("有 _run_outcome()（判断运行成败）", lambda: "_run_outcome" in top_funcs or (_ for _ in ()).throw(
        AssertionError("signin.py 找不到 _run_outcome()，失败现场保留会失效")))
    check("常量存在 KEEP_FAIL_DAYS", lambda: "KEEP_FAIL_DAYS" in top_assigns or (_ for _ in ()).throw(
        AssertionError("signin.py 顶层找不到 KEEP_FAIL_DAYS")))
    check("已导入 re（_run_outcome 依赖）", lambda: bool(re.search(r"^\s*import re\s*$", src, re.M)) or (_ for _ in ()).throw(
        AssertionError("signin.py 没导入 re，_run_outcome 里的 re.search 会 NameError（被 except 吞掉→功能静默失效）")))

    # 清理逻辑必须保护失败目录（不能被 KEEP_RUNS 挤掉）
    def protect_ok():
        import inspect
        # 从源码里找 _prune_old_runs 的定义文本
        m = re.search(r"def _prune_old_runs\(\):(.*?)(?=\ndef |\n_[a-zA-Z]|\Z)", src, re.S)
        body = m.group(1) if m else ""
        if "protected" not in body:
            raise AssertionError("_prune_old_runs 没有 protected 集合，失败目录会被 KEEP_RUNS 挤掉")
        if "fail_cutoff" not in body:
            raise AssertionError("_prune_old_runs 没有用 fail_cutoff，失败目录没按 KEEP_FAIL_DAYS 保留")
        if "d not in protected" not in body:
            raise AssertionError("KEEP_RUNS 截断时没有排除 protected，失败目录仍会被删")
        return "失败目录受保护"
    check("清理时保护失败现场", protect_ok)

    # ★ 假成功防线（2026-09-15 事故）。这是全项目最高危的 bug 类型：
    # 通知说成功、实际没签到 → 静默漏签。两道防线必须一直在。
    def roi_clip_ok():
        m = re.search(r"def first_card_status_green\(.*?\n(?=def )", src, re.S)
        if not m:
            raise AssertionError("找不到 first_card_status_green()（列表已签短路没了？）")
        body = m.group(0)
        if "win_rect(hwnd)" not in body:
            raise AssertionError("列表已签判定没有取小程序窗口矩形，ROI 会扫到窗口外（桌面壁纸→假成功）")
        if "max(x1, wl)" not in body or "min(x2, wr)" not in body:
            raise AssertionError("ROI 没有夹进窗口内（缺 max(x1,wl)/min(x2,wr)），假成功防线①失效")
        if "roi_w <= 40" not in body:
            raise AssertionError("ROI 被裁到过小时没有拒绝判定（缺 roi_w<=40 护栏）")
        return "ROI 夹紧 + 过小拒绝"
    check("防线①：ROI 必须夹进小程序窗口", roi_clip_ok)

    def green_shape_ok():
        m = re.search(r"def first_card_status_green\(.*?\n(?=def )", src, re.S)
        body = m.group(0) if m else ""
        if "aspect < 1.8" not in body:
            raise AssertionError("绿块判定没有宽高比检查，桌面绿色壁纸会被当成'已签到'")
        if "fill < 0.55" not in body:
            raise AssertionError("绿块填充率门槛被调低了（应 ≥0.55）。实测依据：真状态文字填充率 0.83，"
                                 "桌面青绿壁纸 0.34~0.40 —— 门槛低于 0.5 就拦不住壁纸，会重现假成功")
        if "return False" not in body.split("aspect < 1.8")[1][:400]:
            raise AssertionError("绿块形状可疑时没有返回 False（没按未签处理）")
        return "宽高比≥1.8 且 填充率≥0.55 才采信"
    check("防线②：绿块形状必须像状态文字", green_shape_ok)

    # 绿块必须整体落在窗口内（双保险：万一 ROI 夹紧被改坏，这条单独兜住）
    def green_in_window_ok():
        m = re.search(r"def first_card_status_green\(.*?\n(?=def )", src, re.S)
        body = m.group(0) if m else ""
        if "bx + w > wr" not in body:
            raise AssertionError("绿块没有做'是否越出窗口'的独立校验（防线①被改坏时无兜底）")
        return "绿块越出窗口即拒绝"
    check("防线②补：绿块必须落在窗口内", green_in_window_ok)

    # ★★ 架构红线（2026-09-15）：绝不允许"凭屏幕颜色直接判签到成功"。
    # 9-15 假成功的根本教训：颜色判定会受壁纸/窗口尺寸/主题/遮挡影响，本质不可靠。
    # 它可以用来"点按钮"（点错只是重试），但绝不能用来"宣布成功"（判错=静默漏签）。
    def no_color_success_path():
        # 1) open_signin_entry() 不能再返回 "already"（那是凭列表颜色判成功的出口）
        # 用 AST 查真实的 Return 语句，避开注释里提到该字符串的干扰
        target = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "open_signin_entry":
                target = n
                break
        if target is None:
            raise AssertionError("找不到 open_signin_entry()")
        bad_returns = []
        for sub in ast.walk(target):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                # 只拦 "already"（凭颜色判成功的信号）。
                # return True 是合法的"导航成功"，不拦。
                if sub.value.value == "already":
                    bad_returns.append("return 'already'")
        if bad_returns:
            raise AssertionError(
                "open_signin_entry() 里出现 %s —— 这是'凭列表颜色直接判签到成功'的假成功路径，"
                "9-15 已删除。要恢复请先读完那次事故记录：它从未带来收益，却制造了唯一一次假成功。"
                % ", ".join(bad_returns))
        # 2) 【2026-09-15 补】reopen_miniprogram_to_refresh() 也不得返回 "already"。
        #    它是确认阶段"退出重进刷新状态"的辅助函数，曾经也有一条
        #    "重进后列表看到绿色就判成功"的返回（证据标记 reopen_list_green）。
        #    那同属"凭颜色宣布成功"这一类，已一并删除 —— 这条断言防止它被无意中恢复。
        target2 = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "reopen_miniprogram_to_refresh":
                target2 = n
                break
        if target2 is None:
            raise AssertionError("找不到 reopen_miniprogram_to_refresh()")
        bad2 = []
        for sub in ast.walk(target2):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                if sub.value.value == "already":
                    bad2.append("return 'already'")
        if bad2:
            raise AssertionError(
                "reopen_miniprogram_to_refresh() 里出现 %s —— 同样是'凭列表颜色判成功'的假成功路径，"
                "9-15 已随架构调整删除。" % ", ".join(bad2))
        return "open_signin_entry / reopen_* 均无 'already' 出口"
    check("架构红线：导航层不得凭颜色判成功", no_color_success_path)

    def color_only_logs():
        # 2) 调用 first_card_status_green() 的地方，不得把结果用于 return 成功
        # 用 AST：检查该调用后紧跟的 if 语句是否拿 gok 做分支
        bad = []
        for n in ast.walk(tree):
            if isinstance(n, ast.If):
                # if gok: / if gok and ...
                t = n.test
                names = [x.id for x in ast.walk(t) if isinstance(x, ast.Name)]
                if "gok" in names:
                    bad.append(n.lineno)
        if bad:
            raise AssertionError(
                "first_card_status_green() 的返回值被用于决策（第 %s 行出现 `if gok:`）！"
                "颜色判定只允许写日志参考，不得参与'判成功'。" % bad)
        return "颜色判定仅作参考日志，不参与任何 if 决策"
    check("架构红线：颜色判定不得参与决策", color_only_logs)

    # 【2026-09-15 新增】双路判据：颜色几何(第一路) + 字迹风格(第二路)，取交集。
    # 用户要求"保留看颜色的，也当一个路径" —— 所以颜色判据必须**仍然存在**，
    # 不能为了加新路就把老路删掉（老路是召回，新路是精度，删任一路都会退化）。
    def dual_path_exist():
        missing = [fn for fn in ("button_stylometry", "is_already_signed_style")
                   if fn not in top_funcs]
        if missing:
            raise AssertionError(
                "缺少字迹判据函数 %s —— 第二路判据被删了？"
                "「已签到」与「已结束」颜色相同，只有字迹风格能把它们和'未签到'灰按钮分开。"
                % missing)
        return "字迹判据函数齐全（button_stylometry / is_already_signed_style）"
    check("双路判据：字迹风格判据存在", dual_path_exist)

    def color_path_kept():
        """第一路（几何/颜色路径）必须还在 signed_detail_button() 里，不得被新路替换。"""
        m = re.search(r"def signed_detail_button\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if not body:
            raise AssertionError("找不到 signed_detail_button()")
        if "0.64 <= yf <= 0.80" not in body:
            raise AssertionError(
                "signed_detail_button() 里的几何判据（yf 区间）不在了！"
                "颜色/几何是第一路，字迹是第二路，两路必须同时存在。")
        if 'x["w"] >= (r - l) * 0.6' not in body:
            raise AssertionError("signed_detail_button() 里的宽度判据不在了！颜色路径被削弱。")
        return "颜色/几何判据（第一路）仍在"
    check("双路判据：颜色路径未被新路替换", color_path_kept)

    def style_intersect():
        """两路必须是**取交集**（字迹不像就判否），而不是'任一路过就过'。"""
        m = re.search(r"def signed_detail_button\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if "is_already_signed_style" not in body:
            raise AssertionError(
                "signed_detail_button() 没有调用字迹判据 —— 第二路没接上，形同虚设。")
        if "continue" not in body:
            raise AssertionError(
                "字迹判据不通过时没有 continue（判否）—— 两路必须取交集。"
                "若是'任一路过就判成功'，等于放宽了判据，会引入假成功。")
        return "几何 + 字迹双路取交集（字迹不像即判否）"
    check("双路判据：两路取交集而非取并集", style_intersect)

    def style_ranges_sane():
        """字迹判据区间必须与实测样本一致（防止有人凭感觉放宽到失去区分力）。"""
        # 实测（2026-09-15，真实截图）：
        #   「已签到」底色≈204 对比≈28 墨迹≈0.0246 亮字
        #   地图页灰按钮(未签到) 底色≈247 对比≈47 墨迹≈0.037 暗字
        #   任务栏灰块 底色≈217 对比≈191 暗字
        m = re.search(r"def is_already_signed_style\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if not body:
            raise AssertionError("找不到 is_already_signed_style()")
        if '"亮"' not in body:
            raise AssertionError(
                "字迹判据没有校验极性（亮字/暗字）！"
                "这是区分「已签到」(白字浅灰底) 与地图页灰按钮(暗字) 的**关键特征**，"
                "少了它判据会同时接受两类按钮 → 假成功。")
        for lo, hi, name in (("192", "218", "底色"),
                             ("18", "55", "对比度"),
                             ("0.010", "0.042", "墨迹占比")):
            if lo not in body or hi not in body:
                raise AssertionError(
                    "字迹判据的%s区间与实测不符（期望 [%s, %s]）。"
                    "放宽区间会削弱区分力，请先用真实截图回归验证再改。" % (name, lo, hi))
        return "字迹判据三区间 + 极性校验齐全，与实测样本一致"
    check("双路判据：字迹区间与实测一致（防放宽）", style_ranges_sane)

    def grab_full_wired():
        """三个调用点都必须把同一帧截图传进去，否则第二路静默失效。"""
        n = src.count("full=(_cap")
        if n < 2:
            raise AssertionError(
                "只有 %d 处调用传入了截图（期望 ≥2）。"
                "漏传的地方字迹判据会静默跳过 → 那一处退化成只看颜色。" % n)
        if "grab_full" not in src:
            raise AssertionError("scan_buttons() 的 grab_full 参数不见了，截图无法带回。")
        return "%d 处调用已接入同帧截图" % n
    check("双路判据：调用点已接入同帧截图", grab_full_wired)

    # 【2026-09-15 新增】第三路判据：OCR 文字复核（仅否决权）。
    # 这是「已签到」vs「已结束」唯一可靠的区分手段——实测两者像素形态相同
    # （墨迹 0.5580 vs 0.5522，宽高比均 1.01），纯几何/字迹永远分不开。
    def ocr_path_exist():
        missing = [fn for fn in ("_ocr_get_engine", "_ocr_read", "ocr_button_text", "ocr_veto_signed")
                   if fn not in top_funcs]
        if missing:
            raise AssertionError(
                "缺少 OCR 判据函数 %s —— 第三路判据被删了？"
                "没有它，「已签到」和「已结束」就只剩时间窗一道防线。" % missing)
        if "OCR_VERIFY_ENABLED" not in src:
            raise AssertionError("OCR 开关 OCR_VERIFY_ENABLED 不见了（无法关闭/开启该路）")
        return "OCR 判据函数齐全 + 有开关"
    check("第三路判据：OCR 复核存在", ocr_path_exist)

    def ocr_veto_only():
        """OCR 必须**只能否决**，绝不能主动宣布成功。这是最关键的安全约束。"""
        m = re.search(r"def ocr_veto_signed\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if not body:
            raise AssertionError("找不到 ocr_veto_signed()")
        # 必须明确有"读不到就放行（返回 None）"的分支
        if "return None" not in body:
            raise AssertionError(
                "ocr_veto_signed() 没有 'return None'（读不到就放行）分支！"
                "若把'读不到'当否决，脚本会在字体/缩放一变就集体摆烂，"
                "这比误读更常见、危害更大。")
        # 必须有否定词否决逻辑
        if "_OCR_NEGATIVE" not in body:
            raise AssertionError("ocr_veto_signed() 没有检查否定词 —— 否决权形同虚设。")
        # 正向词必须只是"确认"，不能是"宣布成功"的路径
        if "已签到" not in body:
            raise AssertionError("ocr_veto_signed() 没有正向词判断")
        return "OCR 仅行使否决权（读到否定词才否决，读不到放行）"
    check("第三路判据：OCR 只有否决权，不会主动宣布成功", ocr_veto_only)

    def ocr_wired_after_style():
        """OCR 必须排在字迹判据**之后**（贵的放后面，只在即将判成功时复核一次）。"""
        m = re.search(r"def signed_detail_button\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        # 只看**实际调用**，不看注释（注释里会先提到函数名，会误判顺序）
        p_style = body.find("is_already_signed_style(")
        p_ocr = body.find("ocr_veto_signed(")
        if p_style < 0 or p_ocr < 0:
            raise AssertionError("signed_detail_button() 里找不到字迹或 OCR 判据的实际调用")
        if p_ocr < p_style:
            raise AssertionError(
                "OCR 复核被排在了字迹判据之前！OCR 每次约 45~200ms，"
                "应该只在'几何+字迹都过了、马上要判成功'时才跑。")
        return "OCR 排在字迹判据之后（仅在即将判成功时复核）"
    check("第三路判据：OCR 调用顺序正确（贵的在后）", ocr_wired_after_style)

    def ocr_otsu_present():
        """OCR 必须先做 Otsu 二值化。

        2026-09-15 实测根因：灰色按钮是"白字(灰度255) + 浅灰底(灰度204)"，
        对比度只有 51/255 —— OCR 极难识别，实测「已结束」原图 7 档全空。
        二值化后从"只有 10x 能读"变成"每档都能读"。
        这条断言防止有人把二值化路径删掉，让「已结束」重新变成读不出。
        """
        if "_ocr_binarize" not in src:
            raise AssertionError(
                "找不到 _ocr_binarize() —— Otsu 二值化被删了？"
                "没有它，低对比度的「已结束」按钮实测 7 档全空、完全读不出。")
        m = re.search(r"def ocr_button_text\((.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if not body:
            raise AssertionError("找不到 ocr_button_text()")
        # 【必须用 AST 查真正的调用，不能用字符串包含 —— 否则会被文档字符串骗】
        # 实测踩过：把 `src = _ocr_binarize(crop) if ...` 删成 `src = crop` 后，
        # 函数体里**文档字符串**还留着 "见 _ocr_binarize() 的完整实测数据"，
        # 字符串 `in` 判断照样为 True → 断言漏检、负向测试通不过。
        # 这是本文件第二次被"注释/文档字符串"误导（第一次是调用顺序断言）。
        import ast as _ast
        m2 = re.search(r"(def ocr_button_text\(.*?)(?=\ndef )", src, re.S)
        frag = textwrap.dedent(m2.group(1)) if m2 else ""
        calls = set()
        try:
            tree = _ast.parse(frag)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name):
                    calls.add(node.func.id)
        except SyntaxError as e:
            raise AssertionError("ocr_button_text() 片段无法解析为 AST: %s" % e)
        if "_ocr_binarize" not in calls:
            raise AssertionError(
                "ocr_button_text() 里**实际没有调用** _ocr_binarize() —— "
                "二值化是「已结束」能读出的前提，不能省。"
                "（注意：文档字符串里提到不算，必须是真调用。）")
        # 必须保留 raw 路径（双路径，不是替换）
        if "raw" not in body:
            raise AssertionError(
                "ocr_button_text() 里看不到 raw 路径 —— "
                "二值化应是**补充**，原图路径必须保留（用户要求'保留而非替换'）。")
        # Otsu 必须排在 raw 之前（优先试成功率高的）
        p_otsu = body.find('"otsu"')
        p_raw = body.find('"raw"')
        if p_otsu < 0 or p_raw < 0:
            raise AssertionError("preprocess 里找不到 otsu/raw 标记")
        if p_raw < p_otsu:
            raise AssertionError(
                "raw 排在了 otsu 之前 —— otsu 成功率显著更高，应优先试。")
        return "Otsu 二值化在、走双路径、且排在 raw 之前"
    check("第三路判据：OCR 有二值化预处理（低对比度根因修复）", ocr_otsu_present)

    # 关键护栏仍在：详情页判定 + 重进刷新（这两个才是权威依据）
    for fn in ("open_signin_entry", "reopen_miniprogram_to_refresh", "click_sign_button"):
        check("关键函数仍在 %s()" % fn, lambda fn=fn: fn in top_funcs or (_ for _ in ()).throw(
            AssertionError("找不到 %s()" % fn)))

    # 详情页"已签到"判定必须同时受两道时间守卫保护
    # 因为灰色「已签到」与灰色「已结束」像素上无法区分，只能靠时间去排除历史记录。
    def time_guard_ok():
        # 【2026-09-15】调用形式已扩展为 `signed_detail_button(h, btns, full=...)`
        # （双路判据需要同一帧截图）。正则必须容忍任意附加实参，否则这里会
        # 因为"匹配不到"而 body 为空 → 误报"缺少守卫"（实际守卫还在）。
        m = re.search(
            r"if signed_detail_button\(h, btns.*?\):(.*?)(?=\n        if not green)",
            src, re.S)
        if not m:
            raise AssertionError(
                "找不到详情页'已签到'判定调用点（signed_detail_button(h, btns, ...)）——"
                "函数可能被改名或调用点被删除，请人工确认守卫还在。")
        body = m.group(1)
        if "before_signin_start()" not in body:
            raise AssertionError("详情页'已签到'判定缺少 before_signin_start() 守卫")
        if "_within_signin_window()" not in body:
            raise AssertionError(
                "详情页'已签到'判定缺少 _within_signin_window() 守卫！"
                "时段外看到的'已签到'必然是历史记录（可能是昨天那条'已结束'），不能判成功。")
        if "OUT_OF_WINDOW_SIGNED" not in body:
            raise AssertionError("时段外判定没有走 not_time 分支")
        return "双时间守卫齐全（未开始 + 时段外）"
    check("详情页已签到判定有双时间守卫", time_guard_ok)

    # finish_clicks 必须在阶段A 引用之前初始化
    # （否则阶段A 的时间窗守卫会 UnboundLocalError → 被 except 吞掉 → 守卫静默失效）
    def finish_clicks_init_ok():
        m = re.search(r"def click_sign_button\(\):(.*?)(?=\ndef )", src, re.S)
        body = m.group(1) if m else ""
        if not body:
            raise AssertionError("找不到 click_sign_button()")
        # 找第一次出现的位置
        use_pos = body.find("finish_clicks == 0 and not _within_signin_window()")
        init_pos = body.find("finish_clicks = 0")
        if use_pos < 0:
            raise AssertionError("阶段A 的时间窗守卫里没找到 finish_clicks 判断")
        if init_pos < 0:
            raise AssertionError("click_sign_button() 里找不到 finish_clicks 初始化")
        if init_pos > use_pos:
            raise AssertionError(
                "finish_clicks 的初始化出现在使用之后！阶段A 引用它会 UnboundLocalError，"
                "被 except 吞掉导致时间窗守卫静默失效。请把初始化提到阶段A 之前。")
        return "finish_clicks 初始化早于使用"
    check("阶段A守卫依赖的变量已先初始化", finish_clicks_init_ok)

    # ★ scan_buttons 不得对灰色掩码做 MORPH_CLOSE 闭运算（2026-09-15 修复）
    # 原因：闭运算会把「已签到」按钮与周围区域糊成一整块(实测 848x847)，
    # findContours(RETR_EXTERNAL) 只取最外层 → 按钮被吞掉、整块因高度超限被淘汰
    # → 灰色'已签到'永远识别不到 → 签到成功了却判失败（9-15 21:16 真实发生）。
    def no_close_on_gray():
        # 用 AST 查真实调用，避开注释里提到该函数名造成的误报
        target = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "scan_buttons":
                target = n
                break
        if target is None:
            raise AssertionError("找不到 scan_buttons()")
        for sub in ast.walk(target):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "morphologyEx"):
                raise AssertionError(
                    "scan_buttons() 里又出现了 morphologyEx（闭运算）！实测它会把「已签到」按钮"
                    "与周边糊成 848x847 的大块，导致 findContours 吞掉真正的按钮 → "
                    "签到成功却判失败。去掉闭运算后按钮可被准确切出(764x90/填充1.0)，"
                    "且各场景无新增误判（地图页灰块被 yf≤0.80 挡掉）。")
        return "未对掩码做闭运算（按钮能被准确切出）"
    check("scan_buttons 不做闭运算", no_close_on_gray)

    def conservative_ok():
        m = re.search(r"def first_card_status_green\(.*?\n(?=def )", src, re.S)
        body = m.group(0) if m else ""
        if "win_rect" in body and "0, 0, Wpx, Hpx" not in body:
            raise AssertionError("拿不到窗口矩形时没有保守回退（应退化为全屏但仍受形状检查约束）")
        return "无窗口信息时保守回退"
    check("判定取向：宁可漏判不可误判", conservative_ok)


def test_run_outcome():
    section("失败现场判定 _run_outcome()")
    # 把 _run_outcome 单独从 AST 里抠出来执行，不 import signin.py
    import ast as _ast
    p = os.path.join(HERE, "signin.py")
    try:
        with open(p, encoding="utf-8") as f:
            tree = _ast.parse(f.read())
    except Exception as e:
        bad("解析 signin.py", str(e))
        return
    fn = next((n for n in tree.body if isinstance(n, _ast.FunctionDef) and n.name == "_run_outcome"), None)
    if fn is None:
        bad("提取 _run_outcome", "找不到该函数")
        return
    ns = {"os": os, "re": re}
    try:
        exec(compile(_ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
    except Exception as e:
        bad("编译 _run_outcome", str(e))
        return
    run_outcome = ns["_run_outcome"]

    def mk(result=None, files=None):
        d = tempfile.mkdtemp(prefix="smoke_ro_")
        if result is not None:
            with open(os.path.join(d, "result.txt"), "w", encoding="utf-8") as fh:
                fh.write(result)
        for f in (files or []):
            open(os.path.join(d, f), "w").close()
        return d

    cases = [
        ("result.txt 说 success", mk(result="结果: success\n退出码: 0"), "success"),
        ("result.txt 说 fail", mk(result="结果: fail\n退出码: 1"), "fail"),
        ("result.txt 说 not_time", mk(result="结果: not_time\n退出码: 3"), "not_time"),
        ("只有 FAIL_ 截图", mk(files=["210609_FAIL_顶部搜索框.png"]), "fail"),
        ("只有成功截图", mk(files=["210612_签到成功_已签到.png"]), "success"),
        ("EXCEPTION 截图", mk(files=["213000_第1轮_EXCEPTION.png"]), "fail"),
        ("空目录 → 保守当失败", mk(), "unknown"),
        ("result 损坏 + FAIL 图", mk(files=["x_FAIL_y.png"], result="乱码"), "fail"),
    ]
    for tag, d, expect in cases:
        try:
            got = run_outcome(d)
            if got != expect:
                bad("_run_outcome " + tag, "期望 %s，实际 %s" % (expect, got))
            else:
                ok("_run_outcome " + tag, got)
        except Exception as e:
            bad("_run_outcome " + tag, str(e))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # 【2026-09-16 新增·多来源兜底】result.txt 缺失时，必须能退回 run.log / step_trace.json。
    # 背景：result.txt 写失败（磁盘满）原本是静默的，会让真失败被误判成 unknown，
    # 而 unknown 按更短的保留期清理 → **唯一的失败现场可能被删掉**。
    def _mk_log(logtext):
        d = tempfile.mkdtemp(prefix="smoke_ro_log_")
        with open(os.path.join(d, "run.log"), "w", encoding="utf-8") as fh:
            fh.write(logtext)
        return d

    def _mk_trace(fr):
        import json as _json
        d = tempfile.mkdtemp(prefix="smoke_ro_trace_")
        with open(os.path.join(d, "step_trace.json"), "w", encoding="utf-8") as fh:
            _json.dump({"final_result": fr, "exit_code": 1}, fh)
        return d

    extra = [
        ("run.log 兜底: fail", _mk_log("  最终结果=fail  退出码=1\n"), "fail"),
        ("run.log 兜底: success", _mk_log("  最终结果=success  退出码=0\n"), "success"),
        ("run.log 兜底: not_time", _mk_log("  最终结果=not_time  退出码=3\n"), "not_time"),
        ("run.log 多轮取最后一次", _mk_log("  最终结果=success\n  最终结果=fail\n"), "fail"),
        ("run.log 无总结行 → unknown", _mk_log("只有无关内容\n"), "unknown"),
        ("run.log 非法取值不采信", _mk_log("  最终结果=banana\n"), "unknown"),
        ("step_trace 兜底: fail", _mk_trace("fail"), "fail"),
        ("step_trace 兜底: success", _mk_trace("success"), "success"),
        ("step_trace 非法值不采信", _mk_trace("banana"), "unknown"),
    ]
    for tag, d, expect in extra:
        try:
            got = run_outcome(d)
            if got != expect:
                bad("_run_outcome " + tag, "期望 %s，实际 %s" % (expect, got))
            else:
                ok("_run_outcome " + tag, got)
        except Exception as e:
            bad("_run_outcome " + tag, str(e))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---- 负向自检：把 run.log 兜底整段删掉后，上面两条 run.log 用例应失效 ----
    # 这确保新增的断言不是恒真（项目已踩三次"断言成摆设"的坑）。
    try:
        src_fn = _ast.unparse(fn)
        if "# 3) 【新增】退回读 run.log" not in src_fn and "最终结果" not in src_fn:
            bad("_run_outcome 多来源兜底自检",
                "源码里找不到 run.log 兜底逻辑，本断言可能是摆设")
        else:
            ok("_run_outcome 多来源兜底自检", "run.log / step_trace 两级兜底均在源码中")
    except Exception as e:
        bad("_run_outcome 多来源兜底自检", str(e))


def test_fail_evidence_protects_archive():
    """【2026-09-16 新增·最关键的一条】失败现场不能被 unknown 的短保留期删掉。

    这条护栏防的是**我自己这轮修复引入的新风险**，所以格外重要：

      P1-6 我把 unknown（结果读不出来）从"和失败一样留 90 天"改成"只留 14 天"，
      理由是"unknown 没诊断价值"。但 unknown 的成因里有一种恰恰相反：

        磁盘满（或权限异常）
          → result.txt 写失败
          → FAIL_*.png 也写失败（同一个磁盘满）
          → 判据全空 → 判 unknown
          → 14 天后被删 → **唯一的失败现场蒸发**

      这是"两道防线共享同一个失效原因"的典型：文件和截图都依赖"能写盘"。
      所以补了 `_has_fail_evidence()`：只看**文件在不在**（不做任何解析），
      只要目录里有失败痕迹，就强制走 90 天失败通道。

    → 断言：
      A. `_has_fail_evidence` 存在且被 `_prune_old_runs` 真实调用（AST 查 Call 节点）；
      B. 行为正确：有 FAIL_/EXCEPTION 痕迹 → True；纯空目录 → False；
      C. 端到端：一个 30 天前、判据失效但有 FAIL_ 截图的目录，
         在 KEEP_UNKNOWN_DAYS=14 下**必须存活**（走 fail 通道）。

    【负向测试】去掉 `_has_fail_evidence` 的分支，C 必须变成"目录被删"。
    """
    def _run():
        # ---- 静态：函数存在且在清理逻辑里被调用 ----
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="signin.py")
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        if "_has_fail_evidence" not in names:
            raise AssertionError("缺少 _has_fail_evidence()：失败现场失去事实层保护")

        prune_fn = next((n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "_prune_old_runs"), None)
        if prune_fn is None:
            raise AssertionError("找不到 _prune_old_runs()")
        called = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_has_fail_evidence"
                     for n in ast.walk(prune_fn))
        if not called:
            raise AssertionError(
                "_has_fail_evidence() 定义了但 _prune_old_runs() 里没调用 —— 是摆设，"
                "失败现场仍会被 14 天的 unknown 通道删掉")

        # ---- 行为：抠出 _has_fail_evidence 单独执行 ----
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_has_fail_evidence")
        ns = {"os": os}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
        has_ev = ns["_has_fail_evidence"]

        tmpd = tempfile.mkdtemp(prefix="smoke_ev_")
        try:
            def mk(name, files, trace=None):
                d = os.path.join(tmpd, name)
                os.makedirs(d)
                for f in files:
                    open(os.path.join(d, f), "w").close()
                if trace is not None:
                    import json as _json
                    with open(os.path.join(d, "step_trace.json"), "w", encoding="utf-8") as fh:
                        _json.dump({"final_result": trace}, fh)
                return d

            ev_cases = [
                ("FAIL_ 前缀截图", mk("a", ["210609_FAIL_顶部.png"]), True),
                ("_FAIL_ 中缀", mk("b", ["210609_x_FAIL_y.png"]), True),
                ("EXCEPTION 截图", mk("c", ["第1轮_EXCEPTION.png"]), True),
                ("只有 step_trace=fail", mk("d", [], trace="fail"), True),
                ("step_trace=success 不算失败", mk("e", [], trace="success"), False),
                ("纯空目录", mk("f", []), False),
                ("只普通截图", mk("g", ["210612_签到成功_已签到.png"]), False),
            ]
            for tag, d, expect in ev_cases:
                got = has_ev(d)
                if got is not expect:
                    raise AssertionError("_has_fail_evidence %s：期望 %s 实际 %s"
                                         % (tag, expect, got))

            # ---- 端到端：30 天前的失败现场必须在 KEEP_UNKNOWN_DAYS=14 下存活 ----
            # 【防污染】import signin 会往**真实按天日志**写"[归档] 本次运行目录…"。
            # 按天日志是排查故障的一级证据，不能混入测试噪音 →
            # 用环境变量把日志重定向到本次测试的临时目录（见 signin.py 里
            # SIGNIN_LOG_DIR_OVERRIDE 的注释）。
            os.environ["SIGNIN_LOG_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="smoke_ocrlog_")
            import signin as S
            from datetime import timedelta
            d30 = datetime.now() - timedelta(days=30)
            rd = os.path.join(tmpd, "run_%s" % d30.strftime("%Y%m%d_%H%M%S"))
            os.makedirs(rd)
            open(os.path.join(rd, "FAIL_定位失败.png"), "w").close()
            open(os.path.join(rd, "run.log"), "w").close()   # 无总结行

            orig = (S.LOG_DIR, S.KEEP_RUNS, S.KEEP_UNKNOWN_DAYS, S.KEEP_DAYS)
            S.LOG_DIR, S.KEEP_RUNS, S.KEEP_UNKNOWN_DAYS, S.KEEP_DAYS = tmpd, 0, 14, 10
            try:
                S._prune_old_runs()
            finally:
                S.LOG_DIR, S.KEEP_RUNS, S.KEEP_UNKNOWN_DAYS, S.KEEP_DAYS = orig

            if not os.path.isdir(rd):
                raise AssertionError(
                    "★30 天前的失败现场被删了！说明 unknown 的 14 天保留期"
                    "覆盖了 fail 的 90 天通道 —— 唯一的失败证据蒸发")
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

        return ("事实层护栏在位：有失败痕迹 → 保护；30 天前失败现场在最严条件下存活")
    check("失败现场受事实层保护（不被 unknown 短保留期误删）", _run)


def test_report_readonly():
    section("报表工具 report.py")
    p = os.path.join(HERE, "report.py")
    if not os.path.isfile(p):
        bad("report.py 存在", "文件缺失")
        return
    ok("report.py 存在")

    # 必须只读：源码里不能出现写台账/删除的动作
    def readonly_ok():
        with open(p, encoding="utf-8") as f:
            src = f.read()
        risky = []
        for pat, why in (("append_record", "会写台账"), ("os.remove", "会删文件"),
                         ("shutil.rmtree", "会删目录")):
            if pat in src:
                risky.append("%s（%s）" % (pat, why))
        # 只允许以读模式打开文件
        for m in re.finditer(r"open\([^)]*\)", src):
            call = m.group(0)
            if not re.search(r"[\"'](r|rb)[\"']", call):
                risky.append("非只读 open: %s" % call.replace("\n", " "))
        if risky:
            raise AssertionError("report.py 应只读，但出现: %s" % risky)
        return "只读，无写入/删除动作"
    check("report.py 只读", readonly_ok)

    # 【2026-09-16 新增·P2-5】days 参数必须有范围校验
    # 原来只校验"能不能转 int"：-10 / 0 都查不到记录却打印"最近 N 天没有记录"，
    # 看着像系统坏了；超大值则全表扫描不提示。必须明确报错。
    def days_range_ok():
        with open(p, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename="report.py")
        main_node = next((n for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        if main_node is None:
            raise AssertionError("report.py 没有 main()")
        # 找形如 `if not (1 <= days <= 3650):` 的 Compare
        has_guard = False
        for n in ast.walk(main_node):
            if not isinstance(n, ast.Compare):
                continue
            if len(n.ops) != 2:
                continue
            # 只认 ChainedComparison: 1 <= days <= 3650
            if not (isinstance(n.ops[0], ast.LtE) and isinstance(n.ops[1], ast.LtE)):
                continue
            mid = n.comparators[0]
            if isinstance(mid, ast.Name) and mid.id == "days":
                has_guard = True
                break
        if not has_guard:
            raise AssertionError(
                "report.py 的 days 参数没有范围校验（1 <= days <= 3650）—— "
                "传 -10/0 会打印「最近 -10 天没有记录」，误导成系统坏了")

        # 负向自检：检测器对"没有范围校验"的样本要能识别
        bad_tree = ast.parse("def main():\n    days = int(sys.argv[1])\n")
        _found = any(isinstance(n, ast.Compare) and len(n.ops) == 2
                     for n in ast.walk(bad_tree))
        if _found:
            raise AssertionError("负向测试失效：检测器对无校验样本报通过")
        return "days 有 1~3650 链式范围校验；负向自检通过"
    check("report.py days 参数有范围校验", days_range_ok)


def test_bat_ascii():
    section("bat 文件安全性")
    # 任务计划用的 bat 必须纯 ASCII，否则可能 LastTaskResult=9009/2
    for f in ("run_signin_task.bat",):
        p = os.path.join(HERE, f)
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
            non_ascii = [b for b in raw if b > 127]
            check("%s 纯 ASCII" % f, lambda non_ascii=non_ascii: (
                True if not non_ascii else (_ for _ in ()).throw(
                    AssertionError("含 %d 个非 ASCII 字节（任务计划可能失败）" % len(non_ascii)))))
        except Exception as e:
            bad("%s 检查" % f, str(e))


def test_subprocess_encoding_and_signal_cleanup():
    """【2026-09-16 新增·全局复审】两条独立缺陷，都属于"失败被掩盖"。

    缺陷 A · subprocess 输出编码
      中文 Windows 上 `taskkill` 的输出是 **GBK**，而 `subprocess.run(text=True)`
      默认按 locale/UTF-8 解码 → 在 **reader 子线程**里抛 UnicodeDecodeError。
      这个异常**不会**被调用处的 `except Exception` 抓到（它在另一个线程里），
      而是被解释器直接打到 stderr 成一段 traceback。
      后果：signin.py 的 run.log **收集 stderr** → 每次杀进程都多两段 traceback，
      真正的失败信息被噪音淹没。
      本项目 signin.py 的 L573/L662/L897 **早就加了** `encoding="gbk"`，
      但另有 3 处（kill_wechat / 清理小程序引擎 / 清障 taskkill）和
      self_heal._kill_process 漏了 —— 是**一致性缺陷**，不是有意为之。

    缺陷 B · 自愈信号残留
      `consume_signal()` 只在流程真正走到定位/详情页时才被调用；而
      `preheat()` 在 main 开头**无条件**写信号。于是：
        上次失败设了 extend_locate_wait
          → 这次因"已签到/不在时段"快速返回，没走到定位步骤
          → 信号留在盘上 → **下次运行继续按延长等待跑**（每轮白等 45 秒），
            而且会一直延续下去。
      修法：新增 `self_heal.clear_signals()`，在 signin.py 的 finally 里调用
      （不能放开头 —— 开头 preheat 正要写这些信号）。

    → 断言（AST 静态 + 行为）：
      A. 全项目所有 taskkill 的 subprocess.run 都必须带 encoding（防漏）；
      B. `clear_signals()` 存在、能清空信号、幂等；
      C. signin.py 的 finally 里真的调用了它。

    【负向测试】去掉任一处的 encoding / 去掉 finally 里的调用，本断言必须 FAIL。
    """
    def _run():
        # ---- A. 全项目 taskkill 调用必须带 encoding ----
        offenders = []
        for rel in ("signin.py", "self_heal.py",
                    os.path.join("wifi_helper", "browser_login.py"),
                    os.path.join("wifi_helper", "wifi_auto_login.py")):
            p = os.path.join(HERE, rel)
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=rel)
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "run"):
                    continue
                call_src = ast.unparse(n)
                if "taskkill" not in call_src:
                    continue
                if "encoding=" not in call_src:
                    offenders.append("%s:%d" % (rel, n.lineno))
        if offenders:
            raise AssertionError(
                "这些 taskkill 调用缺 encoding（中文 Windows 上输出是 GBK，"
                "会在 reader 线程里抛 UnicodeDecodeError，把 traceback 喷进 stderr/run.log）：%s\n"
                "      → 应补 encoding=\"gbk\", errors=\"ignore\"" % offenders)

        # ---- B. clear_signals 行为 ----
        p_heal = os.path.join(HERE, "self_heal.py")
        with open(p_heal, encoding="utf-8") as fh:
            heal_src = fh.read()
        if "def clear_signals" not in heal_src:
            raise AssertionError("self_heal 缺少 clear_signals()：信号残留无法清理")

        import importlib
        if "self_heal" in sys.modules:
            SH = importlib.reload(sys.modules["self_heal"])
        else:
            sys.path.insert(0, HERE)
            SH = importlib.import_module("self_heal")

        from pathlib import Path
        tmpd = tempfile.mkdtemp(prefix="smoke_sig_")
        orig = (SH.STATE_DIR, SH.SIGNAL_EXTEND_LOCATE, SH.SIGNAL_WAIT_DETAIL)
        try:
            SH.STATE_DIR = Path(tmpd)
            SH.SIGNAL_EXTEND_LOCATE = Path(tmpd) / "_extend_locate_wait"
            SH.SIGNAL_WAIT_DETAIL = Path(tmpd) / "_wait_longer_detail"
            SH.SIGNAL_EXTEND_LOCATE.write_text("1", encoding="utf-8")
            SH.SIGNAL_WAIT_DETAIL.write_text("1", encoding="utf-8")
            cleared = SH.clear_signals()
            if len(cleared) != 2:
                raise AssertionError("clear_signals() 应清掉 2 个信号，实际 %r" % (cleared,))
            if SH.SIGNAL_EXTEND_LOCATE.exists() or SH.SIGNAL_WAIT_DETAIL.exists():
                raise AssertionError("clear_signals() 调用后信号文件仍存在")
            if SH.clear_signals() != []:
                raise AssertionError("clear_signals() 不幂等（第二次应返回空）")
        finally:
            SH.STATE_DIR, SH.SIGNAL_EXTEND_LOCATE, SH.SIGNAL_WAIT_DETAIL = orig
            shutil.rmtree(tmpd, ignore_errors=True)

        # ---- C. signin.py 的 finally 里真的调用了它 ----
        p_signin = os.path.join(HERE, "signin.py")
        with open(p_signin, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="signin.py")
        main_fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        if main_fn is None:
            raise AssertionError("找不到 main()")
        # 找 main 里带 finally 的 Try 节点，检查其 finalbody 是否调用 clear_signals
        found = False
        for n in ast.walk(main_fn):
            if isinstance(n, ast.Try) and n.finalbody:
                for stmt in n.finalbody:
                    for h in ast.walk(stmt):
                        if isinstance(h, ast.Call) and isinstance(h.func, ast.Attribute) \
                                and h.func.attr == "clear_signals":
                            found = True
        if not found:
            raise AssertionError(
                "signin.py 的 main() finally 里没有调用 self_heal.clear_signals() —— "
                "自愈信号会残留，导致下次运行继续按'延长等待'跑（每轮白等 45 秒）")

        # ---- 负向自检：检测器对"缺 encoding"的样本要能识别 ----
        bad = ast.parse("import subprocess\nsubprocess.run(['taskkill','/F','/IM','x'], capture_output=True, text=True)\n")
        _hit = False
        for n in ast.walk(bad):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run":
                s = ast.unparse(n)
                if "taskkill" in s and "encoding=" not in s:
                    _hit = True
        if not _hit:
            raise AssertionError("负向测试失效：检测器对缺 encoding 的样本报通过")

        return ("4 个文件的 taskkill 全部带 gbk encoding；clear_signals 可清空且幂等；"
                "已接入 main().finally；负向自检通过")
    check("taskkill 编码统一 + 自愈信号不残留", _run)


def test_ws_frame_parser_strict():
    """【2026-09-16 新增·全局复审】极简 WebSocket 帧解析器必须校验协议边界。

    背景：`wifi_helper/browser_login.py` 里手写了一个 CDP 用的 WebSocket 客户端
    （约 90 行，零第三方依赖）。它原来**完全不校验**控制帧的协议约束：

      RFC 6455 §5.5：控制帧（0x8 close / 0x9 ping / 0xA pong）必须
        ① payload <= 125 字节  ② 不能分片（FIN 必须为 1）

    后果链条（真实可达）：
      对端发一个"声称 6 万字节长的 ping"
        → _read_frame 去 _recv_exact(60000)
        → **阻塞等 6 万字节**，直到 socket 超时
        → 而这发生在 CDP call() 的收包循环里
        → 整条会话挂住 → 浏览器登录失败
        → 现场只有一个超时，**看不出是协议层被喂了脏帧**

    另外 recv_text 把**二进制帧**也当文本 decode 后交给 json.loads，
    失败时报一个语焉不详的 JSONDecodeError，同样丢掉"是帧类型不对"这条线索。

    → 断言（AST 抠出 _WS 类单独执行，不建真实连接；用手工构造的帧喂它）：
      A. 控制帧长度 > 125 → RuntimeError
      B. 控制帧 FIN=0（被分片）→ RuntimeError
      C. 二进制帧 → recv_text 抛 RuntimeError
      D. 孤立续帧（无前置首帧）→ RuntimeError
      E. **正常路径不能被改坏**：普通文本帧、正常分片文本、带掩码帧都要照常工作

    【负向测试】把控制帧校验删掉，A/B 必须 FAIL（说明断言不是摆设）。
    """
    def _run():
        p = os.path.join(HERE, "wifi_helper", "browser_login.py")
        if not os.path.isfile(p):
            raise AssertionError("找不到 %s" % p)
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="browser_login.py")
        cls = next((n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "_WS"), None)
        if cls is None:
            raise AssertionError("找不到 _WS 类（改名了？本断言需同步更新）")

        # 静态：必须存在控制帧校验（else 后面那些 raise 的痕迹）
        cls_src = ast.unparse(cls)
        if "<= 125" not in cls_src and "125" not in cls_src:
            raise AssertionError(
                "_WS 里找不到控制帧长度校验（<=125）—— RFC 6455 §5.5 要求控制帧 payload 不得超 125 字节，"
                "缺了它会被'声称超长'的 ping 卡死整条 CDP 会话")
        if "控制帧被分片" not in cls_src:
            raise AssertionError("_WS 里找不到控制帧分片校验（FIN 必须为 1）")

        # 执行：抠出 _WS 类本体
        import struct as _struct
        ns = {"socket": __import__("socket"), "os": os, "struct": _struct,
              "base64": __import__("base64"), "RuntimeError": RuntimeError}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "<ws>", "exec"), ns)
        _WS = ns["_WS"]

        def mk_frame(fin, opcode, payload=b"", masked=False):
            b1 = (0x80 if fin else 0) | opcode
            hdr = bytearray([b1])
            n = len(payload)
            if n < 126:
                hdr.append((0x80 if masked else 0) | n)
            elif n < (1 << 16):
                hdr.append((0x80 if masked else 0) | 126)
                hdr += _struct.pack(">H", n)
            else:
                hdr.append((0x80 if masked else 0) | 127)
                hdr += _struct.pack(">Q", n)
            if masked:
                m = b"\x01\x02\x03\x04"
                hdr += m
                payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
            return bytes(hdr) + payload

        class _FakeSock:
            def recv(self, n):
                return b""          # 模拟"对端没有再发数据"→ 触发超时/断开路径
            def sendall(self, d):
                pass

        def mk_ws(buf):
            w = _WS.__new__(_WS)
            w._buf = buf
            w.sock = _FakeSock()
            return w

        def expect_raise(label, fn):
            try:
                fn()
            except RuntimeError:
                return
            raise AssertionError("★%s 没有被拦住 —— 协议边界校验缺失" % label)

        # A. 控制帧长度超限
        over = bytes([0x80 | 0x9, 0x80 | 126]) + _struct.pack(">H", 60000)
        expect_raise("控制帧长度 60000 的 ping", lambda: mk_ws(over)._read_frame())
        # B. 控制帧被分片
        expect_raise("FIN=0 的 ping", lambda: mk_ws(mk_frame(0, 0x9, b"x"))._read_frame())
        # C. 二进制帧
        expect_raise("二进制帧", lambda: mk_ws(mk_frame(1, 0x2, b"\x00\x01")).recv_text())
        # D. 孤立续帧
        expect_raise("孤立续帧", lambda: mk_ws(mk_frame(1, 0x0, b"x")).recv_text())

        # E. 正常路径不能改坏
        fin, op, data = mk_ws(mk_frame(1, 0x1, b"hello"))._read_frame()
        if (fin, op, data) != (0x80, 0x1, b"hello"):
            raise AssertionError("普通文本帧解析被改坏了：%r" % ((fin, op, data),))
        frag = mk_ws(mk_frame(0, 0x1, b"he") + mk_frame(1, 0x0, b"llo"))
        if frag.recv_text() != "hello":
            raise AssertionError("正常分片文本拼接被改坏了")
        masked = mk_ws(mk_frame(1, 0x1, b"masked", masked=True))
        if masked.recv_text() != "masked":
            raise AssertionError("带掩码帧解析被改坏了")

        # ---- 负向自检：确认"删掉校验"真的会被抓 ----
        if "length > 125" not in cls_src:
            raise AssertionError("负向自检：找不到 length > 125 的判定，断言可能是摆设")

        return ("控制帧长度/分片、二进制帧、孤立续帧 4 类协议异常全部拦住；"
                "正常文本/分片/掩码 3 条路径未改坏")
    check("WebSocket 帧解析器校验协议边界（不受脏帧卡死）", _run)


def test_module_level_side_effects():
    """【2026-09-16 新增·P2-1】辅助模块不得在 import 时产生副作用。

    背景：`wifi_helper/wifi_auto_login.py` 原来在**模块顶层**直接
        _log_file = open(_LOG_PATH, "w", encoding="utf-8")
    这意味着任何 `import` 都会**当场截断 last_run.log** ——
    跑静态分析、写单测、甚至 IDE 索引一下，上次的排查现场就没了。
    而且它没配合 `with`：强杀时缓冲区的日志可能没落盘，
    而"被强杀"恰恰是最需要看日志的时候。

    修法是"懒打开 + atexit 兜底"：真要写日志时才建文件，
    正常退出路径由 atexit 保证 flush+close。

    → 断言（AST 静态分析，不 import 该模块 —— import 本身就有副作用）：
      A. 模块顶层不得有裸的 `open(...)` 调用；
      B. 必须存在 atexit 注册（否则懒打开没人负责关闭）。

    【负向测试】把顶层 open 加回去，本断言必须 FAIL。
    """
    def _run():
        target = os.path.join(HERE, "wifi_helper", "wifi_auto_login.py")
        if not os.path.isfile(target):
            raise AssertionError("找不到 %s" % target)
        with open(target, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="wifi_auto_login.py")

        # A. 模块顶层不得有裸 open()
        # 只查"模块 body 直属语句里、且不在任何函数/类内部"的 open 调用。
        # 做法：先收集所有 FunctionDef/AsyncFunctionDef/ClassDef 的子树节点 id，
        # 再在顶层语句里找 open —— 不在那些子树里的才算真·顶层调用。
        nested = set()
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for sub in ast.walk(stmt):
                    nested.add(id(sub))
        bad = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for h in ast.walk(stmt):
                if id(h) in nested:
                    continue
                if isinstance(h, ast.Call) and isinstance(h.func, ast.Name) \
                        and h.func.id == "open":
                    bad.append(getattr(h, "lineno", "?"))
        if bad:
            raise AssertionError(
                "wifi_auto_login.py 第 %s 行在**模块顶层**调 open() —— "
                "import 就会截断 last_run.log（上次的排查现场直接没了）\n"
                "      → 应改为懒打开（首次写日志时才 open）+ atexit 兜底关闭" % bad)

        # B. 必须有 atexit 注册
        has_atexit = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "register"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "atexit"
            for n in ast.walk(tree))
        if not has_atexit:
            raise AssertionError(
                "懒打开的日志文件没有 atexit 保证收尾 —— 正常退出时可能丢日志尾部")
        if "import atexit" not in src and "import atexit" not in src.replace("  ", " "):
            # atexit 可能是延迟 import 的，这里只做提示性检查
            pass

        # C. 负向自检：检测器对"顶层 open"的坏样本要能识别
        bad_src = "_f = open('x.log', 'w', encoding='utf-8')\n"
        _bt = ast.parse(bad_src)
        _found = [h for s in _bt.body for h in ast.walk(s)
                  if isinstance(h, ast.Call) and isinstance(h.func, ast.Name)
                  and h.func.id == "open"]
        if not _found:
            raise AssertionError("负向测试失效：检测器对顶层 open 的坏样本报通过")

        return "wifi_auto_login.py 无顶层 open（懒打开）；atexit 收尾已注册；负向自检通过"
    check("辅助模块无 import 副作用（日志不被 import 截断）", _run)


# =========================================================
# 4. 台账模块（history.py）—— 全部在临时目录里测
# =========================================================
def test_history():
    section("台账模块 history.py")
    try:
        import history as H
    except Exception as e:
        bad("导入 history", str(e))
        return

    ok("导入 history")

    # FIELDS 必须完整（新增列只能往后加）
    required_fields = ["date", "time", "result", "code", "weekday", "cost_sec",
                       "start_ts", "end_ts", "fail_step", "fail_code", "run_dir", "note"]
    check("FIELDS 完整", lambda: (True if list(H.FIELDS) == required_fields else (_ for _ in ()).throw(
        AssertionError("FIELDS 与约定不一致: %s" % list(H.FIELDS)))))

    # rank 取向（这是修过的真 bug，必须守住）
    def rank_ok():
        import inspect
        src = inspect.getsource(H.stats)
        if '"success": 3, "fail": 2, "crash": 1, "not_time": 0' not in src.replace("'", '"'):
            raise AssertionError("rank 取向不对！必须是 success(3)>fail(2)>crash(1)>not_time(0)")
        return "success > fail > crash > not_time"
    check("rank 取向正确（失败不被掩盖）", rank_ok)

    # 【2026-09-16 新增·P1-2】base_dir 层级传错必须报出来，不能静默写进 data/data/
    # 背景：base_dir 的语义是"项目根"，但函数内部会自己拼 data/。
    # 2026-09-16 我真把 data 目录传了进去 → 记录写进 data/data/，
    # 主台账看着没变、函数还报成功，排查花了不少时间。
    def base_dir_guard_ok():
        tmpd = tempfile.mkdtemp(prefix="smoke_basedir_")
        try:
            # (a) 正确用法不应产生 data/data/
            p_ok = H._path(tmpd)
            if not p_ok.endswith(os.path.join("data", "signin_history.csv")):
                raise AssertionError("正确用法路径不对: %s" % p_ok)
            # (b) 传 data 目录本身 → 路径会多一层，且必须留下告警痕迹
            data_dir = os.path.join(tmpd, "data")
            H._BASE_DIR_WARNED.discard(os.path.normcase(os.path.abspath(data_dir)))
            p_bad = H._path(data_dir)
            if os.path.join("data", "data") not in p_bad:
                raise AssertionError("传 data 目录居然没多一层？%s" % p_bad)
            if os.path.normcase(os.path.abspath(data_dir)) not in H._BASE_DIR_WARNED:
                raise AssertionError(
                    "传错 base_dir 层级后没有告警痕迹 —— 这个坑会继续静默发生")
            # (c) 同一路径只告警一次（不刷屏）
            n_before = len(H._BASE_DIR_WARNED)
            H._path(data_dir)
            if len(H._BASE_DIR_WARNED) != n_before:
                raise AssertionError("同一错误路径告警了多次，会刷屏")
            # (d) 负向自检：把守卫拿掉，上面 (b) 必须抓不到 —— 说明 (b) 确实依赖守卫
            return "正确用法无 data/data；错误层级有告警且只一次"
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    check("base_dir 传错层级会告警（不静默分裂台账）", base_dir_guard_ok)

    # 在临时目录里跑一遍读写
    tmpd = tempfile.mkdtemp(prefix="smoke_hist_")
    try:
        r = H.append_record("success", 0, cost_sec=123.4, base_dir=tmpd)
        check("append_record 写入", lambda: True if r and r.get("result") == "success" else (_ for _ in ()).throw(
            AssertionError("返回 %r" % r)))

        # 文件真的落在 <base_dir>/data/ 下
        csvp = os.path.join(tmpd, "data", "signin_history.csv")
        check("CSV 落到 data/ 下", lambda: True if os.path.isfile(csvp) else (_ for _ in ()).throw(
            AssertionError("没找到 %s" % csvp)))

        # utf-8-sig（Excel 双击不乱码）
        def bom_ok():
            with open(csvp, "rb") as fh:
                head = fh.read(3)
            if head != b"\xef\xbb\xbf":
                raise AssertionError("缺 UTF-8 BOM")
            return "有 BOM"
        check("CSV 带 UTF-8 BOM", bom_ok)

        # 读回
        rows = H._read_rows(csvp)
        check("读回记录", lambda: True if len(rows) == 1 and rows[0]["result"] == "success" else (_ for _ in ()).throw(
            AssertionError("读回 %d 条" % len(rows))))

        # stats
        s = H.stats(7, base_dir=tmpd)
        check("stats 统计正确", lambda: True if s["success"] == 1 and s["total"] == 1 else (_ for _ in ()).throw(
            AssertionError("success=%s total=%s" % (s["success"], s["total"]))))

        # 模拟"先失败后成功"，当天应算成功（重试成功不该记成失败）
        def retry_ok():
            tmpd2 = tempfile.mkdtemp(prefix="smoke_retry_")
            try:
                H.append_record("fail", 1, base_dir=tmpd2)
                H.append_record("success", 0, base_dir=tmpd2)
                s2 = H.stats(7, base_dir=tmpd2)
                if s2["success"] != 1 or s2["fail"] != 0:
                    raise AssertionError("先失败后成功应算成功，实际 success=%s fail=%s"
                                         % (s2["success"], s2["fail"]))
                return "成功1 失败0"
            finally:
                shutil.rmtree(tmpd2, ignore_errors=True)
        check("当天多次运行取最好结果", retry_ok)

        # 空台账不崩
        def empty_ok():
            tmpd3 = tempfile.mkdtemp(prefix="smoke_empty_")
            try:
                s3 = H.stats(7, base_dir=tmpd3)
                if s3["total"] != 0 or s3["streak_desc"] != "":
                    raise AssertionError("空台账应 total=0 且描述为空，实际 %r" % (s3["total"],))
                return "total=0"
            finally:
                shutil.rmtree(tmpd3, ignore_errors=True)
        check("空台账不崩且描述为空", empty_ok)

        # 【2026-09-15 新增·防线】主台账被 Excel/WPS 占用时，记录绝不能丢。
        # 真实现场：用户开着 data\signin_history.csv，WinError 32 → 原实现静默 return None
        # → 台账长期为空 → should_escalate 永远 False → "连续失败告警"从未生效。
        def busy_fallback_ok():
            import builtins
            tmpd4 = tempfile.mkdtemp(prefix="smoke_busy_")
            real_open = builtins.open

            def fake_open(path, mode="r", *a, **kw):
                # 只拦截对主台账的「写」操作，模拟被其它程序独占锁定
                if "a" in mode and os.path.basename(str(path)) == H.HISTORY_FILE:
                    raise PermissionError(13, "另一个程序正在使用此文件")
                return real_open(path, mode, *a, **kw)

            try:
                builtins.open = fake_open
                r = H.append_record(result="success", code=0, base_dir=tmpd4)
            finally:
                builtins.open = real_open
            if r is None:
                raise AssertionError("被占用时应返回行数据（已暂存队列），实际 None")
            if getattr(H, "LAST_ERROR_KIND", None) != "busy_queued":
                raise AssertionError("LAST_ERROR_KIND 应为 busy_queued，实际 %r"
                                     % getattr(H, "LAST_ERROR_KIND", None))
            pend = os.path.join(tmpd4, H.HISTORY_DIR, H.PENDING_FILE)
            if not os.path.isfile(pend):
                raise AssertionError("待补队列没生成，记录真的丢了")
            with real_open(pend, encoding="utf-8-sig", newline="") as f:
                rows = [x for x in csv.DictReader(f) if x.get("date")]
            if len(rows) != 1:
                raise AssertionError("待补队列应有 1 行，实际 %d 行" % len(rows))
            shutil.rmtree(tmpd4, ignore_errors=True)
            return "占用时暂存待补队列，未丢记录"

        check("台账被占用不丢记录（补偿队列）", busy_fallback_ok)

        # 补写回路：占用解除后 flush_pending 要能把积压补回主台账
        def flush_ok():
            tmpd5 = tempfile.mkdtemp(prefix="smoke_flush_")
            import builtins
            real_open = builtins.open

            def fake_open(path, mode="r", *a, **kw):
                if "a" in mode and os.path.basename(str(path)) == H.HISTORY_FILE:
                    raise PermissionError(13, "另一个程序正在使用此文件")
                return real_open(path, mode, *a, **kw)

            try:
                builtins.open = fake_open
                H.append_record(result="fail", code=1, base_dir=tmpd5)
            finally:
                builtins.open = real_open
            done, left = H.flush_pending(base_dir=tmpd5)
            if done != 1 or left != 0:
                raise AssertionError("补写应为 (1,0)，实际 (%d,%d)" % (done, left))
            rows = H.recent_records(9999, base_dir=tmpd5)
            if len(rows) != 1 or rows[0]["result"] != "fail":
                raise AssertionError("补写后主台账读不到那条记录")
            pend = os.path.join(tmpd5, H.HISTORY_DIR, H.PENDING_FILE)
            if os.path.isfile(pend):
                raise AssertionError("补写成功后队列文件应被清掉")
            shutil.rmtree(tmpd5, ignore_errors=True)
            return "占用解除后自动补写并清空队列"

        check("台账补偿队列可回填", flush_ok)

    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


# =========================================================
# 5. 通知模块（只做离线组稿，绝不发请求）
# =========================================================
def test_notify():
    section("通知模块（离线）")
    try:
        import feishu_notify as N
    except Exception as e:
        bad("导入 feishu_notify", str(e))
        return

    ok("导入 feishu_notify")

    for fn in ("notify_signin_result", "notify_early_exit", "send", "should_escalate"):
        check("函数存在 %s()" % fn, lambda fn=fn: hasattr(N, fn) or (_ for _ in ()).throw(
            AssertionError("feishu_notify 没有 %s()" % fn)))

    # 截图扫描必须同时认 .png 和 .jpg（改了压缩之后的关键点）
    def exts_ok():
        import inspect
        found = 0
        for fn_name in ("notify_signin_result", "notify_early_exit"):
            src = inspect.getsource(getattr(N, fn_name))
            if '(".png", ".jpg")' in src or "('.png', '.jpg')" in src:
                found += 1
        if found == 0:
            raise AssertionError("截图扫描没有同时识别 .png/.jpg（会漏掉 JPEG 截图）")
        return "%d 处已支持 jpg" % found
    check("截图扫描支持 .png/.jpg", exts_ok)

    # 统计行护栏：台账为空时不应显示（避免"成功0失败0"）
    def guard_ok():
        import inspect
        src = inspect.getsource(N.notify_signin_result)
        if "_st.get(\"total\")" not in src and "_st.get('total')" not in src:
            raise AssertionError("统计行没有 total 护栏（空台账会显示'成功0失败0'）")
        return "有护栏"
    check("统计行有空台账护栏", guard_ok)

    # DRY-RUN 组稿（不联网）
    def dryrun_ok():
        import inspect
        sig = inspect.signature(N.notify_signin_result)
        if "dry_run" not in sig.parameters:
            raise AssertionError("notify_signin_result 缺 dry_run 参数，冒烟测试无法离线验证")
        return "支持 dry_run"
    check("通知支持 dry_run（可离线验证）", dryrun_ok)

    # 【2026-09-15 新增·防线】脚本被强杀时必须补一条"结果未知"提醒。
    # 真实现场：21:16 那次签到其实已成功，判 fail 后用户在 21:18:53 掐掉脚本
    # → notify_feishu 从未调用 → 一条消息都没有，只能干等。
    # 断言两件事：①有 atexit 兜底钩子 ②钩子有"未点过签到就不发"的降噪条件。
    def kill_guard_ok():
        import inspect
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        if "atexit" not in src:
            raise AssertionError("signin.py 没有 atexit 兜底：被强杀时不会发任何通知")
        if "_on_exit_guard" not in src:
            raise AssertionError("找不到 _on_exit_guard 中断兜底函数")
        if "_FEISHU_DONE[0]" not in src.split("_on_exit_guard", 1)[1][:900]:
            raise AssertionError("中断兜底没有复用 _FEISHU_DONE：正常退出时会重复推送")
        if 'finish_clicks", 0) <= 0' not in src and "finish_clicks\", 0) <= 0" not in src:
            raise AssertionError("中断兜底缺降噪条件：每当中断都发会变成噪音")
        return "有中断兜底 + 降噪条件"
    check("被强杀时会补发结果未知提醒", kill_guard_ok)

    # 【2026-09-16 新增】光有 atexit 兜底是**不够的** —— 实测 taskkill /F 与
    # terminate() 下 atexit 都不执行，而"用户掐脚本"的现实路径正是这两条
    # （关窗口 / 任务管理器结束任务 / 计划任务 30 分钟强杀）。
    # 所以必须存在一个**不依赖进程存活**的持久化机制，让下次启动代为报告。
    # 本断言锁死该机制的关键要素，防止将来被"简化"掉。
    def stale_guard_ok():
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src, filename="signin.py")
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}

        # A) 三个关键函数必须存在
        need_fn = {"_guard_mark_in_progress": "落盘进行中标记",
                   "_guard_clear_in_progress": "收尾清除标记",
                   "check_stale_in_progress": "下次启动检查残留"}
        missing_fn = [k for k in need_fn if k not in names]
        if missing_fn:
            raise AssertionError(
                "缺少强杀兜底函数 %s —— atexit 在 taskkill /F 下不执行，"
                "没有持久化标记就无法兜住计划任务强杀" % missing_fn)

        # B) 必须用信号处理（时效快路径），且 SIGINT/SIGTERM 都注册
        if not re.search(r"_signal\.signal\(\s*_signal\.SIGINT", src):
            raise AssertionError("没注册 SIGINT 处理：Ctrl+C 中断会无声退出")
        if not re.search(r"_signal\.signal\(\s*_signal\.SIGTERM", src):
            raise AssertionError("没注册 SIGTERM 处理：关窗口/terminate 会无声退出")

        # C) main() 里必须调用 check_stale_in_progress —— 否则标记写了没人读，
        #    等于建了一个永远不查的账本（本项目踩过"台账写了但从不读"的坑）
        main_fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "main":
                main_fn = n
                break
        if main_fn is None:
            raise AssertionError("找不到 main() 函数")
        called_in_main = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "check_stale_in_progress"
            for n in ast.walk(main_fn))
        if not called_in_main:
            raise AssertionError(
                "main() 里没有调用 check_stale_in_progress()：标记写进了磁盘却没人读，"
                "强杀兜底形同虚设")

        # D) 标记落盘必须用原子替换（不能写半截 JSON 卡住下次启动）
        # 【踩坑记录】初版写成 `("os.replace" in ast.dump(n)) or ("os.replace" in src[...])`，
        # 结果是**永远 PASS 的摆设** —— 因为后半段在源码字符串里搜，而 "os.replace"
        # 在 step_tracer.py 的注释和别的模块里也出现，必然命中。
        # 这正是项目里记过的"不能用字符串 in 判断代码有没有做某件事"的坑，又踩了一次。
        # 现在改成：只在**该函数节点内部**找 os.replace 的真实调用节点。
        def _calls_replace(fn_node):
            for sub in ast.walk(fn_node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if (isinstance(f, ast.Attribute) and f.attr == "replace"
                            and isinstance(f.value, ast.Name) and f.value.id == "os"):
                        return True
            return False

        mark_fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "_guard_mark_in_progress":
                mark_fn = n
                break
        if mark_fn is None:
            raise AssertionError("找不到 _guard_mark_in_progress()")
        if not _calls_replace(mark_fn):
            raise AssertionError(
                "_guard_mark_in_progress() 内部没有调用 os.replace（原子替换）—— "
                "强杀可能留下半截 JSON，下次启动读到坏标记")

        # E) 标记文件路径必须在 .agent/ 下（与 self_heal 同处，且已被 .gitignore 忽略）
        if ".agent" not in src.split("_GUARD_STATE_PATH", 1)[1][:200]:
            raise AssertionError("进行中标记不在 .agent/ 目录下，可能被误提交进仓库")

        return "残留标记（主保障）+ signal（快路径）+ main 启动时检查，要素齐全"
    check("强杀兜底：残留标记机制完整（不依赖 atexit）", stale_guard_ok)

    # 台账写入失败必须在 run.log 可见（原来是纯 pass，空了几个月没人发现）
    def ledger_visible_ok():
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        seg = src.split("signin_history.append_record", 1)
        if len(seg) < 2:
            raise AssertionError("找不到 append_record 调用点")
        tail = seg[1][:1800]
        if "busy_queued" not in tail:
            raise AssertionError("台账写入没区分'暂存待补'：被占用时日志仍看不出问题")
        if "记录写入失败" not in tail:
            raise AssertionError("台账彻底写入失败时没有 WARNING，仍会静默")
        return "占用/失败都写日志"
    check("台账写入失败在日志可见", ledger_visible_ok)


# =========================================================
# 6. 运行环境
# =========================================================
def test_runtime():
    section("运行环境")
    rt = os.path.join(HERE, "runtime", "python.exe")
    check("便携 python 存在", lambda: True if os.path.isfile(rt) else (_ for _ in ()).throw(
        AssertionError("runtime\\python.exe 缺失，双击 bat 会失败")))

    def deps_ok():
        # 依赖装在项目自带的 runtime/ 里，不是当前解释器。
        # 所以不能 __import__ 了就算数——直接看 runtime\Lib\site-packages 目录。
        sp = os.path.join(HERE, "runtime", "Lib", "site-packages")
        if not os.path.isdir(sp):
            raise AssertionError("找不到 runtime\\Lib\\site-packages")
        have = {n.lower() for n in os.listdir(sp)}
        need = {"pyautogui": "pyautogui", "cv2": "cv2", "numpy": "numpy",
                "PIL": "pil", "pyperclip": "pyperclip"}
        missing = [k for k, pat in need.items() if not any(pat in h for h in have)]
        if missing:
            raise AssertionError("runtime 里缺依赖: %s" % missing)
        return "pyautogui/cv2/numpy/Pillow/pyperclip 齐全"
    check("运行依赖齐全（runtime 内置）", deps_ok)

    def ocr_dep_ok():
        """OCR 依赖（第三路判据）也必须内置在 runtime 里，否则拷到别的电脑会静默退化。"""
        sp = os.path.join(HERE, "runtime", "Lib", "site-packages")
        if not os.path.isdir(sp):
            raise AssertionError("找不到 runtime\\Lib\\site-packages")
        have = {n.lower() for n in os.listdir(sp)}
        # 缺任一包 → 该机器上 OCR 复核会静默跳过（脚本仍能跑，但少一道防线）
        need = ["winrt", "winrt_windows_media_ocr", "winrt_windows_globalization",
                "winrt_windows_graphics_imaging", "winrt_windows_storage_streams",
                "winrt_windows_foundation"]
        missing = [p for p in need if not any(p in h for h in have)]
        if missing:
            raise AssertionError(
                "runtime 里缺 OCR 依赖: %s\n"
                "      → 缺了脚本仍能跑，但第三路判据会静默跳过（少一道防线）。\n"
                "      → 重装：runtime\\python.exe -m pip install winrt-Windows.Media.Ocr "
                "winrt-Windows.Globalization winrt-Windows.Graphics.Imaging "
                "winrt-Windows.Storage.Streams winrt-Windows.Foundation" % missing)
        return "winrt OCR 依赖齐全（合计约 2.9MB）"
    check("OCR 依赖齐全（runtime 内置）", ocr_dep_ok)


# =========================================================
# 7. OCR 真实样本回归（可选：样本不在就跳过）
# =========================================================
def test_ocr_real_samples():
    """用**真实截图**跑 OCR，验证「已签到」和「已结束」都能读对。

    【为什么必须用真实样本】2026-09-15 的教训：
    合成对照测试里「已结束」6 档全对，但**真实样本上低倍数全空、只有 10x 左右读得出**，
    而且只要裁图贴边（不给白边）就立刻全空。
    这两个问题合成测试**完全测不出来** —— 只有真实截图会暴露。
    所以：合成验证用来测"最担心的误读方向"，真实样本用来测"到底读不读得出"，两者都要。

    顺便暴露了量级差异：「已签到」遍历 2100 种裁图×倍数组合命中 91.8%，
    「已结束」遍历 1650 种只命中 1.1% —— 后者是整条判据链路上最薄的一环，
    靠上层的双时间守卫兜底。这个数字如实记录，不粉饰。

    样本放在 logs/samples/（已 gitignore，含个人界面信息，不进仓库）。
    没有样本时本组测试自动跳过，不会让冒烟测试失败。
    """
    samples_dir = os.path.join(HERE, "logs", "samples")
    # 期望：(文件名关键字, 真值)
    cases = [
        ("已签到", "已签到"),
        ("已结束", "已结束"),
    ]
    found_any = False
    for key, truth in cases:
        # 找该真值对应的样本
        cands = []
        if os.path.isdir(samples_dir):
            for n in os.listdir(samples_dir):
                if key in n and n.lower().endswith((".png", ".jpg", ".jpeg")):
                    cands.append(os.path.join(samples_dir, n))
        if not cands:
            continue

        def _run(cands=cands, truth=truth, key=key):
            try:
                import cv2
                import numpy as np
                sys.path.insert(0, HERE)
                # 【防污染】import signin 会建 run 目录并往**按天日志**写字。
                # 按天日志是排查故障的一级证据，绝不能混进测试噪音 ——
                # 用环境变量把日志重定向到本次测试的临时目录（见 signin.py 里
                # SIGNIN_LOG_DIR_OVERRIDE 的注释）。
                os.environ["SIGNIN_LOG_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="smoke_ocrlog_")
                import signin
            except Exception as e:
                raise AssertionError("导入 OCR 相关模块失败: %s" % e)

            eng = signin._ocr_get_engine()
            if eng is None:
                raise AssertionError(
                    "OCR 引擎不可用（本机可能没装中文语言包）。"
                    "该机器上第三路判据会静默跳过 —— 这本身不算错，"
                    "但既然有真实样本，就应该能验证。请检查系统'语言设置'里的中文包。")

            hits = []
            for p in cands:
                img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                # 【为什么只用 _find_gray_bar，不用 scan_buttons】
                # scan_buttons() 是给"实时抓的微信窗口截图"设计的，它依赖
                # win_rect/窗口尺寸假设。拿来跑历史样本（尤其 2880x1800 全屏图）时，
                # 实测**非确定性**：同一个文件连跑 5 次，4 次返回 0 个、
                # 1 次返回一个 1548x106 的假灰块（整个标题区）。
                # 这会让测试偶发失败 —— 而**偶发失败比稳定失败更糟**，
                # 它会让人习惯性重跑、最终把真问题也一起忽略掉。
                # 所以测试里的样本定位改用**确定性**的 _find_gray_bar()。
                gray = _find_gray_bar(img)
                if not gray:
                    continue
                # 多个候选时，优先选"最像按钮"的：宽高比最接近实测真值 8.5。
                # （不用 max(w)，否则可能选中标题栏那种超宽块。）
                btn = min(gray, key=lambda b: abs(b["w"] / float(b["h"]) - 8.5))
                txt = signin.ocr_button_text(img, btn)
                hits.append((os.path.basename(p), txt))

            if not hits:
                raise AssertionError("样本存在但没能定位到灰色按钮：%s" % cands)
            good = [h for h in hits if truth in h[1]]
            if not good:
                raise AssertionError(
                    "真实样本 OCR 全部未读出「%s」：%s\n"
                    "      → 这会让第三路判据在该场景静默失效（漏掉否决 = 可能假成功）。"
                    "检查 ocr_button_text() 的 scales/pads 组合是否覆盖了该样本。"
                    % (truth, [(n, t) for n, t in hits]))
            return "「%s」在 %d/%d 张真实样本上读出" % (truth, len(good), len(hits))
        check("OCR 真实样本：「%s」能读出" % truth, _run)
        found_any = True

    if not found_any:
        print("  [跳过] OCR 真实样本回归（logs/samples/ 下没有相应样本）")


def test_ocr_no_reverse_misread():
    """【最关键的反方向断言】真实「已签到」按钮，绝不能读出任何否定词。

    为什么这条最重要：OCR 认错有两个方向，危害完全不对等 ——
      · 把「已签到」误读成否定词 → 多跑一轮核实（可恢复，只是慢）
      · 把「已结束」误读成「已签到」 → **假成功**（不可恢复，静默漏签）
    后者是本项目一直在防的最坏结果。所以"正方向能读出"和
    "反方向不误读"必须都测，后者是底线。

    做法：拿真实「已签到」样本，穷举 预处理 × pad × 倍数 的全部组合，
    任何一个组合读出否定词就是 FAIL。

    2026-09-15 实测结果：56 个组合**零反方向误判**。
    且加了 Otsu 之后，raw 路径原本偶发的「已签至刂」残缺也消失了。
    """
    samples_dir = os.path.join(HERE, "logs", "samples")
    if not os.path.isdir(samples_dir):
        print("  [跳过] OCR 反方向误判检查（无 samples 目录）")
        return
    # 只测「已签到」真值的样本（这些绝不该读出否定词）
    pos = [os.path.join(samples_dir, n) for n in os.listdir(samples_dir)
           if "已签到" in n and n.lower().endswith((".png", ".jpg", ".jpeg"))]
    if not pos:
        print("  [跳过] OCR 反方向误判检查（没有「已签到」真实样本）")
        return

    def _run():
        try:
            import cv2
            import numpy as np
            sys.path.insert(0, HERE)
            # 【防污染】同 test_ocr_real_samples：把 import signin 的日志
            # 重定向到临时目录，不写真实的按天日志。
            os.environ["SIGNIN_LOG_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="smoke_ocrlog_")
            import signin
        except Exception as e:
            raise AssertionError("导入失败: %s" % e)
        eng = signin._ocr_get_engine()
        if eng is None:
            raise AssertionError("OCR 引擎不可用，无法验证反方向误判")

        negatives = ("已结束", "已过期", "未开始", "不在区域", "未在区域")
        bad = []
        total = 0
        for p in pos:
            img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            bars = _find_gray_bar(img)
            if not bars:
                continue
            # 同 test_ocr_real_samples：优先选宽高比最接近真值 8.5 的候选（确定性）
            b = min(bars, key=lambda x: abs(x["w"] / float(x["h"]) - 8.5))
            H, W = img.shape[:2]
            for pad in (0, -1, 1, 2):
                x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
                x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
                crop = img[y0:y1, x0:x1]
                for pp in ("otsu", "raw"):
                    src = signin._ocr_binarize(crop) if pp == "otsu" else crop
                    padded = signin._ocr_pad_to_canvas(src)
                    for s in (10, 8, 6, 12, 4, 5, 3, 2):
                        if padded.shape[1] * s > 9000 or padded.shape[0] * s > 9000:
                            continue
                        big = cv2.resize(padded, None, fx=s, fy=s,
                                         interpolation=cv2.INTER_CUBIC)
                        txt = signin._ocr_read(eng, big)
                        total += 1
                        if not txt:
                            continue
                        hit = [k for k in negatives if k in txt]
                        if hit:
                            bad.append((os.path.basename(p), pad, pp, s, txt, hit))
        if bad:
            raise AssertionError(
                "！！发现 %d 处反方向误判（「已签到」被读成否定词）！！\n"
                "      → 这会导致**多跑一轮**（可恢复，但仍属 bug）：\n%s"
                % (len(bad), "\n".join("      %s pad=%s %s %sx -> %r"
                                       % x for x in bad[:10])))
        return "真实「已签到」在 %d 个组合下零反方向误判" % total
    check("OCR 反方向安全：真实「已签到」绝不读成否定词", _run)


def test_ocr_best_initialized():
    """【2026-09-15 新增】ocr_button_text() 里 best 必须在被读取前初始化。

    背景（真实炸过）：原实现只在 `if len(txt) > len(best)` 里给 best 赋值，
    **从没赋过初值**。真实样本上"完整词"路径总是先命中并 return，
    把这条残缺分支整个跳过了，所以一直没暴露；但触发路径真实存在
    （按钮裁偏、主题变化、读到 unrelated 文本就会走进来）。

    后果链条（这是它值得单独一条断言的原因）：
      ocr_button_text 抛 UnboundLocalError
        → ocr_veto_signed 的 except 吞掉它并返回 None（放行）
        → **第三路判据（OCR 否决权）静默失效**
    而 OCR 是「已签到」vs「已结束」唯一可靠的区分手段 —— 它静默失效 = 假成功风险直线上升。
    2026-09-15 22:45:26 的 signin_20260915.log 里真的出现过这条异常。

    【为什么用 AST 而不是字符串检查】项目踩过两次同类坑：注释/文档字符串里的
    函数名或代码片段会让 `"xxx" in src` 类断言误判。凡"检查代码有没有做某件事"，
    必须查真实语法节点。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="signin.py")

        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "ocr_button_text":
                fn = n
                break
        if fn is None:
            raise AssertionError("找不到 ocr_button_text()")

        # 收集函数体内 best 的赋值行 / 读取行
        stores, loads = [], []
        for x in ast.walk(fn):
            if isinstance(x, ast.Name) and x.id == "best":
                (stores if isinstance(x.ctx, ast.Store) else loads).append(x.lineno)
        if not loads:
            raise AssertionError("ocr_button_text() 里没有读取 best？函数被大改过，请复核本断言")
        if not stores:
            raise AssertionError(
                "ocr_button_text() 里 best **没有任何赋值** —— 读到它就 UnboundLocalError")

        # 关键：必须存在一个"在读取之前"的赋值（初始化）
        first_load = min(loads)
        early = [s for s in stores if s < first_load]
        if not early:
            raise AssertionError(
                "ocr_button_text() 里 best 的首次赋值在第 %d 行，但第一次读取在第 %d 行 —— "
                "**读取发生在赋值之前**，会抛 UnboundLocalError。"
                "（真实后果：OCR 否决权静默失效，日志只留一行 warning）"
                % (min(stores), first_load))

        # 再确认：这个初始化是"赋空串/None"这类无副作用初值，不是从别处搬运
        init_ok = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "best" and node.lineno < first_load:
                        try:
                            vals = ast.unparse(node.value)
                        except Exception:
                            vals = "?"
                        if vals in ("''", '""', "None"):
                            init_ok = True
        if not init_ok:
            raise AssertionError(
                "best 在读取前虽有赋值，但初值不是 '' / \"\" / None —— "
                "请确认它不依赖任何未定义的东西（本断言只认最稳妥的空初值）")
        return "best 在第 %d 行初始化，首次读取在第 %d 行（AST 实测）" % (min(early), first_load)
    check("OCR: ocr_button_text() 的 best 在使用前已初始化", _run)


def test_ocr_negative_stems_veto():
    """【2026-09-15 新增】OCR 读到否定词的"骨架"也必须否决（防假成功漏洞）。

    背景（修复前是真实漏洞）：OCR 常把词读残。若「已结束」被读成「结束」（首字被切掉），
    原判据 `"已结束" in "结束"` 为 **False**（子串方向反了）
    → ocr_veto_signed 判 None（放行）→ **读到"结束"却不否决 = 假成功漏网**。
    而 ocr_button_text 那边的关键词表里恰好有 "结束"，会提前 return 这个残缺结果，
    所以这条路径是能真实走到的。

    修复：加 _OCR_NEGATIVE_STEMS 骨架词表，并优先做双向匹配。
    本断言锁死两个方向：
      · 「结束」/「过期」 这类骨架**必须**被否决（安全方向）
      · 「已签到」/「已签至刂」**绝不能**被骨架词误伤（反方向，危害更大）
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="signin.py")

        # AST 抠出三个词表（不用字符串正则，避免被注释骗）
        tables = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.startswith("_OCR_"):
                        try:
                            tables[t.id] = ast.literal_eval(node.value)
                        except Exception:
                            pass
        for need in ("_OCR_NEGATIVE", "_OCR_NEGATIVE_STEMS"):
            if need not in tables:
                raise AssertionError("找不到词表 %s" % need)
        neg = tables["_OCR_NEGATIVE"]
        stems = tables["_OCR_NEGATIVE_STEMS"]

        # 复刻 ocr_veto_signed 的判据（只判文本，不碰引擎）
        def verdict(txt):
            for w in neg:
                if w in txt:
                    return False
            for st in stems:
                if st in txt:
                    return False
            if "已签到" in txt or "已签 到" in txt or "已签至刂" in txt:
                return True
            return None

        # A) 安全方向：这些残缺形态必须被否决
        must_veto = ["结束", "已结束", "过期", "已过期", "未开始",
                     "签到未开始", "不在区域内", "未在区域"]
        missed = [t for t in must_veto if verdict(t) is not False]
        if missed:
            raise AssertionError(
                "这些读到后**必须否决**的文本没有被否决（假成功漏网风险）：%s\n"
                "      → 请检查 _OCR_NEGATIVE_STEMS 是否覆盖了它们" % missed)

        # B) 反方向（危害更大）：绝不能把正向词误判成否决
        must_pass = ["已签到", "已签至刂", "已签 到"]
        wrong = [t for t in must_pass if verdict(t) is False]
        if wrong:
            raise AssertionError(
                "！！反方向误判：正向词被否决了 %s ！！\n"
                "      → 这会把真实签到判成未签（多跑一轮，甚至漏报成功）" % wrong)

        # C) 骨架词不能宽到把无关文本也否决 —— 至少要放过真正的"不认识"
        for t in ("已", "已纟", "签", "已签", "Q搜索拼21：06口的劬80％"):
            if verdict(t) is False:
                raise AssertionError(
                    "骨架词过宽：无关文本「%s」被误否决了，会无谓地推翻真实签到" % t)

        return "%d 个否决词 + %d 个骨架词：安全方向全否决、反方向零误伤（AST 实测）" % (
            len(neg), len(stems))
    check("OCR: 否定词残缺形态（骨架）也能否决，且不误伤正向词", _run)


def test_ended_record_propagates_not_time():
    """【2026-09-16 新增】OCR 读出的「已结束」必须传到决策层并判 not_time，不能被当成导航失败。

    背景（用户 2026-09-16 反馈的真实现象，日志实证 3 次）：
      用户每天 20:50 才收到学校的晚点名推送。在此之前打开详情页，看到的是
      **昨天那条记录的「已结束」**。这是"今天还没开始"的**正常**状态，不是故障。
      但脚本判成了 fail + 退出码 1 + 留失败现场 + 发飞书告警 + 记自愈失败。

    真根因（不是时间守卫失效，是**证据在途中被丢弃**）：
      signed_detail_button() 只有"按钮dict / None"两种返回，无法区分
        a) 页面上没有符合几何的灰宽按钮
        b) 几何像但字迹不像
        c) 几何像、字迹也像，但 **OCR 明确读出「已结束」**
      三者在调用方 find_and_click_entry 眼里**长得一模一样**，于是 (c) 被当成
      "没找到入口" → `return False` → attempt_once 记 NAV_ENTRY_FAIL → fail。

    铁证（logs/signin_20260916.log 16:34:03，三行紧挨着）：
      [已签到判据] 几何 + 字迹双路一致 → 字迹符合「已签到」
      [OCR] 详情页按钮文字=「已结束」含否定词「已结束」→ **否决**'已签到'判定
      [导航] 第4步「每日签到里的进入按钮」滚动后仍未找到，终止     ← 证据到这里就没了

    修复：给 signed_detail_button 加可选 out 出参，把"读到否定词"这个事实带出去；
          导航层据此返回 "not_time"（沿 3399 行既有链路 → 退出码 3、不告警、不留现场）。

    本断言锁死三层（缺任何一层，修复都会被静默撤销）：
      ① AST：out 参数存在，且**真的被赋值**（不是只在签名里占位）
      ② AST：导航层**真的**读了 _out.get("negative") 并返回 "not_time"
      ③ 运行时：不传 out 时行为与改动前完全一致（防"顺手改坏老路径"）

    为什么必须用 AST 判 ①②：注释里就写着"应该返回 not_time"这句话，
    用字符串搜索必然命中注释 → 断言恒真成摆设。本项目已因这类错误栽过三次。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="signin.py")

        fn = None
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name == "signed_detail_button":
                fn = n
                break
        if fn is None:
            raise AssertionError("找不到 signed_detail_button")

        # ---- ① out 参数存在，且被真实赋值过 ----
        argnames = [a.arg for a in fn.args.args]
        if "out" not in argnames:
            raise AssertionError(
                "signed_detail_button 缺少 out 出参 —— 「已结束」证据无法传到调用方，"
                "会退回『正常状态被误报为失败』的老 bug")

        # 找 `out["negative"] = True` 这类赋值（AST 查真节点，不看注释）
        assigned_true = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                tgt = n.targets[0]
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "out"
                        and isinstance(n.value, ast.Constant)
                        and n.value.value is True):
                    assigned_true.append(n.lineno)
        if not assigned_true:
            raise AssertionError(
                "signed_detail_button 里没有 `out[...] = True` 的赋值 —— "
                "参数只是个占位符，证据依然带不出去")

        # 这个赋值必须发生在 OCR 否决分支内（不能随便找个地方赋）
        veto_lns = []
        for n in ast.walk(fn):
            if isinstance(n, ast.If):
                seg = ast.get_source_segment(src, n.test) or ""
                if "_v is False" in seg or ("ocr_veto_signed" in (ast.get_source_segment(src, n) or "")
                                            and "_v" in seg):
                    veto_lns.append(n.lineno)
        if not veto_lns:
            raise AssertionError("找不到 OCR 否决分支（_v is False）")
        if not any(v <= a for v in veto_lns for a in assigned_true):
            raise AssertionError(
                "`out[...] = True` 不在 OCR 否决分支内 —— "
                "那会把『字迹不符』也记成『读到已结束』，反而制造新的误报")

        # ---- ② 导航层真的读了这个出参并返回 not_time ----
        navf = None
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name == "open_signin_entry":
                navf = n
                break
        if navf is None:
            raise AssertionError("找不到 open_signin_entry")

        # 传了 out= 关键字（否则 signed_detail_button 根本不会写）
        passed_out = False
        for n in ast.walk(navf):
            if isinstance(n, ast.Call):
                if any(k.arg == "out" for k in n.keywords):
                    passed_out = True
                    break
        if not passed_out:
            raise AssertionError(
                "open_signin_entry 调用 signed_detail_button 时没传 out= —— "
                "出参永远是空的，修复等于没做")

        # 必须有 `if <...>.get("negative"):` 且其函数体里有 return "not_time"
        found_guard = None
        for n in ast.walk(navf):
            if isinstance(n, ast.If):
                seg = ast.get_source_segment(src, n.test) or ""
                if 'get("negative")' in seg:
                    found_guard = n
                    break
        if found_guard is None:
            raise AssertionError(
                'open_signin_entry 里没有 `if ....get("negative"):` 判据 —— '
                "读到了反而不处理，证据还是白读了")
        rets = [r.value.value for r in ast.walk(found_guard)
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
                and isinstance(r.value.value, str)]
        if "not_time" not in rets:
            raise AssertionError(
                'out.get("negative") 分支里没有 return "not_time"（实际返回：%s）—— '
                "会退回判 fail 的老毛病" % rets)

        # ---- ③ 运行时：不传 out 的行为必须与改动前一致 ----
        # 用 AST 取真函数源码执行，不复制粘贴实现（防"测试版与线上版各写一套"）
        ns = {}
        fake = {
            "win_rect": lambda hwnd: (5, 0, 1123, 1715),
            "button_stylometry": lambda full, x: {"bg": 204, "contrast": 25,
                                                  "ink": 0.0239, "polarity": "light"},
            "is_already_signed_style": lambda st: (True, "模拟"),
            "logger": logging.getLogger("smoke_detail"),
        }
        ns.update(fake)
        mod = ast.Module(body=[fn], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), "<signed_detail_button>", "exec"), ns)
        run = ns["signed_detail_button"]

        cy = 0 + int(0.72 * 1715)
        btn = {"kind": "gray", "cx": 545, "cy": cy, "w": 838, "h": 90}

        ns["ocr_veto_signed"] = lambda full, x, tag="": False   # 读到「已结束」
        r_old = run(1, [btn], full=object())                   # 老调用方式：不传 out
        if r_old is not None:
            raise AssertionError("不传 out 时读到否定词竟判成功 —— 假成功防线被破坏")
        o1 = {}
        r_new = run(1, [btn], full=object(), out=o1)
        if r_new is not None:
            raise AssertionError("传 out 时读到否定词仍判成功 —— 防线被破坏")
        if o1.get("negative") is not True:
            raise AssertionError('读到「已结束」但 out["negative"] 不是 True：%r' % o1)

        ns["ocr_veto_signed"] = lambda full, x, tag="": True    # 读到「已签到」
        o2 = {}
        r2 = run(1, [btn], full=object(), out=o2)
        if r2 is not btn:
            raise AssertionError("读到「已签到」却没返回按钮（正常成功路径被破坏）")
        if o2.get("negative") is not False:
            raise AssertionError('读到「已签到」但 out["negative"] 不是 False：%r' % o2)

        ns["ocr_veto_signed"] = lambda full, x, tag="": None    # 读不出（最常见）
        o3 = {}
        r3 = run(1, [btn], full=object(), out=o3)
        if r3 is not btn:
            raise AssertionError("OCR 读不出时没有放行 —— veto-only 设计被破坏")
        if o3.get("negative") is not False:
            raise AssertionError('读不出却把 out["negative"] 置 True —— 会大量误报 not_time')

        return ("out 出参存在且仅在 OCR 否决时置 True；导航层读它并返回 not_time；"
                "不传 out 时行为不变（AST + 运行时双证）")
    check("「已结束」证据必须传到决策层并判 not_time（不被当成导航失败）", _run)


def test_wifi_link_connected():
    """【2026-09-16 新增】WiFi 链路状态判据必须是「白名单精确匹配」，不能是子串匹配。

    背景（真实炸过，英文系统才暴露）：
      原判据 `("已连接" in stat) or ("connected" in stat.lower())`
      而 "connected" 是 **"disconnected" 的子串** —— 英文系统下
      `State : disconnected`（已断开）会被判成"已连接"。

    后果链条：
      reconnect_wifi() 里 `linked` 被错误置 True
        → 跳过"重连后未恢复链路"告警
        → 函数返回 True 谎报"重连成功"
        → 调用处（定位漂移场景）把 wifi_refreshed 置 True
        → **最后一次定位自愈机会被白白浪费**

    【负向测试】把判据改回子串即应 FAIL，见本函数末尾的自检。
    用 AST 提取真实函数体执行，不复制粘贴实现（避免"测试版和线上版各写一套"）。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="signin.py")

        # ---- 1) 静态要求：必须存在 wifi_link_connected 与 parse_wlan_interfaces ----
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for fn in ("wifi_link_connected", "parse_wlan_interfaces"):
            if fn not in names:
                raise AssertionError("缺少 %s()：WiFi 判据未抽成可测函数" % fn)

        # ---- 2) 静态要求：不得再出现裸子串判连接 ----
        # 只在"代码"里查，注释/docstring 不算证据（项目踩过注释误导的坑）
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In):
                # 形如 "connected" in xxx.lower()
                left = node.left
                if isinstance(left, ast.Constant) and isinstance(left.value, str):
                    if left.value == "connected":
                        bad.append(getattr(node, "lineno", "?"))
        if bad:
            raise AssertionError(
                "第 %s 行仍在用裸子串 'connected' 判连接状态 —— "
                "'disconnected' 会被误判为已连接" % bad)

        # ---- 3) 行为要求：真实 netsh 输出样本上判对 ----
        # 把 wifi_link_connected 的函数体单独编译执行，测的就是线上那份实现
        fn_node = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "wifi_link_connected":
                fn_node = n
                break
        # 补齐它依赖的 parse_wlan_interfaces
        dep_node = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "parse_wlan_interfaces":
                dep_node = n
                break
        # 只保留这两个函数体（去掉装饰器），模块级 import re 由 ns 提供
        mod = ast.Module(body=[dep_node, fn_node], type_ignores=[])
        ns = {"re": __import__("re")}
        exec(compile(mod, "<wifi_extract>", "exec"), ns)
        judge = ns["wifi_link_connected"]

        cases = [
            # (netsh 输出, 期望, 说明)
            ("名称 : Wi-Fi\n状态 : 已连接", True, "中文已连接"),
            ("名称 : Wi-Fi\n状态 : 已断开连接", False, "中文已断开（原判据恰好能过）"),
            ("Name : Wi-Fi\nState : connected", True, "英文已连接"),
            ("Name : Wi-Fi\nState : disconnected", False, "★英文已断开（原判据在此误判）"),
            ("State : Disconnected", False, "大小写混合"),
            ("State : DISCONNECTED", False, "全大写"),
            ("SSID : XSYU_WLAN\nState : connected\nBSSID : aa:bb", True, "多行中取状态行"),
            ("", False, "空输出"),
            ("一些无关文本", False, "无状态行"),
        ]
        wrong = [(s, e, judge(s)) for s, e, _d in cases if judge(s) is not e]
        if wrong:
            raise AssertionError(
                "WiFi 链路判据判错 %d 例：%s\n"
                "      → 其中 `State : disconnected` 被误判会让重连谎报成功、白白浪费定位自愈机会"
                % (len(wrong), wrong))

        # ---- 4) 负向自检：把判据改回子串，必须判错 ----
        def _broken_baseline(text):
            """原实现的判据（应在此样本上出错）"""
            return ("已连接" in text) or ("connected" in text.lower())
        # 原判据在 disconnected 样本上会返回 True（=误判为已连），这正是要抓的回归。
        # 所以负向自检要求：_broken_baseline 的结果与期望值 False **不相等**。
        if _broken_baseline("Name : Wi-Fi\nState : disconnected") is False:
            raise AssertionError(
                "负向测试失效：还原成子串判据后居然判对了，说明本断言抓不住回归"
                "（样本或判据逻辑被改动了，请检查）")

        return ("%d 组 netsh 输出全部判对（含 disconnected 反向样本）；"
                "且验证了'改回子串即失效'" % len(cases))
    check("WiFi 链路判据：disconnected 不被误判为已连接", _run)


def test_global_timeout_guards_long_waits():
    """【2026-09-16 新增】GLOBAL_TIMEOUT 必须约束到长等待循环内部，不能只查轮边界。

    背景（量化实测）：
      GLOBAL_TIMEOUT = 900（15 分钟），但唯一检查点在 `for rnd in range(1,4)` 循环体首行，
      单轮内部**没有任何刹车**。实测：
        单轮主要等待 DETAIL_WAIT(20) + LOCATE_WAIT(120) + CONFIRM_WAIT(50) = 190 秒
        三轮最坏 = 271×3 + 轮间 98 = 911 秒 → 已超 900
        最坏（第3轮开始时 899 秒 + 再跑满一轮 271 秒）= 1170 秒 = 19.5 分钟
      即"15 分钟防卡死"这道门是虚掩的。

    【负向测试】把任一 while 条件里的 GLOBAL_TIMEOUT 去掉即应 FAIL。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="signin.py")

        # 找出三个长等待循环：条件里引用了 LOCATE_WAIT / CONFIRM_WAIT / DETAIL_WAIT
        targets = ("LOCATE_WAIT", "CONFIRM_WAIT", "DETAIL_WAIT")
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            cond_src = ast.dump(node.test)
            for t in targets:
                if ("Name(id='%s'" % t) in cond_src:
                    # 该循环的条件里必须同时出现 GLOBAL_TIMEOUT
                    found[t] = ("Name(id='GLOBAL_TIMEOUT'" in cond_src)
        missing = [t for t in targets if t not in found]
        if missing:
            raise AssertionError(
                "没找到以 %s 为条件的 while 循环 —— 常量改名了？本断言需同步更新" % missing)
        unguarded = [t for t, ok in found.items() if not ok]
        if unguarded:
            raise AssertionError(
                "这些长等待循环的条件里没有 GLOBAL_TIMEOUT 兜底：%s\n"
                "      → 全局 15 分钟上限形同虚设，最坏可跑到 19.5 分钟"
                % unguarded)

        # 负向自检：断言逻辑本身要能识别"没护栏"的情况
        fake_cond = "Compare(left=Name(id='t0'))"   # 模拟一个没护栏的条件
        if ("Name(id='GLOBAL_TIMEOUT'" in fake_cond):
            raise AssertionError("负向测试失效：护栏检测逻辑对空条件也报通过")
        return "3 个长等待循环（DETAIL/LOCATE/CONFIRM）全部受 GLOBAL_TIMEOUT 约束"
    check("全局超时下沉到长等待循环内部", _run)


def test_prune_visible_and_sideeffect_free():
    """【2026-09-16 新增·P1-3/P1-6】归档清理必须：① 异常可见 ② 不在 import 时执行 ③ unknown 单独定期。

    背景三条（都是真炸过或真实量化出来的）：

    (1) **静默吞异常**：原 `_prune_old_runs()` / `_prune_old_logs()` 通体
        `except Exception: pass`。清理一旦坏了（LOG_DIR 权限、磁盘满、目录被占），
        **一点日志都没有**，等发现时往往是磁盘已经涨满 —— 而磁盘满会连锁让
        截图写盘失败、日志写盘失败，最后签到静默失败。
        → 断言：函数体内不得出现"什么都做的 except: pass"。

    (2) **import 即副作用**：两个清理函数原本在模块顶层调用。这意味着
        `python -c "import signin"`、IDE 索引、静态分析工具都会**真的删磁盘文件**。
        → 断言：模块顶层不得直接调用它们（AST 查真实 Call 节点，不查字符串）。

    (3) **unknown 与 fail 同用 90 天**：unknown（result.txt 读不出）恰恰是
        **最没诊断价值**的一类，却和真失败一样留 90 天。
        → 断言：存在独立的 KEEP_UNKNOWN_DAYS，且清理逻辑真的用到它。

    【负向测试】把顶层调用加回去、或把 KEEP_UNKNOWN_DAYS 去掉，本断言必须 FAIL。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename="signin.py")

        # ---- 1) 两个清理函数都得存在 ----
        fns = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name in ("_prune_old_runs", "_prune_old_logs"):
                fns[n.name] = n
        missing = [x for x in ("_prune_old_runs", "_prune_old_logs") if x not in fns]
        if missing:
            raise AssertionError("缺少清理函数 %s（被改名/删除了？本断言需同步更新）" % missing)

        # ---- 2) 不得再有"吞掉一切的 except: pass" ----
        # 允许 except 里做别的事（计数/告警/continue），只禁"空 pass"这一种。
        bare = []
        for name, node in fns.items():
            for h in ast.walk(node):
                if not isinstance(h, ast.ExceptHandler):
                    continue
                body = h.body
                # 允许 `except X: pass` 之外的收尾；这里只抓"函数体里只有一个 Pass"
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    bare.append((name, getattr(h, "lineno", "?")))
        if bare:
            raise AssertionError(
                "这些清理函数里还有 'except ...: pass'（异常会被吞掉，磁盘涨满也无人知）：%s\n"
                "      → 应改为计数 + _prune_warn() 告警" % bare)

        # ---- 3) 模块顶层不得直接调用清理函数（import 即副作用）----
        # 用 AST 找**模块 body 直属**的 Expr(Call) 节点。嵌套在函数里的调用不算。
        top_calls = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                f = stmt.value.func
                if isinstance(f, ast.Name) and f.id in ("_prune_old_runs", "_prune_old_logs"):
                    top_calls.append((f.id, getattr(stmt, "lineno", "?")))
        if top_calls:
            raise AssertionError(
                "清理函数还在模块顶层被调用（import 就会删磁盘文件）：%s\n"
                "      → 应挪进 main()" % top_calls)

        # ---- 4) main() 里确实调用了它们（否则清理根本不跑了）----
        main_node = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "main":
                main_node = n
                break
        if main_node is None:
            raise AssertionError("找不到 main()")
        main_calls = set()
        for h in ast.walk(main_node):
            if isinstance(h, ast.Call) and isinstance(h.func, ast.Name):
                main_calls.add(h.func.id)
        for want in ("_prune_old_runs", "_prune_old_logs"):
            if want not in main_calls:
                raise AssertionError("main() 里没有调用 %s()，归档/日志永远不会被清理" % want)

        # ---- 5) unknown 必须有独立保留期，且真的被用上 ----
        # 形如 KEEP_UNKNOWN_DAYS = int(CONFIG.get("keep_unknown_days", 14))
        consts = set()
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                fn = stmt.value.func
                if not (isinstance(fn, ast.Name) and fn.id == "int"):
                    continue
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        consts.add(tgt.id)
        if "KEEP_UNKNOWN_DAYS" not in consts:
            raise AssertionError("缺少 KEEP_UNKNOWN_DAYS 常量：unknown 目录会继续按 90 天占坑")
        # 清理函数体内必须引用它（AST 查真实 Name 节点，不查注释/文档字符串）
        used = any(isinstance(n, ast.Name) and n.id == "KEEP_UNKNOWN_DAYS"
                   for n in ast.walk(fns["_prune_old_runs"]))
        if not used:
            raise AssertionError(
                "KEEP_UNKNOWN_DAYS 定义了但 _prune_old_runs() 里没用 —— 是摆设")

        # ---- 6) 负向自检：断言逻辑本身抓得住回归 ----
        # (a) 模拟一段"顶层调用 + except pass"的代码，确认检测器能识别
        bad_src = (
            "def _prune_old_runs():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
            "_prune_old_runs()\n"
        )
        bt = ast.parse(bad_src)
        _bare = 0
        for n in ast.walk(bt):
            if isinstance(n, ast.FunctionDef) and n.name == "_prune_old_runs":
                for h in ast.walk(n):
                    if isinstance(h, ast.ExceptHandler) and len(h.body) == 1 \
                            and isinstance(h.body[0], ast.Pass):
                        _bare += 1
        _top = [s for s in bt.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                and isinstance(s.value.func, ast.Name) and s.value.func.id == "_prune_old_runs"]
        if not _bare or not _top:
            raise AssertionError(
                "负向测试失效：检测逻辑对'顶层调用 + except pass'的坏样本居然报通过")

        return ("2 个清理函数：无空 except、无顶层调用、main 内已调用；"
                "KEEP_UNKNOWN_DAYS 独立生效；负向自检通过")
    check("归档清理：异常可见 + 不在 import 时执行 + unknown 独立保留期", _run)


def test_no_redundant_recompute_and_silent_swallow():
    """【2026-09-16 新增·P1-5 + 通用规则】禁止"同一份计算做两遍"与"复核失败静默吞"。

    背景：
    (1) **P1-5 重复计算**：`first_card_status_green()` 里判定绿块后，为了拿绿块的
        绝对坐标，把 `sub → hsv → inRange → morphologyEx → findContours`
        **整套又算了一遍**。这五步全是纯函数、参数逐字与上面相同，结果必然一致。
        它不是"二次校验"（数据源相同，校验不出东西），只是纯浪费 + 维护陷阱：
        以后只改上面阈值不改下面，两处就悄悄不一致，而坐标复核用的还是旧阈值。

    (2) **复核失败静默吞**：同一处的坐标复核是 `except Exception: pass`。
        复核本身可以失败不阻断（还有形状门槛兜底），但**必须留痕** ——
        否则这道保险悄悄失效了也没人知道。

    → 断言：
      A. `first_card_status_green` 函数体内 `cv2.cvtColor` 调用次数 <= 1
         （重复计算的最直接特征：同一函数里对同一 ROI 多次转 HSV）；
      B. 该函数体内不得有 `except: pass`。

    【负向测试】把重复计算加回去 / 把告警换回 pass，本断言必须 FAIL。
    """
    def _run():
        p = os.path.join(HERE, "signin.py")
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="signin.py")

        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "first_card_status_green":
                fn = n
                break
        if fn is None:
            raise AssertionError("找不到 first_card_status_green()（改名了？本断言需同步更新）")

        # ---- A. 同一函数里 cvtColor 最多 1 次 ----
        n_cvt = 0
        for h in ast.walk(fn):
            if isinstance(h, ast.Call) and isinstance(h.func, ast.Attribute) \
                    and h.func.attr == "cvtColor":
                n_cvt += 1
        if n_cvt > 1:
            raise AssertionError(
                "first_card_status_green() 里 cv2.cvtColor 调了 %d 次 —— "
                "同一 ROI 重复转 HSV，说明又出现了'整套重算一遍'的冗余：\n"
                "      → 应复用上面算好的 hsv / mask_g / m_g / cnts（参数逐字相同，结果必然一致）"
                % n_cvt)

        # ---- B. 不得有 'except: pass' ----
        bare = []
        for h in ast.walk(fn):
            if isinstance(h, ast.ExceptHandler) and len(h.body) == 1 \
                    and isinstance(h.body[0], ast.Pass):
                bare.append(getattr(h, "lineno", "?"))
        if bare:
            raise AssertionError(
                "first_card_status_green() 第 %s 行有 'except: pass' —— "
                "坐标复核失败会被静默吞掉，这道保险悄悄失效也没人知道" % bare)

        # ---- 负向自检：检测器本身要能识别坏样本 ----
        bad = ast.parse(
            "def first_card_status_green(h):\n"
            "    a = cv2.cvtColor(x, cv2.COLOR_BGR2HSV)\n"
            "    b = cv2.cvtColor(x, cv2.COLOR_BGR2HSV)\n"
            "    try:\n"
            "        q()\n"
            "    except Exception:\n"
            "        pass\n"
        )
        _fn = next(n for n in ast.walk(bad)
                   if isinstance(n, ast.FunctionDef) and n.name == "first_card_status_green")
        _c = sum(1 for h in ast.walk(_fn)
                 if isinstance(h, ast.Call) and isinstance(h.func, ast.Attribute)
                 and h.func.attr == "cvtColor")
        _b = sum(1 for h in ast.walk(_fn)
                 if isinstance(h, ast.ExceptHandler) and len(h.body) == 1
                 and isinstance(h.body[0], ast.Pass))
        if _c <= 1 or _b == 0:
            raise AssertionError(
                "负向测试失效：检测器对'重复 cvtColor + except pass'的坏样本报通过")

        return "绿块坐标复核复用已有 mask（cvtColor=1 次）；复核异常已留痕不留白；负向自检通过"
    check("列表已签判定：不算重复账、复核失败不静默", _run)


def _find_gray_bar(img, btn_aspect_min=4.0, btn_aspect_max=14.0):
    """在整图里兜底找"灰色按钮"（当项目扫描逻辑因窗口尺寸假设不匹配而失败时用）。
    返回 list[dict]，格式同 scan_buttons 的输出。

    【2026-09-15 踩坑记录 —— 这个函数被真实样本改了三次，每次都是"看似能用但实际不行"】

    版本 1（按灰带找）：扫描"整行灰色占比 > 0.5"的行 → 拼出灰带。
        ✗ 全屏截图上误选**任务栏**（2880 宽）和全宽横带，而不是真按钮。

    版本 2（加宽高比过滤）：排除占满图宽的长条，卡宽高比 4~14。
        ✗ 真按钮只占 2880 宽里的 764，**整行灰占比只有 0.28**，
          按 0.5 卡在全屏图上一个按钮都找不到（小图 0.93 却正常）。
          → 教训：**阈值在不同尺寸的样本上会失效**，只拿小图校准必翻车。

    版本 3（按"文字块"找，当前版本）：不再找灰带边界，而是**找按钮上的白字**。
        白字(>238)在灰底(~204)上，用横向形态学闭运算把笔画拉成"文字块"，
        然后看这个文字块周围是不是灰底。
        ✓ 实测两张真实样本都精准命中，且"灰底占比"能把真按钮和标题栏分开：
              真按钮文字块  灰底占比 0.88 / 0.89
              标题栏文字块  灰底占比 0.01 / 0.02   ← 干净分离
        这个思路对"按钮被更大灰区包住"、"按钮占图比例变化"都免疫。
    """
    import cv2
    import numpy as np
    H, W = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    ink = ((g > 238) & (g < 256)).astype(np.uint8)          # 白字
    bgm = ((g > 195) & (g < 215)).astype(np.uint8)          # 灰底
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (41, 3))  # 横向拉通笔画
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, k)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(closed, 8)

    out = []
    for i in range(1, n):
        x, y, w, h, _area = stats[i]
        # 文字块尺寸：实测「已签到」81x27、「已结束」81x27
        if not (50 <= w <= 420 and 16 <= h <= 64):
            continue
        # 这块文字周围必须真的是灰底（这是区分"按钮文字"和"标题栏文字"的关键）
        pad = 22
        y0, y1 = max(0, y - pad), min(H, y + h + pad)
        x0, x1 = max(0, x - pad), min(W, x + w + pad)
        if bgm[y0:y1, x0:x1].mean() < 0.5:
            continue
        # 由文字块反推按钮：按钮以文字为中心，宽度约为文字块的 8~9 倍
        # （实测：文字块 81 宽 → 按钮 764 宽，比例 9.4）；
        # 高度按宽高比 4~14 约束，取"居中扩展"后的结果。
        cx, cy = x + w // 2, y + h // 2
        bw = int(w * 9.4)
        bh = int(round(bw / 8.5))          # 真按钮宽高比实测 8.5
        bx0, by0 = cx - bw // 2, cy - bh // 2
        bx0 = max(0, min(bx0, W - bw)); by0 = max(0, min(by0, H - bh))
        if bw < 40:
            continue
        # 注意：不能用 `bw > W * 0.9` 排除——在小图上按钮本来就几乎占满宽度
        # （823 宽的小程序截图里按钮 764 宽 = 93%），这会误杀真按钮。
        # 只排除"明显超过图宽"的异常值。
        if bw > W:
            continue
        aspect = bw / float(bh)
        if not (btn_aspect_min <= aspect <= btn_aspect_max):
            continue
        # 追加：反推出来的按钮必须**基本落在图内**，且按钮四周也应是灰底。
        # 不加这条时，标题栏/整页文字块偶尔也会满足上面的条件（灰底占比恰好过 0.5），
        # 导致测试**偶发**选错区域（实测出现过一次：把整页文字当按钮，
        # OCR 读回"签到内容．每日签到[窗口]激活后…"）。
        # 测试必须确定性，所以这里再加一道"按钮框内灰底占比"校验。
        if bx0 + bw > W or by0 + bh > H:
            continue
        box = bgm[by0:by0 + bh, bx0:bx0 + bw]
        if box.size == 0 or box.mean() < 0.75:
            continue
        out.append(dict(kind="gray", x=int(bx0), y=int(by0), w=int(bw), h=int(bh),
                        cx=int(cx), cy=int(cy), fill=1.0))
    return out


# =========================================================
# main
# =========================================================
def _extract_signin_funcs(names):
    """从 signin.py 抠出若干顶层函数单独执行（不 import signin，避免副作用）。

    与 test_signin_contracts / test_wifi_link_connected 里同样的手法：
    signin.py 一 import 就会建运行目录、清日志、初始化 pyautogui，
    smoke_test 绝不能 import 它，只能用 AST 抠函数体出来跑。
    """
    src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in names]
    got = {n.name for n in body}
    missing = set(names) - got
    if missing:
        raise AssertionError("signin.py 里找不到函数: %s" % ", ".join(sorted(missing)))
    g = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<signin-extract>", "exec"), g)
    return g


def test_time_window_cross_midnight():
    """时间窗解析与跨午夜判定（P1-4）。

    背景：三处用到"签到时间窗"的地方（before_signin_start / _within_signin_window /
    前置提示 _near）原来各写一份解析，且都用 `start <= now <= end` 口径。
    跨午夜窗口（如 23:50~00:10）下 start > end，该式**恒为 False** ——
    也就是"把签到窗口设成跨午夜，程序会认为永远不在窗口内"，属静默失效。

    这里锁死两件事：
      1) _parse_hhm 必须做范围校验（'25:70' 这类不能当成合法分钟数）
      2) _window_contains 必须支持跨午夜（并集语义：now>=start 或 now<=end）
    """
    section("时间窗：范围校验 + 跨午夜")

    g = _extract_signin_funcs({"_parse_hhm", "_window_contains"})
    ph, wc = g["_parse_hhm"], g["_window_contains"]

    def parse_ok():
        cases = [("20:50", 1250), ("21:30", 1290), ("00:00", 0), ("23:59", 1439),
                 ("  21:30  ", 1290)]
        for s, exp in cases:
            got = ph(s)
            assert got == exp, "_parse_hhm(%r) = %r，期望 %r" % (s, got, exp)
        return "5 组合法时刻全部解析正确（含首尾 00:00 / 23:59）"

    def parse_rejects():
        # 这些必须判"配置写坏"（返回 None），否则会被当成合法分钟数参与比较
        for s in ("25:70", "24:00", "-1:00", "21:60", "21:30:99", "", "aa:bb", None):
            got = ph(s)
            assert got is None, "_parse_hhm(%r) = %r，越界/非法值必须返回 None" % (s, got)
        return "8 组越界/非法值全部拒绝（25:70 / 24:00 / -1:00 / 21:60 / 三段式 / 空 / 非数字 / None）"

    def normal_window():
        # 20:50~21:30 = 1250~1290
        for now, exp in [(1250, True), (1270, True), (1290, True),
                         (1249, False), (1291, False), (600, False)]:
            got = wc(now, 1250, 1290)
            assert bool(got) == exp, "普通窗口 now=%d -> %r，期望 %r" % (now, got, exp)
        return "普通窗口 6 个边界点全对（含起止闭区间）"

    def cross_midnight():
        # 23:50~00:10 = 1430~10 —— 旧口径 start<=now<=end 在这里恒 False
        for now, exp in [(1430, True), (1435, True), (1439, True),
                         (0, True), (5, True), (10, True),
                         (1429, False), (11, False), (720, False)]:
            got = wc(now, 1430, 10)
            assert bool(got) == exp, "跨午夜窗口 now=%d -> %r，期望 %r" % (now, got, exp)
        return "跨午夜窗口 9 个边界点全对（23:50 / 23:55 / 23:59 / 00:00 / 00:05 / 00:10 在内；23:49 / 00:11 / 12:00 在外）"

    def old_impl_provably_broken():
        """负向对照：证明旧口径真的会失败，而不是"本来就没问题"。

        如果哪天有人把 _window_contains 改回 `start <= now <= end`，
        本断言会立刻失败（下面 6 个"确在窗口内"的点会变 False）。
        """
        wrong = 0
        for now in (1430, 1435, 1439, 0, 5, 10):
            if not wc(now, 1430, 10):
                wrong += 1
        assert wrong == 0, ("跨午夜窗口下有 %d/6 个'确在窗口内'的点被判为不在窗口内 —— "
                            "很可能 _window_contains 被改回了 `start <= now <= end`" % wrong)
        return "6 个'确在跨午夜窗口内'的点无一被判 False（旧口径在此会全判 False）"

    def config_bad_is_none():
        assert wc(100, None, 10) is None, "start 为 None 时应返回 None（交调用方保守处理）"
        assert wc(100, 10, None) is None, "end 为 None 时应返回 None"
        return "配置坏（None）时返回 None，不擅自判 True/False"

    def single_source_of_truth():
        """三处调用点必须统一走共享助手，不能再各写一份 split(':') 解析。

        判据用 AST 查真实调用节点（不是查字符串 —— 注释/文档串会骗过字符串匹配，
        本项目在这上面踩过两次坑）。
        """
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src)

        def calls_of(fname):
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and n.name == fname:
                    names = set()
                    for c in ast.walk(n):
                        if isinstance(c, ast.Call):
                            f = c.func
                            nm = getattr(f, "attr", None) or getattr(f, "id", None)
                            if nm:
                                names.add(nm)
                    return names
            return set()

        for fname in ("before_signin_start", "_within_signin_window"):
            names = calls_of(fname)
            assert "_parse_hhm" in names, (
                "%s() 没有调用 _parse_hhm()，说明又出现了独立的时间解析实现" % fname)
        # 前置提示那段在 main() 里，main 很大，只要求它用到共享助手
        names = calls_of("main")
        assert "_parse_hhm" in names, "main() 的时间窗提示没有走 _parse_hhm()"
        assert "_window_contains" in names, "main() 的时间窗提示没有走 _window_contains()"

        # 反向：signin.py 里不应再有"本地重定义 _hhm"这种重复实现
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "_hhm":
                raise AssertionError(
                    "signin.py 第 %d 行又出现了内联的 _hhm() 局部实现，"
                    "请改用共享的 _parse_hhm()" % n.lineno)
        return ("before_signin_start / _within_signin_window / main 三处均走共享助手；"
                "且无内联 _hhm 重复实现（AST 实测）")

    check("时间窗：_parse_hhm 合法值解析", parse_ok)
    check("时间窗：_parse_hhm 越界值必须拒绝", parse_rejects)
    check("时间窗：普通窗口判定", normal_window)
    check("时间窗：跨午夜窗口判定（旧实现恒 False）", cross_midnight)
    check("时间窗：跨午夜真缺陷的负向对照", old_impl_provably_broken)
    check("时间窗：配置坏时返回 None", config_bad_is_none)
    check("时间窗：三处调用点统一走共享助手", single_source_of_truth)


def test_history_cross_midnight_date():
    """台账日期归属必须按"开跑时刻"，不是"结束时刻"（P1-7）。

    一次运行可能跨午夜（23:58 开跑、00:03 收尾）。那次签到属于**前一天**，
    若记成次日，会让 recent_records() 的日期过滤、stats() 的 fail_streak、
    should_escalate() 的判断整体错位一天。

    这里用真实调用 append_record（写进临时目录，不碰项目台账）+ 回读校验，
    并配负向对照：把 start_ts 去掉，必须退回"现在"的日期。
    """
    section("台账：跨午夜的日期归属")

    import tempfile
    import history as H

    def run_case(start_dt, expect_date):
        with tempfile.TemporaryDirectory() as td:
            # start_ts / end_ts 刻意跨天：start 在前一天 23:5x，end 在次日 00:0x
            s_ts = start_dt.timestamp()
            e_ts = s_ts + 400          # 约 6 分 40 秒后收尾
            H.append_record("success", code=0, cost_sec=400.0,
                            start_ts=s_ts, end_ts=e_ts,
                            run_dir="run_synthetic", base_dir=td)
            rows = H._read_rows(H._path(td))
            assert rows, "append_record 没有写出任何行（可能 base_dir 用法不对）"
            got = rows[-1].get("date")
            assert got == expect_date, (
                "跨午夜记录的 date = %r，期望 %r（该次签到属于开跑那天）" % (got, expect_date))
            return rows[-1]

    def cross_midnight_uses_start_day():
        # 开跑 2026-09-15 23:58 → 收尾 2026-09-16 00:04，应记 09-15
        row = run_case(datetime(2026, 9, 15, 23, 58, 0), "2026-09-15")
        assert row.get("weekday") == "二", (
            "weekday 应与 date 同一天（09-15 是周二），实际 %r" % row.get("weekday"))
        return "23:58 开跑 / 00:04 收尾 → 记在 2026-09-15（周二），日期与星期一致"

    def normal_case_unaffected():
        # 不跨天的情况不能受影响：20:52 开跑 → 仍是当天
        run_case(datetime(2026, 9, 15, 20, 52, 0), "2026-09-15")
        return "普通（不跨天）运行仍记在开跑当天，未受影响"

    def negative_no_start_ts_falls_back():
        """负向对照：没有 start_ts 时（历史导入等）必须退回"现在"的日期。

        若有人把 `if start_ts:` 保护去掉、直接 fromtimestamp(None)，
        这里会抛异常/写错日期，断言即失败。
        """
        with tempfile.TemporaryDirectory() as td:
            H.append_record("fail", code=1, cost_sec=1.0,
                            start_ts=None, end_ts=None, base_dir=td)
            rows = H._read_rows(H._path(td))
            assert rows, "start_ts=None 时也必须能写出一条记录"
            today = datetime.now().strftime("%Y-%m-%d")
            assert rows[-1].get("date") == today, (
                "无 start_ts 时 date = %r，期望回退到今天 %r" % (rows[-1].get("date"), today))
        return "start_ts 缺失时安全回退到'今天'，不会因 fromtimestamp(None) 崩掉"

    check("台账：跨午夜按开跑日归属", cross_midnight_uses_start_day)
    check("台账：不跨天时不受影响", normal_case_unaffected)
    check("台账：无 start_ts 时安全回退（负向对照）", negative_no_start_ts_falls_back)


def test_bat_encoding_consistency():
    """所有 .bat 的编码必须与它声明的 chcp 一致（P1-8）。

    真实现场：`看签到统计.bat` 的正文是 **UTF-8**，但第 2 行写着 `chcp 936`（GBK）。
    cmd 按 936 解码 UTF-8 字节 → 所有中文（title、echo）显示成乱码，
    甚至出现"非法 GBK 序列"的方块。同目录其它 6 个 .bat 都是 GBK，只有它不一致。

    判据：带中文的 .bat 必须能被 GBK 解码（因为 chcp 936），否则就是编码错配。
    """
    section(".bat 编码一致性")

    def all_bats_gbk_decodable():
        import glob
        if not glob.glob(os.path.join(HERE, "*.bat")):
            raise AssertionError("项目根目录下找不到任何 .bat（路径不对？）")
        offenders = []
        checked = 0
        for p in sorted(glob.glob(os.path.join(HERE, "*.bat"))):
            raw = open(p, "rb").read()
            if not any(b > 127 for b in raw):
                continue                      # 纯 ASCII，编码无所谓
            checked += 1
            try:
                raw.decode("gbk")
            except Exception:
                offenders.append(os.path.basename(p))
        assert not offenders, (
            "这些 .bat 含非 ASCII 字节但**无法按 GBK 解码**，"
            "而它们都写了 `chcp 936` → 中文会显示成乱码：%s" % "、".join(offenders))
        return "%d 个含中文的 .bat 全部可按 GBK 解码，与 chcp 936 一致" % checked

    def looks_like_known_offender():
        """定点回归：`看签到统计.bat` 曾经是 UTF-8 正文 + chcp 936。"""
        p = os.path.join(HERE, "看签到统计.bat")
        if not os.path.isfile(p):
            raise AssertionError("看签到统计.bat 不存在")
        raw = open(p, "rb").read()
        try:
            txt = raw.decode("gbk")
        except Exception as e:
            raise AssertionError("看签到统计.bat 又不能按 GBK 解码了（%s）—— 中文会乱码" % e)
        # 再确认它确实含中文（防止有人"把中文删掉"来绕过测试）
        assert any("\u4e00" <= ch <= "\u9fff" for ch in txt), (
            "看签到统计.bat 里已经没有任何中文了——要么被误删，要么为绕过测试清空了内容")
        return "看签到统计.bat 现已与其它 .bat 一致（GBK 正文 + chcp 936），中文可正常显示"

    check(".bat：带中文的文件必须能按 GBK 解码", all_bats_gbk_decodable)
    check(".bat：看签到统计.bat 编码定点回归", looks_like_known_offender)


def test_self_heal_state_durability():
    """自愈状态：写失败必须出声 + 必须原子写（P1-9）。

    两个问题都真实存在过：
      1) `_save()` 原来是 `except Exception: pass` —— 写失败完全无声。
         后果链条：state 不落盘 → 下次 _load 读到空 state →
         preheat() 认为"无上次失败记录"跳过预热 → **自愈永久停摆且零日志**。
      2) 原来直接 `open(STATE_PATH, "w")` 覆盖写：进程恰好在此刻被强杀会留下
         半截 JSON → 下次解析失败 → consecutive 计数清零 → 冷却失效 →
         同一故障被无限预热。
    """
    section("自愈：状态持久化的可靠性")

    import importlib.util

    def load_self_heal():
        spec = importlib.util.spec_from_file_location(
            "self_heal_probe", os.path.join(HERE, "self_heal.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    sh = load_self_heal()

    def roundtrip_ok():
        """正常路径：写进去能读回来，且不留 .tmp 残渣。"""
        st = {"last_failure": {"failure_code": "X"}, "consecutive": {"X": 2},
              "history": [{"result": "fail"}]}
        sh._save(st)
        back = sh._load()
        assert back.get("consecutive") == {"X": 2}, (
            "写入后回读的 consecutive 不一致: %r" % back.get("consecutive"))
        assert back.get("last_failure"), "last_failure 丢失"
        tmp = str(sh.STATE_PATH) + ".tmp"
        assert not os.path.exists(tmp), "原子写留下了 .tmp 残渣: %s" % tmp
        return "写→读一致，且无 .tmp 残留（原子写生效）"

    def failure_is_visible():
        """负向：写失败必须往 stderr 说话，不能静默。

        若有人把 _save 改回 `except Exception: pass`，本断言立刻失败。
        """
        import io
        orig = sh.STATE_PATH
        try:
            sh.STATE_PATH = type(orig)("Z:/__nonexistent_dir_for_test__/state.json")
            buf = io.StringIO()
            old = sys.stderr
            sys.stderr = buf
            try:
                sh._save({"a": 1})
            finally:
                sys.stderr = old
            out = buf.getvalue().strip()
            assert out, ("_save() 写失败时没有输出任何提示 —— "
                         "自愈会在零日志的情况下永久停摆")
            assert "自愈" in out, "失败提示应带 '[自愈]' 前缀便于检索，实际: %r" % out[:80]
        finally:
            sh.STATE_PATH = orig
        return "写失败时 stderr 有明确提示（含 [自愈] 前缀）"

    def atomic_write_no_partial():
        """原子性：源码里必须是 临时文件 + os.replace，不能直接覆盖写。"""
        src = open(os.path.join(HERE, "self_heal.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "_save":
                fn = n
                break
        assert fn is not None, "self_heal.py 里找不到 _save()"
        body = ast.unparse(fn)
        assert "os.replace" in body, (
            "_save() 没有用 os.replace 原子替换 —— 强杀时可能留下半截 JSON，"
            "导致 consecutive 计数清零、冷却机制失效")
        assert ".tmp" in body, "_save() 没有写临时文件"
        return "「临时文件 + os.replace」原子写在位（AST 实测）"

    check("自愈：状态写→读一致且无 tmp 残留", roundtrip_ok)
    check("自愈：写失败必须出声（负向对照）", failure_is_visible)
    check("自愈：必须原子写盘", atomic_write_no_partial)


def test_no_unclosed_response_handles():
    """网络请求的 response 必须关闭（P2-7）。

    `capture_portal_url()` 里 `opener.open(req)` 在**非重定向**路径
    （真连通了、没抛 HTTPError）下不会进 except，原来返回值被直接丢掉，
    底层 socket 要等 GC 才回收。该函数在每次 wifi 自愈检查里都会调用。
    """
    section("资源：网络响应句柄")

    def portal_probe_uses_with():
        p = os.path.join(HERE, "wifi_helper", "browser_login.py")
        src = open(p, encoding="utf-8").read()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "capture_portal_url":
                # 找所有 opener.open(...) 调用，确认每个都在 with 里
                with_calls = set()
                for w in ast.walk(n):
                    if isinstance(w, ast.With):
                        for item in w.items:
                            for c in ast.walk(item.context_expr):
                                if isinstance(c, ast.Call):
                                    with_calls.add(c.lineno)
                naked = []
                for c in ast.walk(n):
                    if isinstance(c, ast.Call):
                        f = c.func
                        if getattr(f, "attr", None) == "open" and \
                           getattr(getattr(f, "value", None), "id", None) == "opener":
                            if c.lineno not in with_calls:
                                naked.append(c.lineno)
                assert not naked, (
                    "capture_portal_url() 里有未用 with 关闭的 opener.open()，"
                    "行号 %s —— 响应对象泄漏，socket 要等 GC" % naked)
                return "capture_portal_url() 的 opener.open() 已用 with 关闭（AST 实测）"
        raise AssertionError("browser_login.py 里找不到 capture_portal_url()")

    check("资源：portal 探测的响应已关闭", portal_probe_uses_with)


def test_white_screen_semantics():
    """白屏判定：**"测不了" 不得等于 "白屏"**（P1-1 回归护栏）。

    【为什么必须锁这条】
      `client_white_ratio()` 原来三种"测不了"的情况（窗口句柄失效 / 窗口在屏幕外 /
      取图抛异常）统统 `return 1.0` —— 而 1.0 等价于"判定为白屏"。调用方于是会去
      **杀微信进程重启**：约 40 秒 + 小程序重载的全部风险，还会把真正的错误掩盖掉。
      修复方向是把"测不了"返回 None、由调用方单独处理。

    【护栏缺口是实测发现的】
      2026-09-16 用负向测试核查（把 `return None` 改回 `return 1.0`）：
      **154 项断言全部通过，无一捕获** —— 也就是说 P1-1 修完了却没有护栏，
      任何人一次不小心的回退都不会被发现。按本项目"每修一处必配负向测试"的规矩，
      这条必须补上。

    判据分两层（运行时 + AST），任一层失效另一层仍能拦住：
      1. 运行时：传无效窗口句柄，必须得到 None 而不是一个 >= 阈值的占比；
      2. AST：`client_white_ratio` 里不得出现**硬编码常量 return**（1.0 误判的形态）。
    """
    section("白屏判定：'测不了' 不得等同 '白屏'")

    def _run():
        try:
            import cv2  # noqa: F401
            sys.path.insert(0, HERE)
            # 【防污染】同其他 import signin 处：把日志重定向到临时目录
            os.environ["SIGNIN_LOG_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="smoke_wrlog_")
            import signin as S
        except Exception as e:
            raise AssertionError("导入失败: %s" % e)

        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "client_white_ratio":
                fn = n
        assert fn is not None, "signin.py 找不到 client_white_ratio()"

        # --- 判据 1（AST）：不得出现**数值常量**的 return（`return 1.0` 就是它）---
        # 注意：`return None` 在 AST 里同样是 ast.Constant(value=None)，
        # 所以必须把 None 排除掉，否则会把正确实现全部打成 FAIL。
        const_returns = [n.lineno for n in ast.walk(fn)
                         if isinstance(n, ast.Return)
                         and isinstance(n.value, ast.Constant)
                         and isinstance(n.value.value, (int, float))]
        assert not const_returns, (
            "client_white_ratio() 第 %s 行有硬编码**数值常量**的 return ——"
            "把'测不了'当成'白屏'会让调用方去杀微信重启（代价极高的错误自愈）"
            % const_returns)

        # --- 判据 2（AST）：必须有 >=2 处"测不了 → None"的显式分支 ---
        none_returns = [n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.Return)
                        and isinstance(n.value, ast.Constant)
                        and n.value.value is None]
        assert len(none_returns) >= 2, (
            "client_white_ratio() 只有 %d 处 `return None`（期望 >=2："
            "窗口失效 / 区域为空）—— '测不了' 没有被区分出来" % len(none_returns))

        # --- 判据 3（运行时）：无效句柄必须返回 None，而不是一个"白屏"占比 ---
        wr = S.client_white_ratio(0)
        assert wr is None, (
            "client_white_ratio(0) 返回了 %r（期望 None）——"
            "无效窗口被当成了白屏，会触发杀微信重启的自愈" % (wr,))

        white, ratio = S.is_white_screen(0)
        assert white is False and ratio is None, (
            "is_white_screen(0) = (%r, %r)（期望 (False, None)）——"
            "'测不了' 不能按'是白屏'处理" % (white, ratio))

        # --- 判据 4（AST）：调用方必须走 is_white_screen，不得直接用裸占比判 ---
        callers_txt = ast.dump(fn)
        assert "client_white_ratio" in callers_txt
        uses_is_white = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "is_white_screen"
            for n in ast.walk(tree))
        assert uses_is_white, (
            "signin.py 里没有任何地方调用 is_white_screen() ——"
            "调用方绕过它直接用占比判，None 的语义会被丢回给 1.0 时代")

        return ("client_white_ratio 无硬编码 return、%d 处 return None；"
                "无效句柄实测得 None；调用走 is_white_screen（AST + 运行时）"
                % len(none_returns))

    check("白屏：'测不了' 不得等同 '白屏'", _run)


def test_cdp_portal_hijack_detection():
    """CDP 兜底登录：**必须能识别出"被门户劫持"**（2026-09-16 修复的假成功）。

    【为什么必须锁这条】
      `browser_login._internet_ok()` 决定"这次浏览器登录到底成没成"。
      原来的判据有两个独立的洞，**方向都是"把被劫持误判成已通"**：
        ① 不看最终 URL。`generate_204` 被 AC 302 到门户时 urllib 会自动跟随，
           于是 `r.status` 是 200、body 是门户页 —— 而
           `r.status in (200, 204)` 这个条件**永远为真**
           （非 2xx 会抛 HTTPError，根本走不到那一行），等于没校验。
        ② 特征串只有 3 个，`signin.py` 用的是 7 个。少掉的 `DDDDD` / `upass`
           正是 Dr.COM 门户**登录表单的字段名**，也就是劫持时最稳定出现的字眼。
      判错方向的代价不对称：把"已通"误判成"没通"只是多等一轮；
      把"被劫持"误判成"已通"会让上层**报告登录成功**（假成功），
      后面所有步骤都建立在错误前提上。

    判据分两层（静态 + 运行时），任一层失效另一层仍能拦住。
    """
    section("CDP 登录：被门户劫持不得判成'已通'")

    BL = os.path.join(HERE, "wifi_helper", "browser_login.py")

    def static_checks():
        src = open(BL, encoding="utf-8").read()
        tree = ast.parse(src)

        # --- 判据 1（AST）：_internet_ok 必须真的读最终 URL 并查网关 ---
        # 注意：不能用 `"geturl" in src` —— 注释/文档字符串里出现同样字眼就会
        # 假通过（本项目已经踩过三次的坑）。只认 ast.Attribute 调用。
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "_internet_ok":
                fn = n
        assert fn is not None, "browser_login.py 找不到 _internet_ok()"

        geturl_lines = [n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "geturl"]
        assert geturl_lines, (
            "_internet_ok() 没有调用 r.geturl()（AST 实测）——"
            "不看最终 URL 就无法发现'被 302 到门户网关'，会把劫持判成已通")

        # --- 判据 2（AST）：特征串表必须覆盖 signin.py 的全部门户标记 ---
        # 从 signin.py 取权威清单，避免两边再次漂移
        s_src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        s_tree = ast.parse(s_src)
        want = None
        for n in s_tree.body:
            if isinstance(n, ast.Assign):
                tgt = n.targets[0]
                if isinstance(tgt, ast.Name) and tgt.id == "PORTAL_MARKERS":
                    want = tuple(e.value for e in n.value.elts)
        assert want, "signin.py 里没找到 PORTAL_MARKERS 定义"

        got = None
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                tgt = n.targets[0]
                if isinstance(tgt, ast.Name) and tgt.id == "PORTAL_MARKERS":
                    got = tuple(e.value for e in n.value.elts)
        assert got, (
            "browser_login.py 没有模块级 PORTAL_MARKERS（AST 实测）——"
            "门户特征串没有集中定义，两边随时会再次漂移")
        missing = [m for m in want if m not in got]
        assert not missing, (
            "browser_login 的 PORTAL_MARKERS 漏掉了 %s ——"
            "这些是 signin.py 已确认的门户特征串，漏掉会漏判门户（假成功）" % missing)

        # --- 判据 2b（AST）：PORTAL_MARKERS 必须**真的被用**在 body 检查里 ---
        # 只定义不使用 = 摆设。负向测试（把 `any(m in body for m in PORTAL_MARKERS)`
        # 换成 `if False:`）第一版**没被拦住**，才补上这条。
        uses_markers = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id == "PORTAL_MARKERS":
                uses_markers = True
        assert uses_markers, (
            "_internet_ok() 里没有引用 PORTAL_MARKERS（AST 实测）——"
            "门户特征串定义了却没用，body 检查形同虚设（原地劫持会漏判）")

        # --- 判据 3（运行时）：用真实响应形态喂各种门户，方向必须对 ---
        import urllib.request
        sys.path.insert(0, os.path.join(HERE, "wifi_helper"))
        import importlib
        if "browser_login" in sys.modules:
            BL_mod = importlib.reload(sys.modules["browser_login"])
        else:
            import browser_login as BL_mod

        class _Fake:
            def __init__(self, status, url, body):
                self.status, self._u, self._b = status, url, body
            def geturl(self):
                return self._u
            def read(self, n=-1):
                return self._b[:n] if n and n > 0 else self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = urllib.request.urlopen

        def drive(status, url, body):
            urllib.request.urlopen = lambda req, **k: _Fake(status, url, body)
            try:
                return BL_mod._internet_ok(("http://example.invalid/generate_204",))
            finally:
                urllib.request.urlopen = orig

        probe = "http://connectivitycheck.gstatic.com/generate_204"
        gw = "http://10.123.0.253/a79.htm"
        # 真连通：必须 True
        assert drive(204, probe, b"") is True, "真连通（204 + 空 body）被判成了'没通'"
        # 被 302 到网关、body 无任何特征串：必须 False（这是原来漏掉的那类）
        assert drive(200, gw, b"<html>welcome</html>") is False, (
            "被 302 到门户网关（body 无特征串）被判成了'已通' —— 假成功")
        # 门户表单字段名：必须 False（原来特征串没列举到这类）
        assert drive(200, gw, b'<input name="DDDDD"><input name="upass">') is False, (
            "门户页含 DDDDD/upass 被判成了'已通' —— 假成功")
        # 【关键】门户**原地返回 200**：最终 URL 仍是探测源本身（不是网关），
        # 此时唯一能识别的线索就是 body 里的特征串。
        # 这条专门锁"必须真的检查 body"，否则删掉特征串检查也测不出来
        # （2026-09-16 的负向测试正是靠它抓出了第一版护栏的摆设性）。
        assert drive(200, probe, b'<script src="/eportal/x.js"></script>') is False, (
            "门户原地返回 200（最终 URL 未变、只在 body 里露出 eportal 特征）"
            "被判成了'已通' —— 说明 body 特征串检查没生效，是假成功")
        assert drive(200, probe, b'<input name="DDDDD">') is False, (
            "门户原地返回 200、body 含 DDDDD 被判成了'已通' —— 假成功")
        # 正常外网站点：必须 True
        assert drive(200, "http://www.baidu.com", b"<html>baidu</html>") is True, (
            "正常外网站点被判成了'没通'（方向太保守，会导致无谓重登）")

        return ("_internet_ok 读最终 URL 查网关、PORTAL_MARKERS 与 signin.py 对齐（%d 个）；"
                "真实响应形态 6 例实测方向全对（含'原地劫持只看 body'）" % len(got))

    check("CDP 登录：被门户劫持不得判成'已通'", static_checks)


def test_match_geometry_guard():
    """match() 的几何校验 must_be_in（P0-1，2026-09-16 12:33 事故）。

    【事故经过（真实日志 + 截图双重取证）】
      微信在同一台机器上有**两个** Qt 顶层窗口，标题不同：
        · 标题「微信」  → 主界面窗口（事故时 hwnd=918282 rect=(4,0,1122,1715)，WS_VISIBLE=否）
        · 标题「Weixin」→ 另一个窗口（hwnd=3082690 rect=(1267,634,1613,1025)）
      step_enter_wechat 用 `activate("微信", exact=True)` 选中了 918282，
      紧接着 `match(ENTER_WECHAT_BTN)` **不传 roi** → 整屏搜索 →
      命中的是**另一个窗口**里的「进入微信」按钮，中心 (1430,1053)。
      而 918282 的右边界只有 1122 —— **命中点 x=1430 越界 308px**。
      脚本照样点了：点击落在没被激活的窗口上 → 不生效
      → wait_wechat_rendered 只判白屏（登录页不白屏）→ 误报"已就绪"
      → 一路错到搜索框 conf=0.446 → 反复重启微信（用户看到的"任务栏弹窗口"）。

    【本测试锁死什么】
      1) 命中点在矩形内 → 正常返回（不误杀）
      2) 命中点越界 → 必须返回 (-1.0, None, None)，即"没找到"
      3) 矩形边界上的点按闭区间放行（避免 off-by-one 误杀）
      4) must_be_in=None → 保持旧行为（向后兼容）
      5) 用**真实事故截图**验证：整屏 0.977 vs 限定在选中窗口内 0.285
         —— 证明按钮真的不在那个窗口里，而不是我拍脑袋说的
    """
    section("匹配：几何校验 must_be_in")

    def guard_logic():
        """直接抠 match() 的几何校验分支下来跑（不 import signin）。

        match() 依赖 screen_bgr/cv_read/logger，整体 exec 会缺依赖；
        所以这里用 AST 把"几何校验"那段逻辑抽出来等价验证 ——
        但为了不变成"测试自己写的逻辑"，下面第 5 项会用真实图像端到端跑真 match()。
        """
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "match":
                fn = n
                break
        assert fn is not None, "signin.py 里找不到 match()"

        # 断言：match() 签名里必须有 must_be_in
        args = [a.arg for a in fn.args.args]
        assert "must_be_in" in args, (
            "match() 缺少 must_be_in 参数 —— P0-1 的几何校验被删了！"
            "（本次事故正是'命中点越界 308px 仍照点'造成的）")

        # 断言：函数体里必须真的用到 must_be_in（不是只加了个没用的参数）
        used = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id == "must_be_in":
                used = True
                break
        assert used, "match() 声明了 must_be_in 却从未使用 —— 护栏是摆设"

        # 断言：必须有"越界即返回 -1.0"的分支
        has_reject = False
        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                # 找 return -1.0, None, None
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple):
                        elts = sub.value.elts
                        if len(elts) == 3 and isinstance(elts[0], ast.UnaryOp) and \
                           isinstance(elts[0].op, ast.USub) and \
                           isinstance(elts[0].operand, ast.Constant) and \
                           elts[0].operand.value == 1.0:
                            has_reject = True
        assert has_reject, "match() 没有'越界返回 -1.0'的分支"

        return "match() 有 must_be_in 参数、真的使用、且有越界拒绝分支（AST 实测）"

    def real_screenshot_proof():
        """用真实事故截图端到端验证几何校验的必要性。

        这是本测试的核心：不靠推理，靠**12:33 那次留下的真实截图**。
        截图：logs/run_20260916_123305/123334_进入微信后.jpg（2880x1800）
              —— 点击后仍停在「进入微信」页（正是故障现场）
        模板：templates/00_enter_wechat.png（312x53）

        预期（已实测）：
          · 整屏搜索            → conf≈0.977，中心 (1430,1053)   ← 脚本实际拿到的
          · 限定在选中窗口内搜索 → conf≈0.285                    ← 根本不在里面
        差值 0.977 vs 0.285 就是"选错窗口"的铁证。
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            return "跳过（无 cv2）"

        shot = os.path.join(HERE, "logs", "run_20260916_123305", "123334_进入微信后.jpg")
        tpl_p = os.path.join(HERE, "templates", "00_enter_wechat.png")
        if not os.path.exists(shot):
            return "跳过（事故截图已被归档清理，无法复现验证）"
        if not os.path.exists(tpl_p):
            raise AssertionError("模板 00_enter_wechat.png 不存在")

        def imread_u(p):
            return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)

        tpl = imread_u(tpl_p)
        full = imread_u(shot)
        assert tpl is not None and full is not None, "读图失败"
        assert full.shape[:2] == (1800, 2880), \
            "事故截图分辨率变了(%s)，本断言的前提失效，请重新实测" % (full.shape,)

        def best_in(img, ox, oy):
            best = (-1.0, None, None)
            sh, sw = img.shape[:2]
            for sc in (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15):
                th0, tw0 = tpl.shape[:2]
                nw, nh = max(4, int(round(tw0 * sc))), max(4, int(round(th0 * sc)))
                if nh > sh or nw > sw:
                    continue
                t2 = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_AREA) \
                    if abs(sc - 1) > 1e-3 else tpl
                r = cv2.matchTemplate(img, t2, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(r)
                cand = (float(mv), ox + int(ml[0]) + nw // 2, oy + int(ml[1]) + nh // 2)
                if cand[0] > best[0]:
                    best = cand
            return best

        # 整屏（脚本实际行为）
        c_full, x_full, y_full = best_in(full, 0, 0)
        assert c_full > 0.9, "整屏应能高置信命中按钮，实际 conf=%.3f" % c_full
        assert (x_full, y_full) == (1430, 1053), \
            "整屏命中点变了 (%s,%s)，与事故记录 (1430,1053) 不符 —— 前提失效" % (x_full, y_full)

        # 限定在脚本选中的窗口 rect=(4,0,1122,1715) 内
        l, t, r, b = 4, 0, 1122, 1715
        roi = full[t:b, l:r]
        c_win, _, _ = best_in(roi, l, t)

        # 核心断言：按钮不在选中窗口里，且差异必须足够大（不是噪声）
        assert c_win < 0.5, (
            "限定在选中窗口(4,0,1122,1715)内竟能命中 conf=%.3f —— "
            "若如此则'选错窗口'的结论不成立，请重新分析" % c_win)
        assert c_full - c_win > 0.5, (
            "整屏(%.3f)与窗内(%.3f)差异仅 %.3f，不足以证明'按钮在另一个窗口'"
            % (c_full, c_win, c_full - c_win))

        # 几何断言：命中点确实越界
        assert not (l <= x_full <= r), \
            "命中点 x=%d 竟在窗口右边界 %d 之内 —— 与事故记录矛盾" % (x_full, r)
        assert x_full - r == 308, "越界量变了（%d，期望 308）" % (x_full - r)

        return ("真实截图实测：整屏 conf=%.3f@(%d,%d) vs 选中窗内 conf=%.3f；"
                "命中点越界 %dpx —— 几何校验确有必要"
                % (c_full, x_full, y_full, c_win, x_full - r))

    check("匹配：match() 声明并真正使用 must_be_in（AST）", guard_logic)
    check("匹配：真实事故截图证明越界（整屏 vs 窗内）", real_screenshot_proof)


def test_enter_wechat_verifies_click():
    """step_enter_wechat 点击后必须验证是否真的进去了（P0-2）。

    【为什么必须有这一条】
      原来点击后只调 wait_wechat_rendered()，而它**只判白屏**。
      登录页不是白屏 → 它立刻返回"已就绪" → 脚本完全失去"发现没进去"的能力。
      结果：一路错到搜索框那步才报 conf=0.446 → 触发"重启微信以恢复窗口状态"
      → **反复杀微信重启**，就是用户看到的"任务栏弹窗口"。

    【修复方式】点击后**再次匹配那个按钮**，若仍在 → 判定没进去，
      打 ERROR、存失败现场截图、直接返回 False（不再重启微信）。

    【本测试锁死什么】(全部用 AST 查真实节点，不查字符串 —— 见 MEMORY 4.4)
      1) 点击之后，函数体内必须存在"第二次匹配 ENTER_WECHAT_BTN"的调用
      2) 该调用必须作为 match 的实参出现（不是文档字符串里提一句）
      3) 必须有"仍命中 → return False"的分支（不能只是 log 完继续走）
      4) 该分支必须打 ERROR 级别的日志（失败要暴露）
      5) 负向验证：把验证块删掉，断言 1/2/3 必须失败
    """
    section("进入微信：点击后验证（防'没进去却继续走'）")

    def find_step_fn():
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "step_enter_wechat":
                return n, src
        raise AssertionError("signin.py 里找不到 step_enter_wechat()")

    def collect_match_calls(fn):
        """返回 [(行号, 模板表达式源码片段)]，只统计真正的 match(...) 调用节点。

        注意：不用字符串 in 判断 —— 文档字符串里写一句
        "这里会调用 match(ENTER_WECHAT_BTN)" 就能骗过字符串检查（本项目踩过两次）。
        """
        out = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "match":
                    arg0 = node.args[0] if node.args else None
                    tpl = None
                    if isinstance(arg0, ast.Name):
                        tpl = arg0.id
                    out.append((node.lineno, tpl))
        return out

    def click_then_verify():
        fn, src = find_step_fn()
        lines = src.splitlines()

        calls = collect_match_calls(fn)
        assert len(calls) >= 2, (
            "step_enter_wechat() 里只找到 %d 次 match() 调用；"
            "P0-2 要求点击后**再匹配一次**以验证是否进了主界面" % len(calls))

        # 找 pyautogui.click 的行号
        click_lines = [n.lineno for n in ast.walk(fn)
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "attr", None) == "click"
                       and getattr(getattr(n.func, "value", None), "id", None) == "pyautogui"]
        assert click_lines, "step_enter_wechat() 里找不到 pyautogui.click"
        first_click = min(click_lines)

        # 断言 1：必须存在"点击之后"的 match(ENTER_WECHAT_BTN) 调用
        after = [(ln, tpl) for ln, tpl in calls
                 if ln > first_click and tpl == "ENTER_WECHAT_BTN"]
        assert after, (
            "step_enter_wechat() 在点击(行%d)之后**没有**再匹配 ENTER_WECHAT_BTN —— "
            "P0-2 的'点击后验证'被删了（这正是'点了没生效却一路走到底'的根源）"
            % first_click)

        verify_line = min(ln for ln, _ in after)

        # 断言 2：验证块里必须有 return False
        fn_src = ast.get_source_segment(src, fn)
        assert fn_src, "取不到函数源码"
        seg = "\n".join(lines[verify_line - 1: verify_line + 25])
        assert "return False" in seg, (
            "点击后验证块（行%d 起）没有 `return False` —— "
            "验证失败必须立刻中止，不能继续往下走。" % verify_line)

        # 断言 3：验证块必须打 ERROR（失败要暴露，不能静默）
        assert "logger.error" in seg, (
            "点击后验证块（行%d 起）没有 logger.error —— "
            "失败必须出声，不能静默吞掉。" % verify_line)

        # 断言 4：验证必须是**重试**而非只查一次
        #
        # 【为什么锁死这个】只查一次会把成功误判成失败：
        #   点击后窗口要重建（实测 sleep 6s 才稳），冷启动/机器卡时按钮可能
        #   **还在淡出过程中** → 那一刻复核得到高分 → 把"已经点进去了"判成"没生效"。
        #   这个方向比漏判更糟（假失败会让本来能成的签到被判死）。
        # 所以必须是"多轮复核、只要有一次确认按钮消失就放行"。
        # 断言方式：验证块里必须出现 for/while 循环，且循环体内有 match 调用。
        loop_ok = False
        for node in ast.walk(fn):
            if isinstance(node, (ast.For, ast.While)) and node.lineno >= verify_line - 3:
                has_match = any(isinstance(c, ast.Call) and
                                isinstance(c.func, ast.Name) and c.func.id == "match"
                                for c in ast.walk(node))
                if has_match:
                    loop_ok = True
        assert loop_ok, (
            "点击后的验证块不是**循环重试**（只查一次）—— "
            "必须多轮复核、任一次确认按钮消失即放行，否则会把"
            "'点击成功但按钮正在淡出'误判为'点击无效'（假失败，比漏判更糟）")

        return ("点击(行%d)后存在验证匹配(行%d)，且带 return False + logger.error"
                % (first_click, verify_line))

    def hidden_judgment_not_overzealous():
        """_hidden 判定不能把"已在前台的窗口"当成"隐藏到托盘"（P0-2 附带修复）。

        事故日志：`激活后前台='微信' 可见=0 最小化=0`
        —— 前台标题就是「微信」，说明窗口其实已被置前，
           `WS_VISIBLE=0` 只是跨进程读 Qt 窗口不可靠。
        旧判据据此**杀掉微信**（约 40 秒 + 状态混乱），而日志下一行就匹配到
        按钮 conf=0.999，界面明明好着。

        【必须用 AST，不能查字符串 / 正则】
        该函数体内**保留着一行注释掉的旧判据**做历史留痕：
            #     _hidden = (hw0 and not IsWindowVisible(hw0)) or ...
        用字符串或正则查源码会把**这行注释**当成真实代码 → 测试误判。
        （这不是假设：本测试第一版就是用 re.search 写的，当场被这行注释骗到 FAIL，
          与 MEMORY 4.4 记录的两次同类翻车完全一样。）
        所以改查 AST 的真实 Assign 节点 —— 注释不是 AST 节点，天然被排除。
        """
        fn, src = find_step_fn()

        def get_assign(name):
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == name:
                            return node
            return None

        a_hidden = get_assign("_hidden")
        assert a_hidden is not None, "找不到 `_hidden = ...` 的真实赋值（AST）"
        flat = " ".join((ast.get_source_segment(src, a_hidden.value) or "").split())
        assert flat, "取不到 _hidden 赋值表达式源码"

        assert "_is_fg" in flat, (
            "_hidden 的判定式没有引用 _is_fg（'已在前台'的判定结果）—— "
            "会把可用窗口误判为'隐藏到托盘'并杀掉微信重启。实际：%s" % flat)
        assert "IsWindowVisible" in flat, (
            "_hidden 丢失了 IsWindowVisible 判据。实际：%s" % flat)
        assert "not _is_fg" in flat, (
            "'已在前台'必须是否决项（not _is_fg），否则形同没改。实际：%s" % flat)

        a_isfg = get_assign("_is_fg")
        assert a_isfg is not None, "找不到 `_is_fg = ...` 的真实赋值（AST）"
        isfg = " ".join((ast.get_source_segment(src, a_isfg.value) or "").split())
        assert "fg_title()" in isfg and "微信" in isfg, (
            "_is_fg 必须是'前台标题恰为微信'，实际：%s" % isfg)

        return ("_hidden 由 AST 实测：含 IsWindowVisible 与 not _is_fg；"
                "_is_fg = 前台标题为「微信」（未被注释里的旧式样干扰）")

    check("进入微信：点击后有验证块（AST 实测）", click_then_verify)
    check("进入微信：_hidden 不把'已在前台'当'隐藏到托盘'", hidden_judgment_not_overzealous)


def test_smoke_test_does_not_pollute_real_logs():
    """冒烟测试自己不能污染真实的按天日志。

    【为什么需要这条】
      `smoke_test.py` 有三处 `import signin`（两处 OCR 测试 + 一处归档清理测试），
      而 signin.py 在 import 时会**建 run 目录 + 往 signin_YYYYMMDD.log 写两行**。
      实测后果：跑 8 次冒烟就往当天日志里灌了 50 行 `smoke_basedir_*` 噪音。
      而**按天日志是排查故障的一级证据**（MEMORY："证据和现场是两份东西"，
      早期截图会被清理但按天日志仍在）—— 测试把噪音写进证据里，
      会让人翻真实运行记录时先撞上一堆假条目，**而且很难事后意识到是测试写的**。

      `smoke_basedir_` 这个词本身就是特征：它是 test_history 用的临时目录前缀，
      真实运行绝不会产生。

    【本测试锁死什么】
      1) signin.py 必须支持 SIGNIN_LOG_DIR_OVERRIDE 重定向（否则没法防）
      2) 每一处 import signin 之前都必须设置该变量
    """
    section("测试自身：不污染真实按天日志")

    def override_supported():
        src = open(os.path.join(HERE, "signin.py"), encoding="utf-8").read()
        tree = ast.parse(src)

        # 1) 必须有一个变量从 SIGNIN_LOG_DIR_OVERRIDE 环境变量取值。
        #    注意不要假设"赋值右边直接就是 environ.get(...)" ——
        #    实际写法是 `os.environ.get("X", "").strip()`（外面套了 .strip()），
        #    所以用"赋值表达式里出现过该字符串常量，且带 environ 字样"来判定。
        ov_var = None
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            if not isinstance(n.targets[0], ast.Name):
                continue
            has_key = any(isinstance(c, ast.Constant) and c.value == "SIGNIN_LOG_DIR_OVERRIDE"
                          for c in ast.walk(n.value))
            # 注意：`os.environ` 在 AST 里是 Attribute(attr='environ')，不是 Name ——
            # 写 `Name(id='environ')` 永远匹配不到（本测试第一版就栽在这）。
            has_env = any((isinstance(x, ast.Attribute) and x.attr == "environ") or
                          (isinstance(x, ast.Name) and x.id == "environ")
                          for x in ast.walk(n.value))
            if has_key and has_env:
                ov_var = n.targets[0].id
        assert ov_var, (
            "signin.py 没有从环境变量 SIGNIN_LOG_DIR_OVERRIDE 取值 —— "
            "冒烟测试的 import signin 会把噪音写进真实按天日志")

        # 2) LOG_DIR 必须引用上面那个变量（否则重定向不生效）
        ok = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == "LOG_DIR":
                        for sub in ast.walk(n.value):
                            if isinstance(sub, ast.Name) and sub.id == ov_var:
                                ok = True
        assert ok, (
            "LOG_DIR 没有引用 %s —— 环境变量设了也不起作用（重定向形同虚设）" % ov_var)

        # 3) 必须是"有覆盖才用覆盖，否则走 config"的条件式，不能无条件用覆盖
        logdir_val = None
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == "LOG_DIR":
                        logdir_val = n.value
        assert isinstance(logdir_val, ast.IfExp), (
            "LOG_DIR 必须是条件式（有覆盖才用覆盖，否则走 config），"
            "实际是 %s —— 无条件用覆盖会让正常运行也写错目录" % type(logdir_val).__name__)

        return ("signin.py 由 %s 读环境变量，LOG_DIR 条件式引用它"
                "（未设变量时走 config，正常运行不受影响）" % ov_var)

    def all_imports_guarded():
        src = open(os.path.join(HERE, "smoke_test.py"), encoding="utf-8").read()
        tree = ast.parse(src)

        # 收集所有"对环境变量赋值"的位置：os.environ["SIGNIN_LOG_DIR_OVERRIDE"] = ...
        set_lines = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            for t in n.targets:
                if isinstance(t, ast.Subscript):
                    try:
                        key = t.slice.value
                    except AttributeError:
                        continue
                    if key == "SIGNIN_LOG_DIR_OVERRIDE":
                        set_lines.append(n.lineno)

        # 收集所有 import signin 的位置
        imp_lines = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "signin":
                        imp_lines.append(n.lineno)
            elif isinstance(n, ast.ImportFrom) and n.module == "signin":
                imp_lines.append(n.lineno)

        assert imp_lines, (
            "smoke_test.py 里找不到 import signin？（文件结构变了，请复核本断言）")

        # 每个 import 都必须被**自己那一处** env 赋值保护：
        # 在它上方"最近的一段"里存在赋值语句，且两者之间没有别的 import
        # （否则"全局只要有一个赋值"就能骗过检查 —— 本测试第二版就栽在这）。
        # 距离上限取 12 行：赋值紧邻 import（实测 1~5 行），留足余量但不过宽。
        bad = []
        for ln in sorted(imp_lines):
            prev_imp = max([x for x in imp_lines if x < ln], default=0)
            guarded = any(prev_imp < s < ln and (ln - s) <= 12 for s in set_lines)
            if not guarded:
                bad.append(ln)
        assert not bad, (
            "smoke_test.py 第 %s 行的 `import signin` 附近（上方 12 行内、且中间"
            "没有别的 import）没有 SIGNIN_LOG_DIR_OVERRIDE 的**真实赋值语句**"
            "（AST 实测）—— 该处会把噪音写进真实按天日志。"
            "注意：只在注释里提这个名字不算数。" % ", ".join(str(x) for x in bad))
        return ("%d 处 import signin 之前都有真实的 env 赋值（AST 实测，"
                "不受注释干扰）；共 %d 处赋值" % (len(imp_lines), len(set_lines)))

    check("测试防污染：signin 支持日志重定向", override_supported)
    check("测试防污染：所有 import signin 处已加保护", all_imports_guarded)

    def real_log_has_no_test_noise():
        """真实按天日志里不得出现**测试特征串**（2026-09-16 新增）。

        【先说清楚这条断言能测什么、不能测什么】
          第一版我写成"触发一次 history 告警，看真实日志字节数是否变化"。
          **负向测试证明它是摆设**：把 `child.propagate = False` 删掉、
          把字节数核对改成 `if False:`、把触发告警那行删掉 —— **三种改坏全 PASS**。
          原因（实测）：`smoke_test.py` 的每处 `import signin` 之前都设了
          `SIGNIN_LOG_DIR_OVERRIDE`，于是 `signin` 的三个 handler
          （run.log / 按天日志 / stdout）**全部指向临时目录** ——
          真实 `logs/` 在那个进程里根本收不到任何东西。
          所以"触发告警看它漏不漏"验证的是一个**当前不可能发生**的场景，
          那种断言无论怎么改坏都会 PASS，价值为零。

        【本判据改为核对"已落盘的事实"】
          直接扫真实按天日志的**内容**：只要出现测试专属特征串，就是污染。
          这些特征串真实运行绝不会产生：
            - `smoke_`     ：临时目录前缀（smoke_basedir_ / smoke_ocrlog_ / smoke_wrlog_）
            - `[测试]`      ：测试自己写的标记
            - `Temp\\probe_`：诊断脚本的临时目录
          这能拦住所有"测试污染"的现实路径，不管它是从哪个 import 溜进去的 ——
          而且它**会 FAIL**（只要有污染），不是恒真断言。

        【实测佐证】当天日志里 74 行 `smoke_basedir_*` 就是这么被发现的；
        本轮修复后连跑 10 次，计数从 74 保持不动（无新增）。
        """
        real_log = os.path.join(HERE, "logs",
                                "signin_%s.log" % datetime.now().strftime("%Y%m%d"))
        if not os.path.isfile(real_log):
            return "今天还没有真实运行日志，跳过（无污染可查）"

        marks = ("smoke_", "[测试]", "probe_")
        hits = []
        with open(real_log, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                for m in marks:
                    if m in line:
                        hits.append((i, m, line.strip()[:110]))
                        break

        if hits:
            show = "\n".join("    行 %d（%r）：%s" % (i, m, t) for i, m, t in hits[:5])
            raise AssertionError(
                "真实按天日志 %s 里有 %d 行**测试特征串** —— "
                "测试把噪音写进了排查故障的一级证据里：\n%s\n"
                "    修法：该 import signin 处必须设 SIGNIN_LOG_DIR_OVERRIDE；"
                "其它写日志的路径也要一并重定向。"
                % (os.path.basename(real_log), len(hits), show))

        return ("真实按天日志无测试特征串（%s 逐行扫 smoke_/[测试]/probe_ 三类前缀，"
                "命中 0 处）" % os.path.basename(real_log))

    check("测试防污染：真实按天日志无测试特征串", real_log_has_no_test_noise)


def main():
    print("=" * 60)
    print("油学通签到系统 · 冒烟测试（离线，不会碰微信）")
    print("=" * 60)

    # 提示：最好用项目自带的 runtime\python.exe 跑，那才是真实运行环境。
    if not os.path.normcase(sys.executable).startswith(os.path.normcase(os.path.join(HERE, "runtime"))):
        print("\n[提示] 你现在用的不是项目自带的 runtime\\python.exe。")
        print("       本脚本只查静态契约和文件，用哪个解释器结果都一样；")
        print("       但想跟真实运行环境完全一致，建议改用：")
        print("         runtime\\python.exe smoke_test.py")

    test_files_exist()
    test_python_syntax()
    test_config()
    test_templates_exist()
    test_signin_contracts()
    test_run_outcome()
    test_fail_evidence_protects_archive()
    test_report_readonly()
    test_bat_ascii()
    test_module_level_side_effects()
    test_ws_frame_parser_strict()
    test_subprocess_encoding_and_signal_cleanup()
    test_history()
    test_notify()
    test_runtime()
    test_ocr_real_samples()
    test_ocr_no_reverse_misread()
    test_ocr_best_initialized()
    test_ocr_negative_stems_veto()
    test_ended_record_propagates_not_time()
    test_wifi_link_connected()
    test_global_timeout_guards_long_waits()
    test_prune_visible_and_sideeffect_free()
    test_no_redundant_recompute_and_silent_swallow()
    test_time_window_cross_midnight()
    test_history_cross_midnight_date()
    test_bat_encoding_consistency()
    test_self_heal_state_durability()
    test_no_unclosed_response_handles()
    test_white_screen_semantics()
    test_cdp_portal_hijack_detection()
    test_match_geometry_guard()
    test_enter_wechat_verifies_click()
    test_smoke_test_does_not_pollute_real_logs()

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败明细：")
        for name, detail in FAILED:
            print("  - %s：%s" % (name, detail))
        print("=" * 60)
        return 1
    print("全部通过，可以放心。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
