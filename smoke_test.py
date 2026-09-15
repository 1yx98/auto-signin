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
import json
import os
import re
import shutil
import sys
import tempfile
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
