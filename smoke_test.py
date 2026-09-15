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
import os
import re
import shutil
import sys
import tempfile
import textwrap
import traceback

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

    # 台账写入必须被 try 包着（失败不能影响主流程）
    check("台账调用来自 history 模块", lambda: "import history" in src or "history as" in src or (_ for _ in ()).throw(
        AssertionError("signin.py 没有导入 history 模块")))

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
    test_report_readonly()
    test_bat_ascii()
    test_history()
    test_notify()
    test_runtime()
    test_ocr_real_samples()
    test_ocr_no_reverse_misread()
    test_ocr_best_initialized()
    test_ocr_negative_stems_veto()

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
