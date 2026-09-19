# -*- coding: utf-8 -*-
"""生成补丁包 6（不在区域内重跑）与 7（记录日期判定）。只生成文件，不安装。"""
import os, json

ROOT = r'D:\签到系统'
SRC_ENGINE = os.path.join(ROOT, '补丁包', 'patch_engine.py')
SRC_TEST = os.path.join(ROOT, '补丁包', 'test_patch_engine.py')

# ============================================================ 包6：不在区域内
N1 = {
    "title": "注入 _is_not_in_area() 识别「不在区域内」（N2 依赖）",
    "why": "地图页定位漂移时底部按钮是灰色「不在区域内」，与「已结束」同为灰宽按钮，"
           "阶段A 无法区分，于是把'定位漂移'误报成'今天没推送'。",
    "risk": "极低（纯新增函数；读不到/异常一律返回 False，行为完全退化为改动前）",
    "op": "insert_before_function",
    "function": "click_sign_button",
    "idempotent_marker": "def _is_not_in_area(",
    "expected_delta_lines": 43,
    "signatures": ["def _is_not_in_area(", "补丁 N1"],
    "new": '''# ===== 补丁 N1（2026-09-17）：识别「不在区域内」 =====
def _is_not_in_area(hwnd, btns, full=None):
    """判断底部那个灰色宽按钮是不是「不在区域内」（= 地图页 + 定位漂移）。

    【为什么需要·用户 2026-09-17 明确要求】
    地图页在「定位漂到校外」时，底部按钮由绿变灰、文字变成「不在区域内」。
    而 click_sign_button() 阶段A 的判据只看有没有蓝/绿/灰按钮 ——
    一个灰色宽按钮会被当成「未到签到时段/已结束」，**直接判 not_time 并 return**。

    后果有两层（都很严重）：
      ① 明明只是定位漂移，却报成「今天没推送」——**误报正常状态**（违反铁律 R14）。
         用户看台账会以为今天没推送；自愈不记故障；飞书也报「并非失败」。
      ② 外层重试循环看到 not_time 就 break（日志原话「重试无意义」），
         **连"重新跑一遍流程"的机会都没有**。
         用户原话：「如果遇到不在区域内重新跑一遍流程，然后去签到」。

    【判据】OCR 读该灰按钮文字，命中「不在区域内/未在区域/不在区域」即认定。
    **读不到 / 引擎不可用 / 任何异常 -> 一律返回 False**（铁律 R7：读不到 != 坏的），
    这样行为**完全退化为改动前**，绝不引入新的误判。
    """
    try:
        if full is None or not btns:
            return False
        _grays = [b for b in btns if b.get("kind") == "gray"]
        if not _grays:
            return False
        _b = max(_grays, key=lambda z: z.get("w", 0) * z.get("h", 0))
        _t = (ocr_button_text(full, _b) or "").replace(" ", "")
        if not _t:
            return False
        for _kw in ("不在区域内", "未在区域", "不在区域"):
            if _kw in _t:
                logger.warning(f"[详情页] 灰按钮文字读到「{_t[:20]}」-> 判定为地图页定位漂移")
                return True
        return False
    except Exception as _e:
        logger.warning(f"[详情页] 判断「不在区域内」失败（按'不是'处理，照旧走原逻辑）: "
                       f"{type(_e).__name__}: {_e}")
        return False
''',
}

N2 = {
    "title": "阶段A 区分「定位漂移」与「未到时段」：前者不判 not_time，转入定位自愈",
    "why": "两者都是灰色宽按钮但含义完全不同 —— 定位漂移应该重试/自愈，"
           "只有真的未到时段/已结束才该收工。原代码一律判 not_time，"
           "既掩盖了故障、又跳过了本该跑的定位自愈（重新定位/重连 WiFi）。",
    "risk": "低（只有 OCR 明确读到「不在区域内」才改变走向；其余情况逐字等价于原逻辑）",
    "op": "replace_lines",
    "function": "click_sign_button",
    "find": "if not green and gray:",
    "expected_delta_lines": 9,
    "signatures": ["补丁 N2", "转入定位自愈"],
    # ★ 反向校验：旧那一行必须消失（只加条件、不改函数体，所以旧行结尾的冒号没了）
    "absent_signatures": ["if not green and gray:\n"],
    "new": '''        # 【补丁 N2·2026-09-17】先分辨「地图页定位漂移」与「未到时段/已结束」——
        # 两者都是灰色宽按钮，含义却完全不同：前者应重试/自愈，后者才该收工。
        # 这里**只加条件、不动原函数体**，避免"旧代码残留在后面"（见 absent_signatures）。
        _not_in_area = (not green and bool(gray)
                        and _is_not_in_area(h, btns, full=(_cap_last[0] if _cap_last else None)))
        if _not_in_area:
            logger.warning("[详情页] 检测到灰色「不在区域内」—— 已在地图页但定位漂移，"
                           "**不判 not_time**；转入定位自愈（重新定位 / 重连 WiFi），"
                           "仍不行会返回失败并由外层完整重跑一遍流程")
        if not green and gray and not _not_in_area:''',
}

N3 = {
    "title": "放行「不在区域内」走到阶段B（而不是判 fail）",
    "why": "定位漂移时 green 也是 False。若不改这一行，N2 放行后会立刻落到 "
           "`if not green:` 被判 fail —— 虽然比 not_time 诚实，但白白浪费了"
           "阶段B 本来就有的定位自愈（重新定位 + 重连 WiFi）。",
    "risk": "低（`_not_in_area` 为 False 时该行逐字等价于原来的 `if not green:`）",
    "op": "replace_lines",
    "function": "click_sign_button",
    "find": "if not green:",
    "expected_delta_lines": 0,
    "signatures": ["if not green and not _not_in_area:"],
    "absent_signatures": ["if not green:\n"],
    "new": "        if not green and not _not_in_area:",
}

N4 = {
    "title": "阶段B 的日志如实说明是「不在区域内」而不是「有绿色按钮」",
    "why": "原日志无条件说「但有绿色按钮」，而定位漂移时并没有绿色按钮 —— "
           "会误导以后看日志排查的人（本项目踩过多次'日志与实际不符'的坑）。",
    "risk": "极低（纯日志文案，不改变任何流程）",
    "op": "replace_lines",
    "function": "click_sign_button",
    "find": 'logger.info("[详情页] 未见蓝色签到但有绿色按钮，判断已在地图定位页，直接进入阶段B")',
    "expected_delta_lines": 2,
    "signatures": ["走定位自愈"],
    "absent_signatures": ["但有绿色按钮，判断已在地图定位页"],
    "new": '''        logger.info("[详情页] 未见蓝色签到但已在地图页（"
                    + ("灰色「不在区域内」-> 走定位自愈" if _not_in_area else "有绿色按钮")
                    + "），直接进入阶段B")''',
}

# ============================================================ 包7：记录日期判定
D1 = {
    "title": "注入 _detail_record_is_today() 读页面日期（D2 依赖）",
    "why": "详情页会显示「签到时间：2026-09-17 21:25」——日期是比'用时间窗反推'"
           "可靠得多的证据。",
    "risk": "极低（纯新增函数；读不到/异常一律返回 False，行为退化为改动前）",
    "op": "insert_before_function",
    "function": "click_sign_button",
    "idempotent_marker": "def _detail_record_is_today(",
    "expected_delta_lines": 60,
    "signatures": ["def _detail_record_is_today(", "补丁 D1"],
    "new": '''# ===== 补丁 D1（2026-09-17）：读页面日期判断记录是不是今天的 =====
def _detail_record_is_today(hwnd, full=None):
    """读详情页的「签到时间」，判断这条记录是不是**今天**的。

    【为什么需要·2026-09-17 实测】
    原来那个守卫的前提是「不在签到时段内的灰色'已签到'记录必然不是今天的」。
    **这个前提不成立** —— 实测 2026-09-17 21:57：
    用户在 21:25 自己手动签了（重复签到场景），脚本 21:57 才跑，
    页面上明明就是**今天**的「已签到」，却被判成 not_time。
    后果：台账把「今天已签到」记成 not_time —— 统计失真、用户以为没签上。

    **更直接的证据就在页面上**：详情页显示
        「签到时间：2026-09-17 21:25」
    —— 日期比「用时间窗反推」可靠得多（README 早已记下这条）。

    【判据】OCR 整窗 -> 找「签到时间」之后的 8 位数字（YYYYMMDD）-> 与今天比较。
    **读不到 / 引擎不可用 / 异常 -> 返回 False**（铁律 R7），行为退化为改动前。
    """
    try:
        if full is None or hwnd is None:
            return False
        # ★ 必须只 OCR 小程序窗口那一块，不能整屏 OCR。
        # 2026-09-18 用真实样本离线验证发现：整屏 OCR 会把任务栏 / 资源管理器
        # 的文字一起读进来，导致「签到时间」那一行被读错
        # （实测同一张样本读成 520260972125，多出一位 5，日期就取错了）。
        # 裁出窗口后同一张样本读作 202609172125 ✅
        _wl, _wt, _wr, _wb = win_rect(hwnd)
        if _wl <= -30000 or _wt <= -30000:
            return False
        _HH, _WW = full.shape[:2]
        _wl, _wt = max(0, _wl), max(0, _wt)
        _wr, _wb = min(_WW, _wr), min(_HH, _wb)
        if _wr - _wl < 100 or _wb - _wt < 100:
            return False
        _win = full[_wt:_wb, _wl:_wr]
        _eng = _ocr_get_engine()
        if _eng is None:
            return False
        _txt = (_ocr_read(_eng, _win) or "").replace(" ", "")
        if "签到时间" not in _txt:
            return False
        _seg = _txt.split("签到时间", 1)[1]
        _digits = "".join(ch for ch in _seg if ch.isdigit())
        if len(_digits) < 8:
            return False
        _ymd = _digits[:8]
        _today = datetime.now().strftime("%Y%m%d")
        if _ymd == _today:
            logger.info(f"[详情页] 页面「签到时间」读到 {_ymd} == 今天 -> 这条记录就是今天的")
            return True
        logger.info(f"[详情页] 页面「签到时间」读到 {_ymd} != 今天({_today}) -> 不是今天的记录")
        return False
    except Exception as _e:
        logger.warning(f"[详情页] 读取页面日期失败（按'不是今天'处理，照旧走原逻辑）: "
                       f"{type(_e).__name__}: {_e}")
        return False
''',
}

D2 = {
    "title": "时段外的「已签到」先用页面日期核实：是今天就判成功，不再误报 not_time",
    "why": "原守卫只用时间窗反推，遇到「用户自己在时段内手动签、脚本在时段外才跑」"
           "就会把今天的成功记录误报成 not_time。改成先读页面日期。",
    "risk": "低（读不到日期时 `_rec_today=False`，该行逐字等价于原条件）",
    "op": "replace_lines",
    "function": "click_sign_button",
    "find": "if finish_clicks == 0 and not _within_signin_window():",
    "expected_delta_lines": 4,
    "signatures": ["补丁 D2", "_rec_today"],
    "absent_signatures": ["if finish_clicks == 0 and not _within_signin_window():\n"],
    "new": '''            # 【补丁 D2·2026-09-17】用页面上的日期做**更直接的判据**：
            # 若「签到时间」就是今天，说明这条记录是今天的 -> 直接判成功，
            # 不再因为"超出脚本时段"而误报 not_time。
            _rec_today = _detail_record_is_today(h, (_cap_last[0] if _cap_last else None))
            if finish_clicks == 0 and not _within_signin_window() and not _rec_today:''',
}

PACKAGES = [
    ("不在区域内重跑补丁包", {"N1": N1, "N2": N2, "N3": N3, "N4": N4},
     "「不在区域内」不再判 not_time，转入定位自愈；失败则由外层完整重跑。"),
    ("记录日期判定补丁包", {"D1": D1, "D2": D2},
     "读页面「签到时间」的日期，根治'今天已签到被误报成 not_time'。"),
]


def make(name, patches):
    d = os.path.join(ROOT, name)
    os.makedirs(os.path.join(d, "patches"), exist_ok=True)
    os.makedirs(os.path.join(d, "_backup"), exist_ok=True)
    eng = open(SRC_ENGINE, 'rb').read().decode('utf-8').replace('signin.py.orig', 'signin.py.base')
    open(os.path.join(d, "patch_engine.py"), 'wb').write(eng.encode('utf-8'))
    t = open(SRC_TEST, 'rb').read().decode('utf-8').replace('signin.py.orig', 'signin.py.base')
    open(os.path.join(d, "test_patch_engine.py"), 'wb').write(t.encode('utf-8'))
    for pid, p in patches.items():
        q = dict(p); q["id"] = pid
        open(os.path.join(d, "patches", pid + ".json"), 'w', encoding='utf-8').write(
            json.dumps(q, ensure_ascii=False, indent=2))
    print("[生成] %s (%d 个补丁)" % (name, len(patches)))


for nm, ps, _desc in PACKAGES:
    make(nm, ps)
print("\n生成完毕（尚未安装）")
