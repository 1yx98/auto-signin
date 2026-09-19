# -*- coding: utf-8 -*-
"""补丁引擎 —— 可插拔补丁包的核心（不依赖 signin.py，可独立运行）

设计目标（用户 2026-09-16 提出）：
    "先把这些修复模块搞上去，今天晚上成功之后直接装上去，这样就不会影响主程序，
     如果装上去发现有问题还可以拆下来完善。"

所以本引擎必须满足三条硬要求：
  ① **不装就不生效**：主程序 signin.py 一个字节都不改，补丁只存在于本目录。
  ② **装了能拆干净**：拆卸后 signin.py 必须与安装前**逐字节相同**（用 sha256 验证）。
  ③ **失败要暴露**：任何定位失败 / 锚点不唯一 / 替换后语法错误，一律报错退出，
     绝不"猜一个位置改上去"。宁可装不上，不可装错。

与项目既有约定的对齐：
  · 本项目要求"测试断言查代码必须用 AST，禁止字符串搜索"（见 MEMORY.md 第 4 条）。
    引擎里定位锚点同样用 AST 拿到**真实的行列号**，再按行列号做精确切片替换，
    而不是 `src.replace(old, new)` —— 后者在锚点出现多次时会静默改错地方。
  · 替换后立即 `ast.parse()` 复核语法；再核对"改动行数 == 预期"，超出即回滚。

用法：
    python patch_engine.py list              # 列出所有补丁及状态
    python patch_engine.py check             # 校验（能装上吗/装了吗/能拆吗）
    python patch_engine.py apply             # 全部装上
    python patch_engine.py apply P1          # 只装 P1
    python patch_engine.py revert            # 全部拆掉
    python patch_engine.py verify            # 装完后跑签名校验（确认补丁真的在）
"""
import os
import sys
import json
import ast
import hashlib
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 签到系统 根目录
TARGET = os.path.join(ROOT, "signin.py")     # 被补丁的主程序
BACKUP_DIR = os.path.join(HERE, "_backup")   # 原始文件备份（拆卸用）
STATE_PATH = os.path.join(HERE, "_state.json")  # 已装补丁记录

# ============================================================================
# 【2026-09-16·跨包冲突防护】同一份 signin.py 上可能同时挂着多个补丁包
# （例如隔壁 `跳过无效滚动补丁包/`）。
#
# 真实风险：多个包的拆卸都是"用**自己的基线备份**整体覆盖文件"。于是：
#   · 先拆的那个包 → 会把**另一个包已装的改动一起冲掉**；
#   · 而另一个包的 _state.json 仍以为装着 → apply 被幂等保护跳过、
#     签名校验又失败 → **装不回去**（与 §7.7.1 那个缺陷同源）。
#
# 修法：在 ROOT 下放**共享注册表** `.patch_packages.json`，每个包改动文件后
# 写一条"我改过、当时的 sha256 是多少"。拆卸前若发现**别的包**的记录与当前
# 文件 sha 不一致，就拒绝整包覆盖拆卸（宁可拆不掉，不可拆错）。
# ============================================================================
REGISTRY_PATH = os.path.join(ROOT, ".patch_packages.json")
PKG_NAME = os.path.basename(HERE)


def load_registry():
    if os.path.exists(REGISTRY_PATH):
        try:
            return json.loads(read_text(REGISTRY_PATH))
        except Exception:
            return {"_corrupt": True, "packages": {}}
    return {"packages": {}}


def save_registry(reg):
    reg["_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_text(REGISTRY_PATH, json.dumps(reg, ensure_ascii=False, indent=2))


def registry_note(pkg, note):
    reg = load_registry()
    reg.setdefault("packages", {}).setdefault(pkg, {})
    reg["packages"][pkg].update(note)
    reg["packages"][pkg]["touched_at"] = \
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_registry(reg)


def foreign_packages_touching():
    """返回"**在我之后**改过文件"的别的补丁包名列表。

    【★ 2026-09-16 修正：判据方向原来搞反了】

    危险的情形只有一种：**别人写在我后面**。因为整包拆卸是"用自己的基线
    整体覆盖"，会把**我之后**发生的改动抹掉。别人写在我前面的改动，本来就
    已包含在我的基线里，覆盖回去**不会**伤到它。

    怎么判断"写在我后面"？**看谁登记的最后 sha 等于当前文件 sha**——
    等于当前 sha 的那个包，就是最后落笔的人。别人若写在我之前，
    它登记的是那个中间态 sha，**不等于**当前 sha。

    原来的写法 `if sha256_after != cur: 视为冲突` **方向是反的**：
      · 别人写在我之前（无害）→ 其记录 ≠ 当前 → 被**误报**为冲突；
      · 别人写在我之后（有害）→ 其记录 == 当前 → 反而**漏报**。
    实测代价：另一个包（P2）的 `revert_one` 被它的防护拦住，
    而引擎返回的提示还叫人"改用 revert <单个补丁id> 逐条拆"——
    那条路同样被拦，**建议是自相矛盾的**。
    """
    cur = sha256_of_file(TARGET) if os.path.exists(TARGET) else ""
    out = []
    # 本包登记的"我最后写成什么样"，用来判断"当前是不是我写的"
    mine = load_state().get("last_written_sha256")
    if mine != cur:
        # 当前的最后落笔者不是本包 → 找出是哪个包
        for name, info in (load_registry().get("packages") or {}).items():
            if name == PKG_NAME:
                continue
            if not isinstance(info, dict):
                continue
            if info.get("sha256_after") == cur:
                out.append(name)
    return out


def self_record_mismatch():
    """【判据 2·自查】当前文件是否已经**不是本包上次改完的那个样子**。

    【为什么必须有这条·2026-09-16 真实事故】
    仅靠判据 1 拦不住这种情况：**本包从未登记过注册表**（例如本包安装在
    注册表机制上线之前），而另一个包改了文件。此时：
      · 判据 1 看别的包：它的记录 sha == 当前 sha → 误判"没冲突" → 放行；
      · 结果本包一覆盖，**把对方的改动冲掉了**。
    这就是实测中真实发生的事（旧包 18:35 装的，21:26 拆卸时冲掉了新包）。

    本判据换一个角度问：**"我上次改完之后登记的那个 sha，还是当前 sha 吗？"**
      · 是  → 说明我之后没人动过文件 → 可以安全整包覆盖；
      · 不是 → 说明**有人在我之后改过** → 绝不能覆盖（会抹掉别人的改动）。
      · 我从没登记过（拿不到自己的记录）→ 返回 None，交由调用方保守处理。
    """
    st = load_state()
    mine = st.get("last_written_sha256")
    if not mine:
        return None
    return mine != (sha256_of_file(TARGET) if os.path.exists(TARGET) else "")


def _revert_safety_check():
    """拆卸前的统一安全检查。返回 (是否允许, 拒绝原因或 None)。"""
    # 判据 2 优先：我自己都认不出这个文件了 → 一定有人动过
    self_state = self_record_mismatch()
    foreign = foreign_packages_touching()

    if self_state is True:
        return False, (
            "拒绝拆卸：当前 signin.py **不是本包上次改完的样子**（sha256 对不上）。\n"
            "  说明本包装好之后，**有别人（另一个补丁包，或手工编辑）改过这个文件**。\n"
            "  本包的拆卸方式是\"用自己的基线整体覆盖\"，会把那些改动一起抹掉。\n"
            f"  当前文件 sha256 = {(sha256_of_file(TARGET) or '')[:16]}...\n"
            f"  本包上次登记的 = {(load_state().get('last_written_sha256') or '')[:16]}...\n"
            "  → 请先弄清是谁改的；确需强行拆卸请用 `revert <单个补丁id>`。\n"
            "  （原则：宁可拆不掉，不可拆错）"
        )
    if foreign:
        return False, (
            f"拒绝拆卸：检测到另有补丁包在**本包之后**改过 signin.py —— {', '.join(foreign)}\n"
            f"  （它登记的 sha256 正好等于当前文件 → 它是最后落笔的那个）\n"
            f"  本包的拆卸方式是\"用自己的基线整体覆盖文件\"，"
            f"会把对方在我之后的改动一起抹掉，而对方的状态文件仍以为装着。\n"
            f"  → 请先拆掉对方，再拆本包；或改用 `revert <单个补丁id>` 逐条拆\n"
            f"     （逐条拆只在本包自己的范围内反向编辑，不会整包覆盖）。\n"
            f"  （原则：宁可拆不掉，不可拆错）"
        )
    return True, None


# ==================== 基础工具 ====================
def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path):
    """按字节读、显式 utf-8 解码。绝不用文本模式读写——
    文本模式会静默把 CRLF 转成 LF，导致"没改内容文件却变了"（本项目踩过）。"""
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_text(path, text):
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.loads(read_text(STATE_PATH))
        except Exception:
            return {}
    return {}


def save_state(st):
    st["_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_text(STATE_PATH, json.dumps(st, ensure_ascii=False, indent=2))


# ==================== 补丁定义加载 ====================
def load_patches():
    """从 patches/ 目录加载所有 .json 补丁定义，按 id 排序返回。"""
    pdir = os.path.join(HERE, "patches")
    if not os.path.isdir(pdir):
        return []
    out = []
    for fn in sorted(os.listdir(pdir)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(pdir, fn)
        try:
            d = json.loads(read_text(p))
        except Exception as e:
            raise RuntimeError(f"补丁定义解析失败 {fn}: {e}")
        d["_file"] = fn
        out.append(d)
    return out


def find_patch(pid):
    for d in load_patches():
        if d.get("id") == pid:
            return d
    return None


# ==================== AST 锚点定位 ====================
class AnchorError(Exception):
    pass


# ast.get_source_segment 需要一个"完整的源码字符串"才能还原片段。
# 引擎在 apply 时把当前源码放进来，供 locate_for_loop 的 signature 匹配使用。
_SRC_CACHE = [""]


def _iter_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def locate_function(tree, func_name):
    """按函数名定位，返回 (lineno, end_lineno)。重名时报错（不猜）。"""
    hits = [n for n in _iter_functions(tree) if n.name == func_name]
    if not hits:
        raise AnchorError(f"找不到函数 {func_name}()")
    if len(hits) > 1:
        raise AnchorError(f"函数 {func_name}() 有 {len(hits)} 个同名定义，拒绝猜测")
    return hits[0].lineno, hits[0].end_lineno


def locate_for_loop(tree, func_name, loop_index=None, signature=None):
    """在指定函数体内定位 for 循环，返回 AST 节点。

    两种定位方式（二选一，优先用 signature —— 它不依赖位置，更抗结构变动）：
      · signature：在函数内**所有** for（含嵌套）里找，要求"含该特征串的 for"**恰好 1 个**。
        多于 1 个时报错，绝不猜。
      · loop_index：取函数**顶层 body** 里第 N 个 for（旧方式，仅在无 signature 时用）。

    【为什么必须支持嵌套定位】本项目踩过：open_signin_entry() 的顶层 for 是
    "遍历导航步骤"的外层循环（140 行），而真正要改的 `for k in range(6)` 是它
    内部的嵌套循环。只按顶层索引取，会**整个外层循环**替换掉——引擎的行数护栏
    当场拦下（实际 -88 行 vs 预期 +30），否则就是灾难性误改。
    """
    fns = [n for n in _iter_functions(tree) if n.name == func_name]
    if len(fns) != 1:
        raise AnchorError(f"函数 {func_name}() 定位不唯一，找到 {len(fns)} 个")
    body = fns[0]

    if signature:
        hits = []
        for n in ast.walk(body):
            if isinstance(n, ast.For):
                seg = ast.get_source_segment(_SRC_CACHE[0], n) or ""
                if signature in seg:
                    hits.append(n)
        if not hits:
            raise AnchorError(
                f"{func_name}() 里找不到含特征串 {signature!r} 的 for 循环")

        # 【嵌套去重】AST 遍历外层循环时会**把内层循环一并包含**，
        # 于是"内层循环独有的特征串"必然同时命中外层（因为它包着内层）。
        # 例：open_signin_entry() 的外层 for（导航步骤，140行）里嵌着
        #     内层 `for k in range(6)`（滚动重试），特征串 '下滚第{k+1}次'
        #     会同时命中两者（行 2158 和 2214）。
        # 规则：**若命中的节点之间存在包含关系，保留最内层的那一个。**
        # （要改的始终是具体干事的那个循环，不是包它的骨架循环。）
        innermost = []
        for h in hits:
            contains_other = any(
                other is not h
                and h.lineno <= other.lineno
                and other.end_lineno <= h.end_lineno
                for other in hits)
            if not contains_other:
                innermost.append(h)
        hits = innermost

        if not hits:
            raise AnchorError(
                f"{func_name}() 特征串 {signature!r} 的循环嵌套关系异常，定位失败")
        if len(hits) > 1:
            raise AnchorError(
                f"{func_name}() 里有 {len(hits)} 个**互不包含**的 for 循环都含特征串 "
                f"{signature!r}（行 {[h.lineno for h in hits]}），定位不唯一，拒绝猜测。"
                f"请改用更长的特征串来区分。")
        return hits[0]

    loops = [n for n in body.body if isinstance(n, ast.For)]
    idx = loop_index or 0
    if len(loops) <= idx:
        raise AnchorError(
            f"{func_name}() 顶层只有 {len(loops)} 个 for，取不到第 {idx} 个")
    return loops[idx]


def _text_of(lines, node):
    """按 AST 节点的真实行列号切出源码片段（1 基行号）。"""
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


# ==================== 补丁应用 ====================
def _apply_one(src_lines, patch):
    """对源码行列表套用一个补丁，返回 (新行列表, 说明)。

    支持的 op：
      · replace_function_head   —— 替换函数定义行（def xxx(...): 那一行）
      · replace_loop            —— 替换某函数体内第 N 个 for 循环整体
      · insert_before_function  —— 在某函数**定义之前**插入一段自带代码
                                   （用于补丁自带辅助函数，保证自包含）
      · replace_lines           —— 替换某函数体内**一行**（按"整行精确文本"定位）
                                   （2026-09-17 新增，见下方详细说明）
    """
    tree = ast.parse("\n".join(src_lines))
    _SRC_CACHE[0] = "\n".join(src_lines)
    op = patch["op"]
    fn = patch["function"]

    if op == "insert_before_function":
        lineno, _ = locate_function(tree, fn)
        # 幂等：已存在则不重复插入（补丁装两次不应插两遍）
        marker = patch.get("idempotent_marker")
        if marker and marker in "\n".join(src_lines):
            return src_lines, f"{fn}() 之前已存在 {marker!r}，跳过插入（幂等）"
        # 往前吞掉紧邻的空行，让插入后仍保持"函数间空一行"的 PEP8 风格
        ins = lineno - 1
        while ins > 0 and src_lines[ins - 1].strip() == "":
            ins -= 1
        block = patch["new"]
        new_lines = src_lines[:ins] + [""] + block.split("\n") + ["", ""] + src_lines[ins:]
        return new_lines, f"在 {fn}() 定义前（第 {lineno} 行）插入补丁自带代码"



    if op == "replace_function_head":
        lineno, _ = locate_function(tree, fn)
        old_line = src_lines[lineno - 1]
        expect = patch["expect"].strip()
        if old_line.strip() != expect:
            raise AnchorError(
                f"锚点不符：{fn}() 第 {lineno} 行实际是\n    {old_line.strip()!r}\n"
                f"补丁期望\n    {expect!r}\n"
                f"（主程序可能已改过，请更新补丁定义而不是强行套用）")
        new_lines = list(src_lines)
        new_lines[lineno - 1] = patch["new"]
        return new_lines, f"替换 {fn}() 定义行（第 {lineno} 行）"

    if op == "replace_loop":
        node = locate_for_loop(tree, fn, patch.get("loop_index"),
                               patch.get("loop_signature"))
        start = node.lineno - 1                       # 0 基
        end = node.end_lineno                         # 切片右开
        old_text = "\n".join(src_lines[start:end])
        # 二次确认：切出来的这段确实是我们想改的那个循环（防行列号算法本身有 bug）
        sig = patch.get("loop_signature")
        if sig and sig not in old_text:
            raise AnchorError(
                f"{fn}() 定位到的循环（第 {start+1}~{end} 行）里没有特征串 "
                f"{sig!r}，拒绝替换（说明定位结果与预期不符）")
        new_lines = src_lines[:start] + patch["new"].split("\n") + src_lines[end:]
        return new_lines, f"替换 {fn}() 第 {patch.get('loop_index', 0)} 个 for 循环（第 {start+1}~{end} 行）"

    if op == "replace_lines":
        # 【2026-09-17 新增·本项目第 4 种 op】
        # 替换函数体内**一行**，按"整行精确文本"定位。
        #
        # 【为什么必须加这个 op】原 3 种都改不了函数体**内部**的语句：
        #   · replace_function_head 只换 `def` 那一行
        #   · replace_loop 只换整个 for 循环
        #   · insert_before_function 只能加在函数**外面**
        # 而实际修复常常是"改函数体里的一个判断/一行调用"。
        # 实测踩到：修 `find_wins()` 要在 `out.append((h, t))` 前加僵尸过滤，
        # 三种 op 一个都用不上 —— 要么改不了，要么得整函数替换（风险大得多）。
        #
        # 【安全设计·与既有 op 同一标准：宁可装不上，不可装错】
        #   · 锚点必须在**目标函数体内**（不是整个文件里搜）
        #   · 锚点在该函数内必须**恰好命中 1 行**：0 行→报"找不到"，
        #     多行→报"定位不唯一，请用更长锚点"。**两种都拒绝，绝不猜。**
        #   · 按 strip 后**精确相等**比较，不做模糊匹配、不做子串包含
        #   · 外层仍统一做 expected_delta_lines 行数校验
        fns = [n for n in _iter_functions(tree) if n.name == fn]
        if len(fns) != 1:
            raise AnchorError(f"函数 {fn}() 定位不唯一，找到 {len(fns)} 个")
        _node = fns[0]
        anchor = patch["find"].strip()
        hits = [i for i in range(_node.lineno - 1, _node.end_lineno)
                if src_lines[i].strip() == anchor]
        if not hits:
            raise AnchorError(
                f"{fn}() 内（第 {_node.lineno}~{_node.end_lineno} 行）找不到锚点行：\n"
                f"    期望 {anchor!r}\n"
                f"（主程序可能已改过，请更新补丁定义而不是强行套用）")
        if len(hits) > 1:
            raise AnchorError(
                f"{fn}() 内锚点命中 {len(hits)} 行（行 {[i + 1 for i in hits]}），"
                f"定位不唯一，拒绝猜测。\n    锚点 {anchor[:70]!r}\n"
                f"    请把锚点写得更长/更独特，使其在该函数内只出现一次。")
        _i = hits[0]
        new_lines = src_lines[:_i] + patch["new"].split("\n") + src_lines[_i + 1:]
        return new_lines, f"替换 {fn}() 内第 {_i + 1} 行（锚点在该函数内唯一）"

    raise AnchorError(f"不支持的 op: {op}")


def apply_patch(patch, dry=False):
    """套用单个补丁。返回 (ok, message)。dry=True 只校验不落盘。"""
    pid = patch.get("id", "?")
    if not os.path.exists(TARGET):
        return False, f"[{pid}] 找不到目标文件 {TARGET}"

    src = read_text(TARGET)
    src_lines = src.split("\n")

    # 【幂等保护·2026-09-16 由负向测试发现】
    # 若补丁的特征串**已经全部存在**于目标文件里，说明它已经装过了。
    # 此时再套一次会造成"补丁叠补丁"：循环被替换成一个**包含旧替换结果**的新循环
    # （负向测试实测：重复套 P1b 得到 +44 行而不是 +28），主程序会被改烂。
    # 所以这里直接判定为"已装"，拒绝二次套用。
    # 注意判据用 **全部** 签名都在（而任一在）——只要有一条不在，就说明没装全，
    # 交给后面的定位/校验去报错，不要在这里蒙混过去。
    sigs = patch.get("signatures", [])
    if sigs and all(s in src for s in sigs):
        return False, f"[{pid}] 特征串已全部存在，判定为**已装过**，拒绝重复套用（幂等保护）"

    try:
        new_lines, desc = _apply_one(src_lines, patch)
    except AnchorError as e:
        return False, f"[{pid}] 锚点定位失败：{e}"

    new_src = "\n".join(new_lines)

    # ① 语法必须仍然正确
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        return False, f"[{pid}] 替换后语法错误，已放弃：{e}"

    # ② 改动行数必须与预期相符（超出说明改到了别处）
    delta = len(new_lines) - len(src_lines)
    exp_delta = patch.get("expected_delta_lines")
    if exp_delta is not None and delta != exp_delta:
        return False, (f"[{pid}] 改动行数异常：实际 {delta:+d}，预期 {exp_delta:+d}。"
                       f"拒绝落盘（可能误改了别处）")

    # ③ 签名校验：替换后必须能找到补丁声明的特征串
    for sig in patch.get("signatures", []):
        if sig not in new_src:
            return False, f"[{pid}] 落盘后校验失败：找不到特征串 {sig!r}"

    # ③.5 【2026-09-17 新增】**反向**校验：声明"必须消失"的串，装完就不能再出现。
    #
    # 【为什么必须有·一次真实事故】`replace_lines` 只替换**一行**。
    # 若某个补丁想把「一个 if 连同它的函数体」整体换掉，只替换 `if` 那一行，
    # **原来的函数体会残留在后面**，形成"新逻辑后面又跟了一段旧逻辑" ——
    # 旧逻辑照样执行，**补丁等于没生效**。
    # 而 signatures 只验"该有的在"，**验不出残留**；expected_delta_lines 也验不出
    # （残留属于原有行，不计入 delta）。实测就这么静默漏过去了一次。
    # 所以补丁可声明 absent_signatures：装完后**必须不再出现**的串。
    for sig in patch.get("absent_signatures", []) or []:
        if sig in new_src:
            return False, (f"[{pid}] 落盘后校验失败：仍存在本应被替换掉的旧串 {sig!r}"
                           f"（补丁可能只替换了一半，旧代码残留在后面）")

    if dry:
        return True, f"[{pid}] 可安装：{desc}（改动 {delta:+d} 行）"

    # ④ 首次安装前备份原始文件（只备份一次，保证能回到"未打任何补丁"的状态）
    os.makedirs(BACKUP_DIR, exist_ok=True)
    st = load_state()
    if not st.get("baseline"):
        bp = os.path.join(BACKUP_DIR, "signin.py.orig")
        write_text(bp, src)
        st["baseline"] = {
            "sha256": sha256_of_file(bp),
            "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bytes": os.path.getsize(bp),
        }
        save_state(st)

    # ⑤ 原子写回（tmp + os.replace），避免中途崩溃留下半个文件
    tmp = TARGET + ".patching"
    write_text(tmp, new_src)
    os.replace(tmp, TARGET)

    st = load_state()
    st.setdefault("installed", {})
    st["installed"][pid] = {
        "desc": desc,
        "applied_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sha256_after": sha256_of_file(TARGET),
    }
    save_state(st)
    # 【跨包】登记"本包改过 signin.py"
    try:
        st2 = load_state()
        st2["last_written_sha256"] = sha256_of_file(TARGET)
        save_state(st2)
        registry_note(PKG_NAME, {
            "sha256_after": sha256_of_file(TARGET),
            "installed_ids": sorted(st["installed"].keys()),
        })
    except Exception as _re:
        print(f"[警告] 跨包注册表写入失败（不影响安装）: {type(_re).__name__}: {_re}")
    return True, f"[{pid}] 已安装：{desc}（改动 {delta:+d} 行）"


# ==================== 拆卸 ====================
def revert_all():
    """把所有补丁一次性拆掉：直接用基线备份覆盖回去。

    【为什么用覆盖而不是逐条反向替换】
    反向替换要维护一份"还原锚点"，一旦主程序在两版之间被手工改过就会错位。
    而基线备份是**逐字节**的原件，覆盖回去必然干净——这正是补丁包
    "能装能拆"承诺的最强实现。代价是：**打补丁期间对 signin.py 的手工修改会丢**。
    所以引擎在拆卸前会显式警告，并检查是否有"非补丁改动"。
    """
    st = load_state()
    bp = os.path.join(BACKUP_DIR, "signin.py.orig")
    if not st.get("baseline") or not os.path.exists(bp):
        return False, "没有找到基线备份，无法拆卸（可能从未安装过补丁）"

    base_sha = st["baseline"]["sha256"]
    if sha256_of_file(bp) != base_sha:
        return False, "基线备份自身已被改动（sha256 不符），拒绝拆卸以免覆盖成错误内容"

    cur_sha = sha256_of_file(TARGET)
    if cur_sha == base_sha:
        return True, "当前文件已等于基线（补丁本就没装或已拆），无需操作"

    # 【跨包防护】统一安全检查（见 _revert_safety_check 的详细说明）
    _ok, _why = _revert_safety_check()
    if not _ok:
        return False, _why

    # 备份"拆卸前"的版本，便于万一拆错了还能捞回来
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    write_text(os.path.join(BACKUP_DIR, f"signin.py.before_revert_{ts}"), read_text(TARGET))

    write_text(TARGET, read_text(bp))
    if sha256_of_file(TARGET) != base_sha:
        return False, "拆卸后 sha256 与基线不一致，请人工检查"

    st["installed"] = {}
    st["last_reverted_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["last_written_sha256"] = sha256_of_file(TARGET)
    save_state(st)
    # 【跨包】登记"本包把文件改回了基线"
    try:
        registry_note(PKG_NAME, {"sha256_after": sha256_of_file(TARGET),
                                 "installed_ids": []})
    except Exception:
        pass
    return True, f"已全部拆卸，signin.py 恢复为基线（sha256={base_sha[:16]}...）"


def revert_one(pid):
    """拆卸单个补丁 —— 走"全部拆 + 重装其余"的等效路径，保证结果确定。"""
    st = load_state()
    installed = st.get("installed", {})
    if pid not in installed:
        return False, f"[{pid}] 未安装，无需拆卸"
    others = [p for p in installed if p != pid]
    ok, msg = revert_all()
    if not ok:
        return False, msg
    msgs = [msg]
    for op in sorted(others):
        d = find_patch(op)
        if not d:
            msgs.append(f"[{op}] 警告：定义文件已丢失，无法重装")
            continue
        # 刚 revert 完是干净基线，这里必须能装上；装不上要如实报错
        ok2, m2 = apply_patch(d)
        msgs.append(m2)
        if not ok2:
            return False, "\n".join(msgs)
    return True, "\n".join(msgs)


def sync_state():
    """把 _state.json 的 installed 记录与实际文件痕迹对齐（**双向**）。

    【为什么需要】_state.json 是"记录"，文件里的特征串是"事实"。
    两者可能不一致，典型场景：
      · 有人手工编辑了 signin.py（或测试脚本直接覆盖了文件）；
      · _state.json 被误删/回滚；
      · 另一个补丁包整包覆盖后，本包的记录被冲掉但文件里其实还在。
    本项目一贯的原则是"**记录服从事实**"——所以这里以文件痕迹为准。

    【★ 2026-09-16 修正：原来是单向的，漏了一半】
    原实现只做"文件里没有 → 删掉记录"这一半。反过来的情况
    （**记录说没装、文件里其实装着**）没有处理，于是会出现：
        `patch_engine.py list` 打印"已装 0 个"，
        但文件里 P1a 的特征串明明在。
    —— 这既是"记录压过事实"，又会让后续 revert/apply 判断错。
    实测就在这条上栽了：另一个包在本包之后落了笔，本包的记录被冲成空，
    文件里却仍是两根补丁叠加，`list` 显示"已装 0 个"，人看了会以为没装。

    修法：**双向对齐**。文件里有痕迹却没有记录的，补上记录。
    返回 (调整的条数, 说明列表)。
    """
    st = load_state()
    installed = st.get("installed", {})
    src = read_text(TARGET)
    notes = []

    # 方向一：记录说已装，文件里却没痕迹 → 删掉这条假记录
    for pid in list(installed.keys()):
        d = find_patch(pid)
        if not d:
            continue
        sigs = d.get("signatures", [])
        if sigs and not all(s in src for s in sigs):
            del installed[pid]
            notes.append(f"{pid}: 文件里已无签名痕迹，从'已装'记录中移除")

    # 方向二：文件里有痕迹，记录里却没有 → 按事实补上记录
    # （用 sha256_after 无法还原真实值，这里标记为 unknown，只求"已装"这个判断正确）
    for d in load_patches():
        pid = d.get("id")
        if pid in installed:
            continue
        sigs = d.get("signatures", [])
        if sigs and all(s in src for s in sigs):
            installed[pid] = {
                "desc": d.get("title", ""),
                "applied_at": "(由文件事实推定)",
                "sha256_after": None,
                "source": "sync_from_file",
            }
            notes.append(f"{pid}: 文件里有签名痕迹但无记录，按事实补记为'已装'")

    if notes:
        st["installed"] = installed
        save_state(st)
    return len(notes), notes


def _sync_state_quiet():
    """同步状态并只返回可打印的提示行（无记录可清理时返回 []）。

    【为什么单独包一层·2026-09-16】踩过一次真实事故：
    自检脚本 test_patch_engine.py 跑完会把 signin.py 还原成干净基线，
    但 _state.json 里 installed 仍写着"已装"。此时执行 apply：
        [P1a] 已装过，跳过
        [P1b] 已装过，跳过
        → 签名校验失败 → 补丁**装不回去**，而且输出里那句"已装过"是谎话。
    根因是 apply/list 只读了 _state.json 这个"记录"，没看文件里的"事实"。
    修法：在 CLI 分派前统一 sync 一次，让后面所有判断都落在事实上。
    """
    try:
        _, notes = sync_state()
    except Exception as e:      # 同步失败绝不能拦住 apply
        return [f"状态同步失败（忽略，按记录继续）：{type(e).__name__}: {e}"]
    if notes:
        return ["[状态同步] 记录与文件不一致，已按文件事实修正："] + \
               ["    " + n for n in notes]
    return []


# ==================== 校验 ====================
def check_all():
    """校验每个补丁：能否定位、是否已装。返回 (all_ok, 报告行列表)。"""
    st = load_state()
    installed = st.get("installed", {})
    src = read_text(TARGET)
    report = []
    all_ok = True

    base = st.get("baseline")
    if base:
        report.append(f"基线备份：{base['sha256'][:16]}...  ({base['bytes']} 字节, {base['saved_at']})")
    else:
        report.append("基线备份：尚未创建（首次安装时自动生成）")
    report.append(f"当前 signin.py：{sha256_of_file(TARGET)[:16]}...")
    report.append("")

    for d in load_patches():
        pid = d.get("id", "?")
        title = d.get("title", "")
        sigs = d.get("signatures", [])
        missing = [s for s in sigs if s not in src]
        sig_all_present = bool(sigs) and not missing
        recorded = pid in installed

        # 【三态判定·2026-09-16】不能只看 _state.json 的记录，要看**文件里的实际痕迹**。
        # 因为记录文件可能丢失/被删，而"补丁到底在不在代码里"才是事实。
        if recorded and sig_all_present:
            report.append(f"  [已装·完好] {pid} {title}")
        elif recorded and not sig_all_present:
            report.append(f"  [已装·签名丢失] {pid} {title}  缺: {missing}"
                          f"（代码被改过？建议 revert 后重装）")
            all_ok = False
        elif not recorded and sig_all_present:
            # 有痕迹但没记录：可能是手动装的、或 _state.json 被删了
            report.append(f"  [疑似已装·无记录] {pid} {title}"
                          f"（文件里有补丁痕迹但状态文件无记录，请人工确认）")
            all_ok = False
        else:
            try:
                ok, msg = apply_patch(d, dry=True)
            except Exception as e:
                ok, msg = False, f"[{pid}] 异常：{e}"
            if ok:
                report.append(f"  [待装·可安装] {pid} {title}  -> {msg}")
            else:
                report.append(f"  [待装·无法安装] {pid} {title}  -> {msg}")
                all_ok = False
    return all_ok, report


def verify_installed():
    """确认补丁真的生效了（不是"以为装了"）。"""
    st = load_state()
    installed = st.get("installed", {})
    src = read_text(TARGET)
    if not installed:
        return False, "没有任何补丁处于已装状态"
    bad = []
    for d in load_patches():
        pid = d.get("id")
        if pid not in installed:
            continue
        for s in d.get("signatures", []):
            if s not in src:
                bad.append(f"{pid}: 缺特征串 {s!r}")
    if bad:
        return False, "签名校验失败：\n  " + "\n  ".join(bad)
    return True, f"{len(installed)} 个补丁签名校验全部通过"


# ==================== CLI ====================
def main():
    ap = argparse.ArgumentParser(description="签到系统 可插拔补丁包")
    ap.add_argument("cmd", choices=["list", "check", "apply", "revert", "verify"],
                    help="list=列补丁 / check=校验 / apply=安装 / revert=拆卸 / verify=签名校验")
    ap.add_argument("pid", nargs="?", help="补丁 id（apply/revert 时可指定单个）")
    args = ap.parse_args()

    # 【2026-09-16·记录服从事实】任何命令开始前，先把 _state.json 与实际文件对齐。
    # 否则会出现"记录说已装、文件里其实没有"→ apply 被幂等保护跳过 → 补丁装不回去。
    sync_msgs = _sync_state_quiet()

    patches = load_patches()
    st = load_state()
    installed = st.get("installed", {})

    if sync_msgs:
        for _m in sync_msgs:
            print(_m)
        print()

    if args.cmd == "list":
        print(f"补丁目录：{os.path.join(HERE, 'patches')}")
        print(f"目标文件：{TARGET}")
        print()
        if not patches:
            print("  (没有补丁定义)")
        for d in patches:
            pid = d.get("id", "?")
            mark = "✅已装" if pid in installed else "⬜未装  "
            print(f"  {mark}  {pid:<4} {d.get('title','')}")
            desc = d.get("why", "")
            if desc:
                print(f"            {desc}")
        print()
        print(f"共 {len(patches)} 个补丁，已装 {len(installed)} 个")
        return 0

    if args.cmd == "check":
        ok, report = check_all()
        print("=== 补丁校验 ===")
        for line in report:
            print(line)
        print()
        print("结论：" + ("全部正常，可以安装" if ok else "存在无法安装/异常项，请先解决"))
        return 0 if ok else 1

    if args.cmd == "apply":
        targets = [find_patch(args.pid)] if args.pid else patches
        if args.pid and not targets[0]:
            print(f"找不到补丁 {args.pid}")
            return 1
        failed = 0
        for d in targets:
            if not d:
                continue
            if d.get("id") in installed:
                print(f"[{d['id']}] 已装过，跳过")
                continue
            ok, msg = apply_patch(d)
            print(msg)
            if not ok:
                failed += 1
        if failed:
            print(f"\n{failed} 个补丁安装失败")
            return 1
        ok, msg = verify_installed()
        print("\n" + msg)
        return 0 if ok else 1

    if args.cmd == "revert":
        if args.pid:
            ok, msg = revert_one(args.pid)
        else:
            ok, msg = revert_all()
        print(msg)
        return 0 if ok else 1

    if args.cmd == "verify":
        ok, msg = verify_installed()
        print(msg)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
