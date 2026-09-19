# -*- coding: utf-8 -*-
"""补丁引擎负向测试（严格版）。

【为什么要先 revert】本项目铁律 4.7：负向测试前必须先固定干净基线。
否则"补丁已装"这个状态会让幂等保护先行拦截，用例②③④⑤验证的护栏
（行数/语法/函数名）根本没被执行 —— 那是**假 REAL**，比不做还危险。
所以流程固定为：revert 到基线 → 每条注入 → 确认拒绝 → 再 revert 校验 sha256。
"""
import sys, os, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch_engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
P1B_DEF = os.path.join(HERE, "patches", "P1b.json")

BASE = E.read_text(os.path.join(E.BACKUP_DIR, "signin.py.base"))
BASE_SHA = hashlib.sha256(BASE.encode("utf-8")).hexdigest()
print(f"干净基线 sha256 = {BASE_SHA[:16]}...")
print()


results = []

def reset_to_baseline():
    E.write_text(E.TARGET, BASE)
    assert E.sha256_of_file(E.TARGET) == BASE_SHA, "基线恢复失败"

def caser(name, mutate, expect_reject):
    reset_to_baseline()                    # ← 关键：每条用例都从干净基线出发
    d = json.loads(E.read_text(P1B_DEF))
    d["id"] = "_neg"
    mutate(d)
    try:
        ok, msg = E.apply_patch(d, dry=True)
    except Exception as e:
        ok, msg = False, f"异常 {type(e).__name__}: {e}"
    got_reject = (not ok)
    verdict = "REAL" if got_reject == expect_reject else "摆设!!"
    results.append(verdict)
    print(f"[{verdict}] {name}")
    print(f"        期望={'拒绝' if expect_reject else '放行'} → 实际={'拒绝' if got_reject else '放行'}")
    print(f"        引擎说：{msg[:130]}")
    print()

caser("① 特征串改成不存在的字符串", lambda d: d.update(loop_signature="根本不存在XYZ"), True)
caser("② expected_delta_lines 改成错误值", lambda d: d.update(expected_delta_lines=999), True)
caser("③ signatures 写一个装完也不会有的串", lambda d: d.update(signatures=["绝对不存在的签名ABC"]), True)
caser("④ new 里埋语法错误", lambda d: d.update(new="        for k in range(6\n            pass"), True)
caser("⑤ function 改成不存在的函数名", lambda d: d.update(function="no_such_func_xyz"), True)
caser("⑥ 原样定义（正例，必须放行）", lambda d: None, False)


# ============================================================
# ⑦ 回归：记录与事实不一致时，apply 必须能装回去
#
# 【为什么必须有一条】2026-09-16 真实事故：
#   跑完本自检脚本 → signin.py 被 reset_to_baseline() 还原成干净基线
#   → 但 _state.json 的 installed 仍写着"已装"
#   → 用户执行 `patch_engine.py apply`：
#         [P1a] 已装过，跳过
#         [P1b] 已装过，跳过
#         签名校验失败：P1a 缺特征串 'def _scroll_fingerprint(' ...
#   补丁**装不回去**了，且那句"已装过"是谎话。
#   根因：apply 只信 _state.json 这个"记录"，没看文件里的"事实"。
#
# 【为什么必须起子进程走 CLI】第一版这条用例直接调 E.sync_state()，
#   结果"把 CLI 里的 sync 调用整段禁用"的变异体它照样 PASS —— 假 REAL。
#   因为判据必须落在**用户真实走的那条路**（`patch_engine.py apply`），
#   而不是"我顺手能调到的那个函数"。所以这里用 subprocess 起真 CLI。
# ============================================================
import subprocess
reset_to_baseline()
print("⑦ 记录与事实不一致（_state.json 说已装、文件其实干净）时 apply 能否装回去")

_st = E.load_state()
_st_backup = json.dumps(_st, ensure_ascii=False)
_st["installed"] = {
    "P1a": {"desc": "伪造记录", "applied_at": "2026-09-16 00:00:00", "sha256_after": "0" * 64},
    "P1b": {"desc": "伪造记录", "applied_at": "2026-09-16 00:00:00", "sha256_after": "0" * 64},
}
E.save_state(_st)
print(f"        伪造记录 installed = {list(E.load_state().get('installed', {}).keys())}"
      f"  文件基线 sha256 = {E.sha256_of_file(E.TARGET)[:16]}...")

_ENGINE = os.path.join(HERE, "patch_engine.py")
_py = sys.executable
_r = subprocess.run([_py, _ENGINE, "apply"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", cwd=HERE,
                    timeout=120)
_out = (_r.stdout or "") + (_r.stderr or "")
print("        --- CLI apply 输出 ---")
for _ln in _out.strip().splitlines():
    print("        " + _ln)

# 判据（落在事实，不落在"入口有没有做某事"）：
#   a) 退出码为 0              —— 引擎自己认为成功
#   b) 文件里真的出现补丁痕迹  —— 这件事的**唯一**可靠证据
#   c) 哈希等于正常安装后的值  —— 逐字节确认装对了
_final_sha = E.sha256_of_file(E.TARGET)
_sig_ok = all(s in E.read_text(E.TARGET)
              for d in E.load_patches() for s in d.get("signatures", []))
_ok7 = (_r.returncode == 0) and _sig_ok
if _ok7:
    print(f"        最终 sha256 = {_final_sha[:16]}...  （正常安装后应为 2f2d675c081eea05...）")
    _ok7 = _final_sha.startswith("2f2d675c081eea05")
    if not _ok7:
        print("        哈希与正常安装结果不符 → 装出来的东西不对")
else:
    print(f"        退出码={_r.returncode}  补丁痕迹={_sig_ok}  → 装不回去")
results.append("REAL" if _ok7 else "摆设!!")
print(f"[{'REAL' if _ok7 else '摆设!!'}] ⑦ 结果={'能装回去' if _ok7 else '装不回去'}")
print()

# 还原状态与文件，别影响后续
if _st_backup:
    E.save_state(json.loads(_st_backup))
reset_to_baseline()

# ============================================================
# ⑧ 跨包防护：别的补丁包也改过这个文件时，整包拆卸必须被拒绝
#
# 【为什么必须有一条】2026-09-16 真实事故（本脚本这次上线当天就踩了）：
#   21:26 用 `补丁包/patch_engine.py revert` 拆卸，输出
#        "已全部拆卸，signin.py 恢复为基线（sha256=af3865177324d8a5...）"
#   而当时文件里还装着另一个包（跳过无效滚动补丁包）的 P2a+P2b
#   → 整包覆盖把 P2a+P2b 一起抹掉了，对方 _state.json 仍以为装着。
#   后果：脚本静默退化为"没有 P2 的版本"，而所有状态都显示"已装"。
#
# 【判据必须落在"文件有没有被抹"这个事实】
#   不能只看"revert 有没有打印拒绝"——那只是入口行为。
#   真正的判据：**执行完 revert 后，文件里 P2 的签名痕迹还在不在**。
#   在 = 防护生效；没了 = 防护是摆设。
#
# 【怎么构造"别的包"】不伪造注册表，而是**真的**让本包的 state 承认
#   "我上次改完是 X，现在文件是 Y（≠X）"——这正是对方改过文件后的样子。
#   等价于：登记一个与当前文件不同的 last_written_sha256。
#
# 【★ 必须清空注册表·否则是假 REAL（第一版就踩了）】
#   本用例要验的是**判据 2（自查）**。但判据 1（注册表交叉核对）也会
#   "拒绝拆卸"，而且它先被判据 2 之前……不，两者都会拒绝，**结果一样**。
#   第一版实测：把 self_record_mismatch 强制返回 False（变异）后，
#   用例⑧ **照样 PASS** —— 因为注册表里留着上次测试的残留条目，
#   判据 1 顶上来拒绝了。判据 2 明明没生效，用例却绿了 → 假 REAL。
#   修法：**跑这条用例前把注册表清空**，让判据 1 必然失效，
#   此时"拒绝"就只可能来自判据 2 —— 判据才落到我要验的那个护栏上。
# ============================================================
print("⑧ 别的包改过文件后，本包整包拆卸必须被拒绝（且文件不得被抹）")

_reg_backup = E.read_text(E.REGISTRY_PATH) if os.path.exists(E.REGISTRY_PATH) else None
E.write_text(E.REGISTRY_PATH, json.dumps({"packages": {}}, ensure_ascii=False))
print("        [前置] 注册表已清空 → 保证判据 1 必然失效，只可能由判据 2 拦截")

# 先造出"本包已装 + 另一个包又改过"的真实状态：
#   1) apply 本包（P1a+P1b）→ 文件 = 2f2d675c…
E.apply_patch(json.loads(E.read_text(os.path.join(HERE, "patches", "P1a.json"))))
E.apply_patch(json.loads(E.read_text(P1B_DEF)))
_mid_sha = E.sha256_of_file(E.TARGET)
print(f"        本包已装（P1a+P1b）→ sha256 = {_mid_sha[:16]}...")

# apply_patch 会把本包写进注册表 → 判据 1 看"别的包"时看不到任何条目；
# 但本包自己会跳过自己。为绝对确定，这里再清一次。
E.write_text(E.REGISTRY_PATH, json.dumps({"packages": {}}, ensure_ascii=False))

#   2) 把本包登记的 last_written_sha256 改成"另一个包改完之后"的 sha。
#      用 0000… 代表"本包装完后，文件又被别人动过"。
_st8 = E.load_state()
_st8_backup = json.dumps(_st8, ensure_ascii=False)
_st8["last_written_sha256"] = "0" * 64
E.save_state(_st8)
print(f"        模拟另一包改过文件 → 本包登记 sha 已与当前文件不符")

#   3) 通过真 CLI 走用户真实路径拆卸
_r8 = subprocess.run([_py, _ENGINE, "revert"], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", cwd=HERE, timeout=120)
_out8 = (_r8.stdout or "") + (_r8.stderr or "")
print("        --- CLI revert 输出 ---")
for _ln in _out8.strip().splitlines():
    print("        " + _ln)

_after_sha = E.sha256_of_file(E.TARGET)
# 判据：文件必须**原封不动**（仍是 _mid_sha）
_ok8 = (_after_sha == _mid_sha)
print(f"        拆卸后 sha256 = {_after_sha[:16]}...  （应仍为 {_mid_sha[:16]}...）")
if not _ok8:
    print("        → 文件被抹掉了！跨包防护失效")
results.append("REAL" if _ok8 else "摆设!!")
print(f"[{'REAL' if _ok8 else '摆设!!'}] ⑧ 结果={'拦截成功·文件完好' if _ok8 else '未拦截·文件被抹'}")
print()

# 还原：先把 _state 还原，再逐条拆本包（整包 revert 现在会被拦，属正常）
if _st8_backup:
    E.save_state(json.loads(_st8_backup))
E.revert_one("P1b")
E.revert_one("P1a")
reset_to_baseline()
# 恢复注册表
if _reg_backup is not None:
    E.write_text(E.REGISTRY_PATH, _reg_backup)
    print("        [后置] 注册表已恢复原状")

# ============================================================
# ⑨ 状态同步必须是**双向**的：文件里有痕迹但记录里没有 → 要补记
#
# 【为什么必须有一条】2026-09-16 实测：
#   另一个包在本包之后落了笔 → 本包的 _state.json installed 被冲成空，
#   但文件里 P1a/P1b 的特征串**明明还在**。
#   此时 `patch_engine.py list` 打印"已装 0 个" —— **记录压过事实**，
#   人看了会以为没装，后续 revert/apply 的判断也跟着错。
#   根因：sync_state() 原来只做"删假记录"这一个方向，
#   没有做"文件有痕迹但记录没有 → 补记"这另一个方向。
#
# 【判据落在事实】不看"sync 有没有被调用"，而看**调用后 list 报的已装数**
#   是否等于文件里实际的补丁数。
# ============================================================
print("⑨ 记录为空但文件里有补丁痕迹时，list 必须报出真实的已装数")

# 造出"本包已装、但记录被清空"的状态
E.apply_patch(json.loads(E.read_text(os.path.join(HERE, "patches", "P1a.json"))))
E.apply_patch(json.loads(E.read_text(P1B_DEF)))
_sha9 = E.sha256_of_file(E.TARGET)

_st9 = E.load_state()
_st9_backup = json.dumps(_st9, ensure_ascii=False)
_st9["installed"] = {}          # ← 记录被冲成空
E.save_state(_st9)
print(f"        文件里 P1a+P1b 都在（sha256={_sha9[:16]}...），但 _state.json 的 installed 被清空")

# 走真 CLI
_r9 = subprocess.run([_py, _ENGINE, "list"], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", cwd=HERE, timeout=120)
_out9 = (_r9.stdout or "") + (_r9.stderr or "")
print("        --- CLI list 输出（末尾） ---")
for _ln in _out9.strip().splitlines()[-6:]:
    print("        " + _ln)

# 判据：list 报告的"已装 N 个"必须等于文件里真实的补丁数（2）
import re as _re
_m = _re.search(r"已装\s*(\d+)\s*个", _out9)
_reported = int(_m.group(1)) if _m else -1
_expected = sum(1 for d in E.load_patches()
                if d.get("signatures") and all(s in E.read_text(E.TARGET)
                                               for s in d["signatures"]))
_ok9 = (_reported == _expected == 2)
print(f"        list 报'已装 {_reported} 个'，文件里实际 {_expected} 个 → 期望 2")
if not _ok9:
    print("        → 记录压过事实，list 报错了")
results.append("REAL" if _ok9 else "摆设!!")
print(f"[{'REAL' if _ok9 else '摆设!!'}] ⑨ 结果={'报数正确' if _ok9 else '报数错误'}")
print()

# 还原
if _st9_backup:
    E.save_state(json.loads(_st9_backup))
E.revert_one("P1b")
E.revert_one("P1a")
reset_to_baseline()

print(f"全部用例跑完后 signin.py sha256 = {E.sha256_of_file(E.TARGET)[:16]}...")
print(f"与干净基线一致: {E.sha256_of_file(E.TARGET) == BASE_SHA}")
if os.path.exists("_tmp_neg.json"): os.remove("_tmp_neg.json")
print()
real = results.count("REAL")
print(f"===== 负向测试汇总：{real}/{len(results)} REAL =====")
sys.exit(0 if real == len(results) else 1)
