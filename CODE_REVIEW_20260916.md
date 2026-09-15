# 签到系统 · 全面代码复查报告

- **复查日期**：2026-09-16
- **复查基线**：`4dad934`（工作区干净，`smoke_test.py` 111 项全过）
- **复查范围**：`signin.py`(3133) / `smoke_test.py`(1467) / `history.py`(330) / `self_heal.py`(190) / `step_tracer.py`(165) / `report.py`(148) / `import_history.py`(173) / `capture_templates.py`(181) / `collect_samples.py`(172) / `wifi_helper/wifi_auto_login.py`(576) / `wifi_helper/browser_login.py` / `notify_helper/feishu_notify.py`(477)，合计约 7800 行
- **复查维度**：边界条件 · 异常处理 · 资源释放 · 并发/时序 · 输入校验
- **方法论声明**：以下每条**均已实测复现或静态验证**，不接受"读代码推测"。凡我推测但未能证实的，一律标注"未证实"。

## 总体结论

代码质量**高于一般个人项目**：无可变默认参数、无 `eval`/`exec` 滥用、文件句柄基本都用 `with`、所有 `subprocess` 关键路径都带 `timeout`、注释诚实且大多有实测依据。

但发现 **3 个高优先级真缺陷**、**6 个中优先级问题**、**5 个低优先级改进项**。其中 `P0-1`（`atexit` 兜底失效）和 `P0-2`（WiFi 连接误判）是**确凿的功能性 bug**，会在真实场景下造成"以为有兜底其实没有"的错觉——按本项目"失败要暴露、不能掩盖"的第一原则，这两条必须修。

---

## 一、高优先级（建议立即修复）

### P0-1 `atexit` 中断兜底在强杀/关窗下完全不执行 —— 兜底形同虚设

> **状态：已于 2026-09-16 修复（提交见 git log）。**
> 修复方案：三层兜底 —— ① `atexit`（保留，覆盖正常退出）
> ② `signal.SIGINT/SIGTERM`（覆盖 Ctrl+C / 关窗口，**当场**发提醒，时效最好）
> ③ **残留标记（主保障）**：`.agent/_in_progress.json`，落盘于首次点击"完成签到"之后，
>    `main()` 开头由 `check_stale_in_progress()` 检查并补发"结果未知"提醒。
>    **只有 ③ 能覆盖 `taskkill /F`** —— 那是内核级终止，进程没有任何执行机会。
> 验证：真实强杀链路实测（`taskkill /F` 与 `terminate()` 下 `atexit` 均不执行，
> 残留标记均留存）；6 个逻辑场景 + 3 个分支（含截图带出、目录已清理）全部通过；
> 新增断言 5 项子检查均经"改坏必 FAIL"负向验证。
> 详见文末「修复记录」。

**位置**：`signin.py` L3106-L3132（`_on_exit_guard` + `atexit.register`）

**触发条件**（三种中最常见的那两种都失效）：

| 终止方式 | `atexit` 是否执行 | 实测结果 |
|---|---|---|
| 正常 `sys.exit()` | ✅ 执行 | True |
| `proc.terminate()`（任务管理器"结束任务"） | ❌ **不执行** | False |
| `taskkill /F`（强杀、计划任务超时） | ❌ **不执行** | False |

**实测复现**（三种方式逐一验证）：

```
场景1 taskkill /F  -> atexit 标记存在? False
场景2 terminate()  -> atexit 标记存在? False
场景3 正常退出      -> atexit 标记存在? True
```

**影响范围**：
`_on_exit_guard()` 的设计意图（见 L3107-3113 注释）正是"用户在 21:18:53 把脚本掐了 → 收不到任何消息"。而"掐脚本"的现实操作 —— **关窗口、任务管理器结束任务、计划任务 30 分钟 `ExecutionTimeLimit` 强杀**（`install_task.ps1` L12 确实设了 30 分钟上限）—— 走的全是 `terminate`/`taskkill` 路径，`atexit` 一律不触发。

**后果**：这条兜底**只在正常退出时生效，而正常退出时本来就会走 `notify_feishu()`**。也就是说 `_FEISHU_DONE` 标记几乎总已置位，兜底会提前 `return`。**该通道的实际保护率接近 0%**。

**严重程度**：**高**。属于"以为有防护、实际没有"的隐性缺失，比明确没有更危险 —— 一旦真发生中断，用户和开发者都会以为"应该有通知啊"，从而延迟排查。

**修复建议**（三选一，推荐方案 1）：

**方案 1（推荐）**：改用 `signal` 捕获 + 兜底文件双保险
```python
import signal

def _on_exit_guard_signal(signum, frame):
    """信号路径：Ctrl+C / 控制台关闭 / 部分终止请求"""
    _on_exit_guard()
    os._exit(1)

try:
    signal.signal(signal.SIGINT, _on_exit_guard_signal)
    signal.signal(signal.SIGTERM, _on_exit_guard_signal)
except Exception:
    pass
```
注意：`SIGTERM` 在 Windows 上对 `Popen.terminate()` 有效，但对 `taskkill /F` 仍无效。

**方案 2（真正兜住强杀，推荐与方案 1 并用）**：**心跳 + 启动时补偿**
在 `_GUARD` 里维护一个"进行中"落盘标记，每次 `finish_clicks` 变化时原子写 `.agent/_in_progress.json`；`main()` **开头的 `flush_pending` 附近**检查该残留标记：
```python
# main() 开头：若发现上次留下"已点完成签到但未收尾"的残留标记 → 补发一次"结果未知"
# 这是唯一能兜住 taskkill /F 的手段：进程已死，只能靠下次启动来补
```
收尾时删除标记。这样即使用户强杀，**下次运行（第二天）也能补发提醒**。

**方案 3（最轻量）**：把 `shutdown_pc()` 里的关机指令改成"先发通知再关机"，确保 `notify_feishu()` 一定在不可逆动作之前完成（当前顺序已正确，见 L3016→L3019，仅需保持）。

**负向测试建议**：断言 `signin.py` 里除 `atexit` 外**必须**存在信号处理或残留标记机制 —— 用 AST 检查 `signal.signal` 调用或 `.agent/_in_progress` 字符串出现在 `main()` 可达路径中。

---

### P0-2 `reconnect_wifi()` 的 "connected" 子串匹配会把「已断开」误判为「已连接」

**位置**：`signin.py` L2229

```python
if ("已连接" in stat) or ("connected" in stat.lower()):
```

**触发条件**：`netsh wlan show interfaces` 的英文输出包含 `State : disconnected`（系统语言为英文，或 `netsh` 返回英文串时）。

**实测复现**：
```
未连   | '名称 : Wi-Fi\n状态 : 已断开连接'
误判为已连 | 'Name : Wi-Fi\nState : disconnected'   ← Bug
误判为已连 | 'Name : Wi-Fi\nState : connected'
误判为已连 | '名称 : Wi-Fi\n状态 : 已连接'
```

**影响范围**：
- `reconnect_wifi()` 的 `linked` 标志被错误置为 `True`，直接跳过"重连后未恢复链路"的告警分支（L2231-2233）
- 后续进入 `net_reachable()` 探测。若确实没网，会走 L2237-2240 的"仍继续尝试定位"分支 —— **函数返回 `True`（声称重连成功）实际上网络是断的**
- 真实受害者：`LOCATE_WAIT` 内定位漂移场景（L2554）。脚本以为"已刷新定位"，于是 `wifi_refreshed = True`，**不会再次尝试重连**，把最后一次自愈机会浪费掉

**严重程度**：**高**（但在中文系统上触发概率较低）。当前机器是中文 `netsh` 输出，所以**今天没暴露** —— 这正是"偶发失败比稳定失败更危险"的典型：一旦系统语言变化或换机器，就会静默退化。

**修复建议**：改为**否定词优先 + 精确匹配**
```python
# 先排否定，再判肯定 —— "disconnected"/"未连接"/"已断开" 必须优先于 "connected"
stat_l = stat.lower()
linked = False
if ("已连接" in stat) or ("已断开" in stat) or ("未连接" in stat):
    # 中文输出：只认"已连接"，且确保不是"已断开连接"
    linked = ("已连接" in stat) and ("已断开" not in stat)
else:
    # 英文输出：用词边界，杜绝 disconnected 命中
    linked = bool(re.search(r"\bconnected\b", stat_l)) and not re.search(r"\bdisconnected\b", stat_l)
```
更稳妥的写法（推荐）—— 直接匹配**整行**：
```python
m = re.search(r"^\s*(?:State|状态)\s*:\s*(.+)$", stat, re.M)
state = (m.group(1).strip().lower() if m else "")
linked = state in ("connected", "已连接")   # 白名单，杜绝子串命中
```

**负向测试建议**：喂 `"Name : Wi-Fi\nState : disconnected"` 给判据函数，断言必须返回 `False`。当前测试集**没有覆盖这条**，属于真实存在的护栏缺口。

---

### P0-3 `GLOBAL_TIMEOUT` 只检查轮次边界，实际最坏可跑 19.5 分钟

**位置**：`signin.py` L281（定义 900 秒）+ L2957（唯一检查点）

**触发条件**：单轮内部耗时接近上限时。检查点位于 `for rnd in range(1, MAX_ROUNDS+1)` **循环体第一行**，单轮内部**没有任何全局超时检查**。

**量化实测**：
```
单轮主要等待上限: DETAIL_WAIT(20) + LOCATE_WAIT(120) + CONFIRM_WAIT(50) = 190 秒
台账实测最慢一次: 271 秒（4.5 分钟）
三轮最坏(271×3 + 轮间98秒) = 911 秒 = 15.2 分钟  → 已超过 900
最坏情况(第3轮开始时 899 秒 + 再跑满一轮 271 秒) = 1170 秒 = 19.5 分钟
```

**影响范围**：
- 计划任务设了 `ExecutionTimeLimit = 30 分钟`（`install_task.ps1` L12）。19.5 分钟仍在 30 分钟内，**不会触发超时强杀**，所以不会造成漏签
- 但**"全局 15 分钟防卡死"的承诺不成立**。若某天单轮某处出现"等待本身也卡住"（如 `pyautogui.click` 被 UAC 弹窗阻塞、`cv2` 大图处理卡顿），实际无任何刹车
- 激活 `extend_locate_wait` 自愈信号时（`LOCATE_WAIT = 120`），单轮上限升到 190+ 秒，更贴近边界

**严重程度**：**中高**。当前不至于漏签，但"防卡死"这道门是虚掩的。

**修复建议**：把全局检查下沉到**每个长等待循环内部**。最小改动 —— 在 `LOCATE_WAIT` / `CONFIRM_WAIT` / `DETAIL_WAIT` 三个主循环的 `while` 条件里加上超时：
```python
# 原：while time.time() - t0 < LOCATE_WAIT:
while time.time() - t0 < LOCATE_WAIT and (time.time() - T0) < GLOBAL_TIMEOUT:
```
并在循环退出后区分两种退出原因（本地超时 vs 全局超时），日志明确写出，避免排障时误判。

**负向测试建议**：AST 断言 `LOCATE_WAIT` 所在的 `while` 条件中同时出现 `GLOBAL_TIMEOUT` 引用。当前无此断言。

---

## 二、中优先级（建议排入下次迭代）

### P1-1 `client_white_ratio()` 把"取不到窗口矩形"与"真白屏"混为一谈

**位置**：`signin.py` L676-L690（三处 `return 1.0`）

**触发条件**：`win_rect()` 返回哨兵值（`l <= -30000`，即窗口已销毁/最小化）或 `reg.size == 0`（窗口完全在屏幕外）。

**影响范围**：调用方（L1084 等）按 `占比 > 0.85 判定白屏` → 此路径下必然判为"白屏" → 触发 `restart_wechat_and_enter()` **杀微信重启**。而真实原因可能是"窗口句柄已失效"，此时重启微信属于**代价极高的错误自愈**（约 40 秒 + 重新加载小程序的全部风险）。

**严重程度**：中。方向上是"保守"（宁可多重启一次），但重启微信本身有副作用，且会掩盖真实错误。

**修复建议**：区分两种语义 —— 返回 `None` 表示"测不了"，调用方单独处理：
```python
def client_white_ratio(hwnd):
    """... 返回 None 表示'无法测量'（窗口已失效），调用方应重新取窗口而非判白屏。"""
    try:
        l, t, r, b = win_rect(hwnd)
        if l <= -30000 or t <= -30000:
            return None          # 窗口已销毁/最小化 —— 不是白屏
        full = screen_bgr(); H, W = full.shape[:2]
        x1, x2 = max(0, l), min(W, r); y1, y2 = max(0, t), min(H, b)
        reg = full[y1:y2, x1:x2]
        if reg.size == 0:
            return None          # window 在屏幕外 —— 不是白屏
        g = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
        return float((g > 240).mean())
    except Exception as e:
        logger.warning(f"[白屏] 测量失败，按'未白屏'处理以免误触发重启: {e}")
        return None
```
调用方：`wr = client_white_ratio(h)`，`if wr is None: 重新取窗口/继续等待`，`elif wr > 0.85: 走白屏自愈`。

---

### P1-2 `_path(base_dir)` 层级语义无校验，传错会静默新建目录、台账悄悄分裂

**位置**：`history.py` L48-L50

```python
def _path(base_dir=None, name=None):
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, HISTORY_DIR, name or HISTORY_FILE)
```

**触发条件**：调用方把 `base_dir` 理解成"data 目录"而非"项目根目录"，传成 `.../data`。

**实测复现**（我在验证阶段亲自踩中）：
```
传入 base_dir = D:\签到系统\data
→ 实际写入 D:\签到系统\data\data\signin_history.csv
→ 目录被静默创建，新记录写进新文件
→ 主台账 data\signin_history.csv 纹丝不动
→ 但 append_record 返回非 None（声称成功）
```

**影响范围**：台账静默分裂成两份，`report.py` / `should_escalate()` 只读其中一份 → "连续失败告警"可能永久不触发。这正是本项目**已经踩过一次**的坑（见 `append_record` docstring 里 2026-09-15 的记录：台账长期为空导致告警从未生效）。

**严重程度**：中。当前主流程调用不带 `base_dir`（用默认值，安全），只有 `smoke_test.py` 显式传入且传对了。**属于防御性缺口，不是现存故障**。

**修复建议**：①`append_record` 里加一句自检：
```python
if base_dir and os.path.basename(os.path.normpath(base_dir)) == HISTORY_DIR:
    _warn("[台账] base_dir 疑似传成了 data 目录本身（应传项目根目录）：%s" % base_dir)
```
②或在 docstring 中把 `base_dir` 明确标注为"项目根目录（其下会拼 data/）"，并加一个负向测试：传 `data` 目录时应产生明确告警。

---

### P1-3 `_prune_old_runs()` 每次启动全量扫描 + 逐目录读文件，且异常整体吞掉

**位置**：`signin.py` L190-L217，模块顶层调用（L223）

**触发条件**：每次启动必然执行。

**实测现状**：
```
logs/ 下 run_* 目录数: 150
其中文件总数约: 319
```
`_prune_old_runs()` 对**每个**目录调用 `_run_outcome()`（内含 `os.path.isfile` + 打开 `result.txt` 读 400 字节 + 失败时 `os.listdir`），即 150 次目录读 + 最多 150 次文件读。虽实测总耗时不高（毫秒级），但它是**同步阻塞在日志初始化之前**的，且 `except Exception: pass` 会把任何异常（如权限、目录被占用）**整体吞掉**。

**影响范围**：①轻微拖慢启动；②若清理逻辑本身坏了（例如 `LOG_DIR` 不可写），**不会有任何日志**，日积月累直到磁盘满才发现。

**严重程度**：中低。属"能工作但不透明"。

**修复建议**：
```python
def _prune_old_runs():
    try:
        ...
    except Exception as e:
        # 原来整体吞掉 —— 清理失败必须被看见，否则磁盘会悄悄涨满
        _prune_warn("清理旧运行目录失败（不影响签到）: %s: %s" % (type(e).__name__, e))
```
其中 `_prune_warn` 走 `sys.stderr`（与 `history._warn` 同策略，主程序会把 stderr 收进 `run.log`）。另外建议把这两次 `_prune_*()` 调用**从模块顶层挪进 `main()` 开头**，与 `flush_pending()` 放一起 —— 理由：模块顶层执行意味着**import 即产生副作用**，这也是 `smoke_test.py` 不敢 import `signin.py` 的根本原因（见测试文件注释），挪进 `main()` 会让静态测试与复用都更容易。

---

### P1-4 `reconnect_wifi()` 与 `current_wifi_ssid()` 的 SSID/profile 解析逻辑不一致

**位置**：`signin.py` L2206-L2217（`reconnect_wifi`）vs L368-L372（`current_wifi_ssid`）

| | 键名匹配 | 取值方式 |
|---|---|---|
| `reconnect_wifi` | `key in ("ssid",)` / `key in ("profile","配置文件")` | `line.split(":", 1)` |
| `current_wifi_ssid` | `startswith("ssid")` | 正则 `^\s*SSID\s*:\s*(.+)$` |

**影响范围**：两处对同一份 `netsh` 输出的解析口径不同。`current_wifi_ssid` 用 `startswith("ssid")` 会匹配到 `"SSID"` 也可能匹配到别的以 ssid 开头的键；`reconnect_wifi` 用精确 `==` 则不会。**两者对中文/英文输出的兼容性也不同**（前者不认"配置文件"）。

**严重程度**：中低。当前两处各自能工作，但**维护时极易改一处忘另一处**。

**修复建议**：抽出公共函数 `parse_wlan_interfaces(text) -> {"ssid":..., "profile":..., "state":...}`，两处共用。顺便统一与 P0-2 的 state 解析，一处修全修。

---

### P1-5 `first_card_status_green()` 在坐标复核时重复计算全部 HSV 掩码

**位置**：`signin.py` L1594-L1598

```python
_sub = full[y1:y2, x1:x2]                              # 与外层 sub 完全相同
_hsv = cv2.cvtColor(_sub, cv2.COLOR_BGR2HSV)           # 与外层 hsv 完全相同
_m = cv2.inRange(_hsv, (35, 70, 60), (87, 255, 255))   # 与外层 mask_g 完全相同
_m = cv2.morphologyEx(_m, cv2.MORPH_CLOSE, kern)       # 与外层 m_g 完全相同
_cs, _ = cv2.findContours(_m, ...)                     # 与外层 cnts 完全相同
```

**实测影响**：`full` 是全屏截图（1920×1080 或更高）。`cvtColor` + `inRange` + `morphologyEx` + `findContours` 四步重复执行，**单次约 30-50ms**。该函数在 `LOCATE_WAIT` 循环里被高频调用（每 ~1.5 秒一次，最多 120 秒 = 80 次），累计约 2.4-4 秒纯浪费 —— 在高频轮询路径上不值得。

**严重程度**：低（性能，不影响正确性）。

**修复建议**：直接复用外层变量。外层已有 `m_g` 和 `cnts`，只需把 `_c` 循环改成遍历 `cnts`：
```python
if has_green:
    ...
    bx = by = None
    for _c in cnts:                      # 复用外层findContours结果
        _x, _y, _w, _h = cv2.boundingRect(_c)
        if (_w, _h) == (w, h) and int((m_g[_y:_y+_h, _x:_x+_w] > 0).sum()) == px:
            bx, by = x1 + _x, y1 + _y
            break
    if bx is not None and rect:
        ...
```
注意：**必须确认 `kern` 与外层一致**（当前代码里就是同一个 `kern`，安全）。改完要跑真实样本回归，确认坐标结果不变。

---

### P1-6 `_run_outcome()` 把 `unknown` 归入失败保留期，让"读不出结果"的目录占 90 天

**位置**：`signin.py` L205-L206

```python
outcome = _run_outcome(os.path.join(LOG_DIR, d))
is_fail = outcome in ("fail", "crash", "unknown")
```

**影响范围**：`unknown` 的语义是"`result.txt` 不存在且截图无标记"—— 最常见的成因是**进程被强杀（没走到收尾，所以没写 `result.txt`）**。这类目录按 90 天保留。而它们**恰恰是 P0-1 场景的产物**，数量会随强杀次数增长。

**严重程度**：低。方向保守（多留证据），但会让 `logs/` 长期膨胀。

**修复建议**：保持保守取向不变，但**在保留期内为 `unknown` 单独设置一个折中期限**（如 30 天），并在清理日志里分别统计 `fail`/`unknown` 数量，让"最近强杀了多少次"变成一个可见数字。这比单纯缩短期限更有价值 —— 它把隐藏信息暴露出来了。

---

## 三、低优先级（改进项，可择机处理）

### P2-1 `wifi_auto_login.py` 的模块级日志文件句柄未用 `with`/`finally` 兜底

**位置**：`wifi_helper/wifi_auto_login.py` L50（模块级 `open`）+ L556/L559/L566/L568（各分支手动 `close`）

**触发条件**：`__main__` 块中若在 `_log_file` 关闭前发生 `sys.exit()`（如 L570 的 `sys.exit(code)` 在 `__main__` 里，但关闭已在其之前 —— 当前顺序正确）。

**实测结论**：当前**所有分支都正确关闭了**（L556-559 成功路径、L566-568 异常路径），且作为独立进程运行、进程退出后句柄自动释放。**不构成实际泄漏**。

**建议**：这是唯一一处不用 `with` 的 `open`（全项目扫描结果），建议统一为 `contextlib.ExitStack` 或至少加注释说明"模块级句柄是有意为之，供 `_log()` 全局使用"。**属一致性改进，非缺陷。**

### P2-2 `net_state()` 只读 2048 字节可能截断门户特征串

**位置**：`signin.py` L392

**触发条件**：门户页面较长，`PORTAL_MARKERS` 中的某个词出现在 2048 字节之后。

**实测分析**：`PORTAL_MARKERS` 全部是 Dr.COM 登录页的表单字段名（`DDDDD` / `upass` / `authloginpath` 等），这些**一定出现在 HTML 头部附近**，2KB 足够覆盖。**判定为风险极低，可不动。**

**建议**：仅在注释里补一句"2048 字节足以覆盖门户特征（都是表单字段名，位于 head 附近）"，避免后人误改成全量读取。

### P2-3 `_within_signin_window()` 与 `before_signin_start()` 容错方向相反但都合理

**位置**：L2817-L2828 vs L108-L122

**实测分析**：

| 函数 | 解析失败时返回 | 保守方向 | 是否合理 |
|---|---|---|---|
| `before_signin_start()` | `True` | "按未开始"→多走一遍详情页 | ✅ 合理（防把昨天记录当今天已签） |
| `_within_signin_window()` | `True` | "按在窗口内"→多跑一轮补救 | ✅ 合理（防过早放弃） |

**结论**：两者返回值相同但语义不同（一个是"未开始"，一个是"在窗口内"），**都是正确取向**。已有注释说明。**无需修改，仅建议在 docstring 里互相引用**，让读者一眼看到"这两个 True 不是同一个意思"。

### P2-4 `_prune_old_logs()` 对 `signin_*.log` 的日期解析依赖文件名格式

**位置**：`signin.py` L235-L249

**实测分析**：已校验 `len(d) == 8 and d.isdigit()`，且显式跳过今天。逻辑健全。唯一未覆盖的是"未来日期的文件名"（如手改出的 `signin_20991231.log`）——会被保留。**属可接受行为。**

### P2-5 `report.py` 的 `days` 参数无范围校验

**位置**：`report.py` L42-L47

**实测复现**：
```
recent_records(-1) = []
stats(-1)['streak_desc'] = ''      # 空字符串
should_escalate(-1) = False
```

**影响**：传负数或极大值（`python report.py 999999`）不会崩溃，但会输出误导性的"最近 -1 天没有记录"。**纯 CLI 工具，影响仅限手动执行。**

**建议**：
```python
if not (1 <= days <= 3650):
    print("天数需在 1~3650 之间")
    return 2
```

---

## 四、已验证但**判定为非缺陷**的项（避免后续重复排查）

| 项 | 初步怀疑 | 实测结论 |
|---|---|---|
| `notify_helper` 卡片发送失败降级 | 可能丢通知 | ✅ 卡片→纯文本各重试 2 次，含 token 过期刷新重试（L187-191），设计健全 |
| `browser_login._close_browser()` 资源泄漏 | 可能留残留窗口/profile | ✅ 有 `finally` 兜底（L659）+ 三层关闭（CDP→terminate→taskkill /T /F）；实测 `%TEMP%` 下 `wifi_login_profile_*` 残留数 = **0** |
| `history.append_record` 表头不一致时覆盖旧数据 | `os.rename` 撞名覆盖 | ✅ `alt` 带秒级时间戳，实测两次调用命名错开，**不覆盖** |
| `step_tracer.flush()` 写盘中断 | 可能留半截 JSON | ✅ 临时文件 + `os.replace` 原子替换，设计正确 |
| `subprocess` 缺 `timeout` | 可能永久挂起 | ✅ 全项目扫描后，仅 `Popen`（有意不等待）和 `shutil` 类调用无 timeout，**关键等待路径全部带 timeout** |
| 可变默认参数 / `eval` / `exec` 滥用 | 常见陷阱 | ✅ 全项目**零命中**（`smoke_test.py` 的 `exec` 是 AST 测试装置，`browser_login` 的 `eval` 是 CDP 协议方法名） |
| 无限重试 / 死循环 | 可能卡死 | ✅ 所有 `while` 均有 `deadline` 或计数上限 |
| `net_state()` urllib 句柄泄漏 | 无 `with` | ✅ 用了 `with urlopen(...)`，已正确释放 |

---

## 五、修复优先级建议（按"风险 ÷ 成本"排序）

| 序 | 编号 | 事项 | 风险 | 成本 | 建议 |
|---|---|---|---|---|---|
| 1 | P0-2 | WiFi `connected` 子串误判 | 高 | **极低**（改 3 行） | **立刻修**，配负向测试 |
| 2 | P0-1 | `atexit` 兜底失效 | 高 | 中（需加信号 + 残留标记） | **立刻修**，至少先加 `signal` |
| 3 | P0-3 | 全局超时只查轮边界 | 中高 | **低**（改 3 处 while 条件） | 本次一并修 |
| 4 | P1-1 | 白屏判定混淆"测不了" | 中 | 低 | 下次迭代 |
| 5 | P1-5 | 重复 HSV 计算 | 低 | 低 | 下次迭代 |
| 6 | P1-3 | 清理逻辑静默 + import 副作用 | 中低 | 中 | 下次迭代 |
| 7 | P1-2/P1-4 | 层级语义 / 解析口径不一致 | 中低 | 中 | 择机 |
| 8 | P1-6 / P2-* | 保留期 / CLI 校验 / 注释 | 低 | 低 | 择机 |

---

## 六、复查方法说明（供复核）

本次所有结论均通过以下手段取得，不含"读代码猜测"：

1. **实测复现**：`atexit` 三种终止方式对比实验（子进程 + 标记文件）；`reconnect_wifi` 判据喂 4 组真实 `netsh` 输出样本；`history._path` 传入错误层级观察目录分裂；`report.py` 负数参数。
2. **静态全量扫描**：全项目 `subprocess` 无 timeout、`except: pass` 计数（`signin.py` 99 处 / `wifi_auto_login` 21 处 / `browser_login` 25 处 / `feishu_notify` 16 处）、可变默认参数、`eval`/`exec`、无 `with` 的 `open`。
3. **量化分析**：`logs/` 目录规模（150 个 run_ 目录 / 319 文件）、三轮最坏耗时（911→1170 秒）、重复计算单次开销（30-50ms × 80 次）。
4. **`settrace` 逐行追踪**：用于验证 `append_record` 的执行路径（据此发现是我自己的传参错误，而非代码 bug —— **这条自我纠错同样记录在案，避免把测试失误误报成缺陷**）。

**未覆盖（诚实声明）**：
- `wifi_helper/browser_login.py` 的 CDP 协议实现细节（约 600 行）仅做了函数结构与资源释放审查，**未逐行核对 WebSocket 帧解析与超时语义**。
- `smoke_test.py` 1467 行的测试断言有效性**未做元审查**（即"测试本身是否真的在测它声称要测的东西"）。
- 真实签到流程**仍未端到端跑过**（沙箱无 GUI 登录态），所有 GUI 相关结论均为静态推理。
- `logs/samples/` 真实样本仍各只有 1 张，「已结束」OCR 40% 的结论未经更多样本加固。

---

## 七、修复记录（2026-09-16）

### 已修复并提交

| 编号 | 问题 | 修复要点 | 测试 |
|---|---|---|---|
| P0-2 | WiFi 链路 `connected` 子串误判 | 抽 `parse_wlan_interfaces()` + `wifi_link_connected()`，改"取状态行 + 白名单精确匹配" | 9 组 netsh 样本 + 5 项静态检查 |
| P0-3 | `GLOBAL_TIMEOUT` 只查轮边界 | 把 `(time.time()-T0) < GLOBAL_TIMEOUT` 写进 3 个长等待循环条件 | AST 校验 3 个循环 |
| P0-1 | `atexit` 强杀下失效 | **三层兜底**：atexit + signal + **残留标记（主保障）** | 真实强杀实测 + 6 场景 + 3 分支 |

测试从 **109 → 114 项**，全部通过，连跑 6 次结果一致。

### P0-1 修复的详细设计（为什么这样选）

**为什么不只加 signal**：`signal` 能覆盖 Ctrl+C 和 `terminate()`，但 **`taskkill /F`
是内核级终止，进程收不到任何信号**。而计划任务超时强杀（`install_task.ps1` 设了
`ExecutionTimeLimit = 30 分钟`）走的正是这条路。所以 signal 只是"时效优化"，
**必须有持久化机制兜底**。

**三层分工**：

| 层 | 覆盖场景 | 特点 |
|---|---|---|
| ① `atexit` | 正常退出 | 保留（无害），但正常退出本就发过通知，实际用不上 |
| ② `signal.SIGINT/SIGTERM` | Ctrl+C / 关窗口 / `terminate()` | **当场**发提醒，时效最好 |
| ③ **残留标记** | **`taskkill /F` / 计划任务强杀** | **唯一能覆盖全部场景**，下次启动代为报告 |

**残留标记的实现要点**：
- 路径 `.agent/_in_progress.json`（已被 `.gitignore` 忽略，与 `self_heal.py` 同处）
- **只在首次点击"完成签到"之后落盘** —— 这是"值得告警"的门槛。还没点过签到的中断
  没有信息量（下次正常跑就行），不该制造噪音
- **原子写盘**（临时文件 + `os.replace`），避免下次启动读到半截 JSON
- `main()` **开头**（在 `notify_start()` 之前）检查残留 → 补发 → 立即清除，
  确保不重复告警
- 标记损坏 / 缺 `run_dir` → 删除并按"无残留"处理（避免坏标记永久卡住每次启动）
- 提醒里带上：上次开始时间、运行目录、**现场截图名**；目录已被清理时明确说明

**实测证据（真实强杀链路）**：
```
场景：taskkill /F（计划任务超时走这条）
  atexit 是否执行      : **否**
  残留标记是否留存      : 是
  结论                  : 残留标记兜住了
场景：terminate()（任务管理器'结束任务'）
  atexit 是否执行      : **否**
  残留标记是否留存      : 是
  结论                  : 残留标记兜住了
```

### 本次复查中我自己的一个失误（记录在案）

写 P0-1 的测试断言时，D 项"必须用 `os.replace` 原子替换"最初写成：
```python
mark_ok = ("os.replace" in ast.dump(n)) or ("os.replace" in src[...])
```
后半段在**源码字符串**里搜 —— 而 `"os.replace"` 在 `step_tracer.py` 的注释和别处也出现，
必然命中，导致**该断言永远是 PASS 的摆设**。负向测试（把 `os.replace` 换成
`shutil.move`）时它**没 FAIL**，才暴露出来。
→ 这正是项目里记过的"**不能用字符串 `in` 判断代码有没有做某件事**"的坑，我又踩了一次。
现已改为纯 AST：只在该函数节点内部找 `os.replace` 的**真实调用节点**，
重做负向测试后正确 FAIL。

### 未修复（保留待决）

- P1-1 `client_white_ratio()` 混淆"测不了"与"真白屏"
- P1-2 `history._path(base_dir)` 层级语义无校验
- P1-3 `_prune_old_runs()` 静默吞异常 + 模块顶层副作用
- P1-4 SSID/profile 解析口径不一致（**P0-2 修复时已顺带统一**）
- P1-5 `first_card_status_green()` 重复计算 HSV 掩码
- P1-6 `_run_outcome()` 把 `unknown` 归入 90 天失败保留期
- P2-1 ~ P2-5 各项一致性/注释改进
