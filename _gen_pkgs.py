# -*- coding: utf-8 -*-
"""生成 3 个可拆卸补丁包（缺陷 Z / P / E）。只生成文件，不安装。"""
import os, json, shutil

ROOT = r'D:\签到系统'
SRC_ENGINE = os.path.join(ROOT, '补丁包', 'patch_engine.py')
SRC_TEST = os.path.join(ROOT, '补丁包', 'test_patch_engine.py')

# ---------------------------------------------------------------- 补丁定义
# ============ 包1：僵尸窗口过滤（根因）============
Z1 = {
    "title": "注入 _win_is_zombie() 僵尸窗口识别函数（Z2 依赖）",
    "why": "微信退出/崩溃后窗口对象会残留：可枚举、GetWindowRect 有效，"
           "但 IsWindowVisible 恒 0 且任何激活手段都无效（进程已死）。"
           "本项目 2026-09-16 首次查清、2026-09-17 再次复现。",
    "risk": "极低（纯新增函数，不被调用时完全无影响）",
    "op": "insert_before_function",
    "function": "find_wins",
    "idempotent_marker": "def _win_is_zombie(",
    "expected_delta_lines": 54,
    "signatures": ["def _win_is_zombie(", "补丁 Z1"],
    "new": '''# ===== 补丁 Z1（2026-09-17）：僵尸窗口识别 =====
def _win_is_zombie(hwnd):
    """判断窗口是否为「僵尸」—— 拥有它的进程已退出，只剩窗口对象残留。

    【为什么需要·2026-09-17 真实事故】
    微信退出后其顶层窗口对象会在系统里残留：可以枚举、GetWindowRect 仍返回
    有效坐标（看起来很正常），但 IsWindowVisible 恒为 0，且**任何激活手段都无效**
    —— 因为进程已死，没有任何人处理窗口消息。

    实测因果链（09-17 21:22 那两次失败）：
      find_wins 找到僵尸窗口（它 rect=1118x1715、面积大，按面积排序排第一）
        -> activate 选中它 -> ShowWindow/SetForegroundWindow 全无效
        -> 真正的可用窗口（登录页 586x773）从未被激活 -> 点击落空
        -> 搜索框匹配失败 -> 重启微信 -> 僵尸仍在枚举列表里 -> 死循环
        -> 用户看到「任务栏反复弹窗」

    【判据】OpenProcess + GetExitCodeProcess != STILL_ACTIVE。

    【★ 取不到信息时一律按「活着」处理（返回 False）】
    这是本项目铁律「读不到 != 坏的」：宁可留下一个可疑窗口，
    也绝不因为权限/异常把正常窗口误判成僵尸而丢掉它。
    """
    try:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        _h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not _h:
            # ★★ 2026-09-18 修正·实测发现的真 bug
            # 死进程的 pid 调 OpenProcess 会**失败**，错误码 = 87
            # (ERROR_INVALID_PARAMETER = 该 pid 不存在)。
            # 而僵尸窗口的定义**恰恰就是“拥有它的进程已经死了”**。
            # 原来这里一律 return False（按活着处理），后果是
            # **僵尸永远判不出来** —— 整个 Z 补丁等于失效。
            # 现在按错误码区分：
            _err = kernel32.GetLastError()
            if _err == 87:      # 该 pid 不存在 -> 进程已退出 -> 僵尸
                return True
            return False        # 其它（如 5=权限不足）-> 不敢断言 -> 按活着处理
        try:
            _code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(_h, ctypes.byref(_code)):
                return False
            return _code.value != 259    # 259 = STILL_ACTIVE
        finally:
            kernel32.CloseHandle(_h)
    except Exception:
        return False
''',
}

Z2 = {
    "title": "find_wins() 过滤僵尸窗口（不再选中已死进程的残留窗口）",
    "why": "僵尸窗口 rect 大，在 activate 的按面积排序里总是排第一，"
           "于是每次都被选中、每次激活都失败。必须在**进入候选列表之前**就排除掉。",
    "risk": "低（只新增一个过滤分支；_win_is_zombie 取不到信息时按活着处理，不会误杀）",
    "op": "replace_lines",
    "function": "find_wins",
    "find": "out.append((h, t))",
    "expected_delta_lines": 5,
    "signatures": ["_win_is_zombie(h)", "跳过僵尸窗口", "补丁 Z2"],
    "new": '''            # 【补丁 Z2·2026-09-17】进入候选列表前排除僵尸窗口。
            if _win_is_zombie(h):
                logger.warning(f"[窗口] 跳过僵尸窗口 hwnd={h} {t!r}"
                               f"（拥有它的进程已退出，窗口对象残留，激活必然失败）")
                continue
            out.append((h, t))''',
}

# ============ 包2：进程检测（噪音源）============
P1 = {
    "title": "注入 _tasklist_has() 进程探测辅助函数（P2 依赖）",
    "why": "原 wechat_running() 用一次 tasklist 的结果直接当结论，"
           "且把「检测失败」与「真的没运行」压成同一个 False。",
    "risk": "极低（纯新增函数）",
    "op": "insert_before_function",
    "function": "wechat_running",
    "idempotent_marker": "def _tasklist_has(",
    "expected_delta_lines": 23,
    "signatures": ["def _tasklist_has(", "补丁 P1"],
    "new": '''# ===== 补丁 P1（2026-09-17）：进程探测辅助 =====
def _tasklist_has(imagename, timeout=15):
    """查 tasklist 里是否有该进程。返回 (是否在, 是否成功查到)。

    【为什么要把「查到没在」和「没查成」分开】原 wechat_running() 只有
    一个 bool 返回值，于是「tasklist 超时/编码异常」和「微信真的没运行」
    被压成同一个 False。两者后果完全不同：
      · 真的没运行   -> 应该启动微信
      · 没查成       -> **不该**贸然启动（微信可能正在运行，启动多余实例
                        会制造任务栏弹窗噪音，正是用户报的「任务栏问题」）
    """
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {imagename}"],
                           capture_output=True, text=True, encoding="gbk",
                           errors="ignore", timeout=timeout)
        return (imagename in (r.stdout or "")), True
    except Exception as _e:
        logger.warning(f"[进程] 查询 {imagename} 失败（不代表它没运行）: {_e}")
        return False, False
''',
}

P2 = {
    "title": "wechat_running() 区分「没运行」与「没查成」，避免误启动多余实例",
    "why": "查不到进程时不该直接启动微信：如果微信其实在运行，多启动一个实例"
           "会制造任务栏弹窗噪音（用户 09-16 / 09-17 两次报的「任务栏问题」）。"
           "改为：查成且确实没有 -> 启动；没查成 -> 用「窗口是否存在」交叉验证，"
           "仍不能确定就**不启动**、如实告警。",
    "risk": "低（只在「检测失败」这条原本就有缺陷的分支上改变行为）",
    "op": "replace_lines",
    "function": "wechat_running",
    "find": 'return "Weixin.exe" in (r.stdout or "")',
    "expected_delta_lines": 13,
    "signatures": ["补丁 P2", "不启动新实例"],
    "new": '''        # 【补丁 P2·2026-09-17】三态处理：查到有 / 查到没有 / 根本没查成
        _found, _ok = _tasklist_has("Weixin.exe")
        if _ok:
            return _found
        # 没查成 -> 用「窗口是否存在」交叉验证。注意：僵尸窗口也算「有窗口」，
        # 但这里只关心「要不要启动微信」，保守不启动更安全。
        _has_win = bool(find_wins("微信")) or bool(find_wins("Weixin"))
        if _has_win:
            logger.warning("[进程] tasklist 查不到微信，但存在微信窗口 —— "
                           "不启动新实例（避免制造多余弹窗）")
            return True
        logger.warning("[进程] 无法确定微信是否在运行，且未见微信窗口 —— "
                       "按未运行处理，交由 start_wechat 兜底")
        return False''',
}

# ============ 包3：进入微信判定（误判）============
E1 = {
    "title": "「进入微信」匹配失败时不再谎报「已在主界面」",
    "why": "match() 在「命中点越界/完全匹配不到」时返回 conf=-1.0。"
           "原代码把 conf<0.8 一律当成「没有确认页 -> 已在主界面」，"
           "于是窗口明明不可见、页面停在登录页，脚本却带着错误认知一路往下走，"
           "直到搜索框连续失败才暴露（而且报的是「窗口状态坏了」）。",
    "risk": "低（只改一行日志与其前的判定；不改变点击/流程走向）",
    "op": "replace_lines",
    "function": "step_enter_wechat",
    "find": 'logger.info("[进入微信] 已在主界面，无需确认")',
    "expected_delta_lines": 11,
    "signatures": ["补丁 E1", "不能据此断定"],
    "new": '''        # 【补丁 E1·2026-09-17】conf < 0 是「匹配不到/命中点越界」，
        # **不等于**「页面上没有确认页按钮」。原来直接把两者当同一件事，
        # 会谎报「已在主界面」，掩盖真实现场（09-17 21:22 实测：
        # 报「已在主界面」时页面其实停在登录页，且窗口 IsWindowVisible=0）。
        if c < 0:
            _vw = activate("微信", exact=True)
            _vis = bool(_vw) and user32.IsWindowVisible(_vw)
            logger.warning(f"[进入微信] 确认页按钮匹配失败(conf={c:.3f}) —— "
                           f"不能据此断定「已在主界面」；当前微信窗口可见={_vis}"
                           + ("（窗口不可见，后续匹配很可能失败）" if not _vis else ""))
        else:
            logger.info("[进入微信] 已在主界面，无需确认")''',
}

PACKAGES = [
    ("僵尸窗口过滤补丁包", {"Z1": Z1, "Z2": Z2},
     "过滤掉「进程已退出、窗口对象残留」的僵尸窗口，从根上消除激活失败与重启循环。"),
    ("进程检测补丁包", {"P1": P1, "P2": P2},
     "把「进程没运行」与「进程没查成」分开，避免误启动多余微信实例制造任务栏弹窗。"),
    ("进入微信判定补丁包", {"E1": E1},
     "匹配失败时不再谎报「已在主界面」，让真实现场（窗口不可见）如实暴露。"),
]

def make(name, patches, desc):
    d = os.path.join(ROOT, name)
    os.makedirs(os.path.join(d, "patches"), exist_ok=True)
    os.makedirs(os.path.join(d, "_backup"), exist_ok=True)

    # 引擎：改基线备份名为 .base（与「跳过无效滚动补丁包」一致，因为不是原版）
    eng = open(SRC_ENGINE, 'rb').read().decode('utf-8')
    eng = eng.replace('signin.py.orig', 'signin.py.base')
    open(os.path.join(d, "patch_engine.py"), 'wb').write(eng.encode('utf-8'))

    t = open(SRC_TEST, 'rb').read().decode('utf-8')
    t = t.replace('signin.py.orig', 'signin.py.base')
    open(os.path.join(d, "test_patch_engine.py"), 'wb').write(t.encode('utf-8'))

    for pid, p in patches.items():
        q = dict(p); q["id"] = pid
        open(os.path.join(d, "patches", f"{pid}.json"), 'w', encoding='utf-8').write(
            json.dumps(q, ensure_ascii=False, indent=2))

    print(f"[生成] {name}  ({len(patches)} 个补丁)  —— {desc}")

for name, patches, desc in PACKAGES:
    make(name, patches, desc)
print("\n全部生成完毕（尚未安装）")
