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

---

# 八、全局复审（第二轮，2026-09-16 续）

第一轮修完 P1/P2 后做的**全局复审**，逐维度核查：

## 8.1 复审覆盖的维度与结论

| 维度 | 核查方式 | 结论 |
|---|---|---|
| 逻辑正确性 | 全项目扫描 + 关键路径逐行读 | 发现 2 处（P1-4 / P1-7）|
| 边界情况 | **时间/跨天边界专项** | 发现 2 处（P1-4 跨午夜 / P1-7 跨天归属）|
| 错误处理 | 静默 `except` 全量扫描 | 决策路径 0 处；非决策路径发现 1 处（P1-9）|
| 安全性 | 凭据硬编码 / gitignore / 危险命令 | **无缺陷**（config.json 已忽略、无硬编码、删除均有守卫）|
| 性能与资源使用 | AST 扫未关闭句柄 + 重复计算 | 发现 1 处句柄泄漏（P2-7）；1 处已在前轮修 |
| 可读性与命名 | AST 扫命名规范 / 超长函数 | **无违规**（全部 snake_case；2 个 >200 行函数为已知结构债）|
| 编码一致性 | 全量 `.bat` 按 chcp 声明实解 | 发现 1 处（P1-8）|
| 时区一致性 | 全项目扫 UTC/本地混用 | **无缺陷**（统一本地时间，fromtimestamp 与 now 同源）|
| 并发/时序 | 守卫标记读写时序 | **无缺陷**（原子写 + 单进程语义）|
| 运维脚本 | 逐行读 .bat / .ps1 | 编码 1 处（P1-8）；任务设置**经复核无缺陷** |

## 8.2 第二轮发现清单（位置 / 分级 / 判定依据）

### P1-4 时间窗：跨午夜静默失效 + 无范围校验 + 三处各自实现
- **位置**：`signin.py` — `before_signin_start()` / `_within_signin_window()` / `main()` 前置提示
- **严重程度**：P1（静默失效，符合本项目最该防的类型）
- **判定依据**：三处都用 `start <= now <= end`。跨午夜窗口（如 23:50~00:10）下
  `start > end`，该式**恒为 False**。实测对照：
  ```
  跨午夜 23:50~00:10，now=23:55：旧=False  新=True
  跨午夜 23:50~00:10，now=00:05：旧=False  新=True
  ```
  即"窗口设成跨午夜 → 程序认为永远不在窗口内"，不报错、只是判断反了。
  另 `int(split(":"))` 无范围校验：`'25:70'` → 1570，被当成合法分钟数参与比较。
- **修复**：抽出 `_parse_hhm()`（含 `0<=hh<=23` / `0<=mm<=59`）与
  `_window_contains()`（`start>end` 时按并集 `now>=start or now<=end`），
  三处统一收敛；`main` 的 `_near` 增加模 1440 环绕（否则 23:50 的窗口在 00:05
  会被算成"相差 1400 多分钟"）。
- **影响**：现网配置 20:50~21:30 逐点对照新旧 `_near` **完全一致**，无回归。

### P1-7 台账：跨午夜日期归属错误
- **位置**：`history.py` `append_record()`（`row` 的 `date` / `weekday`）
- **严重程度**：P1
- **判定依据**：`date` 一律取 `datetime.now()`（= **结束时刻**）。一次运行可能跨午夜
  （23:58 开跑、00:03 收尾），那次签到**属于前一天**。记成次日会让
  `recent_records()` 日期过滤、`stats()` 的 `fail_streak`、`should_escalate()`
  整体错位一天。
- **修复**：有 `start_ts` 时以**开跑时刻**所在日期为准（`weekday` 同步）；
  无 `start_ts`（历史导入）安全回退到今天。

### P1-8 `.bat` 编码与 `chcp` 声明不一致
- **位置**：`看签到统计.bat`（正文 UTF-8，第 2 行 `chcp 936`）
- **严重程度**：P1（用户直接可见的功能性乱码）
- **判定依据**：cmd 按 936 解 UTF-8 字节。实测复刻 cmd 解码结果：
  ```
  L5  显示: title 绛惧埌鍑哄嫟鎶ヨ〃
  L8  显示: <非法 GBK 序列 → 方块>
  L16 显示: <非法 GBK 序列 → 方块>
  L17 显示: <非法 GBK 序列 → 方块>
  ```
  同目录其余 6 个含中文的 `.bat` 均为 GBK，**只有它不一致**。
- **修复**：转 GBK 正文（语义不变，583→523 字节），与其余 `.bat` 统一。

### P1-9 自愈状态：写失败完全静默 + 非原子写
- **位置**：`self_heal.py` `_save()`
- **严重程度**：P1（静默失效 + 状态损坏）
- **判定依据**（两条独立缺陷）：
  1. `except Exception: pass` → 写失败**零日志**。链条（实测确认）：
     state 不落盘 → 下次 `_load` 读到旧/空 state → `preheat()` 判
     "无上次失败记录"跳过预热 → **自愈永久停摆且零提示**。
     与"失败要暴露不能掩盖"直接冲突。
  2. 直接 `open(...,"w")` 覆盖写：进程恰好被强杀会留下**半截 JSON** →
     下次解析失败 → `state={}` → `consecutive` 计数清零 → **冷却机制失效** →
     同一故障被无限预热。
- **修复**：改「临时文件 + `os.replace`」原子写；失败时往 stderr 明确告警。
  与 `_guard_mark_in_progress()` / `step_tracer.flush()` 的做法统一。

### P2-6 `step_tracer.py` 的定位与可见性需收敛
- **位置**：`step_tracer.py` 模块 docstring + `flush()`
- **严重程度**：P2（文档与实现语义不一致，会误导后续维护）
- **判定依据**：模块 docstring 写"任何记录异常只静默忽略"，但自本轮起
  `step_trace.json` 已是 `_run_outcome()` 的**第 4 级判据来源** ——
  它不只观察层。写失败会让"上次成没成"少一条判据。
- **修复**：`flush()` 失败改为往 stderr 明说（**仍不抛**，不影响主流程）；
  docstring 措辞相应收敛。逐步骤 `signal/recovery` 保持静默（真的只是锦上添花）。

### P2-7 网络响应句柄泄漏
- **位置**：`wifi_helper/browser_login.py` `capture_portal_url()`
- **严重程度**：P2
- **判定依据**：`opener.open()` 在**非重定向**路径（真连通、未抛 `HTTPError`）
  不会进 `except`，返回的 response 原来直接丢弃 —— 底层 socket 要等 GC 回收。
  该函数在**每次 wifi 自愈检查**里都会被调用，攒下来可能耗尽句柄。
- **修复**：改 `with opener.open(...)` 显式关闭。

## 8.3 判定为**非缺陷**的项（避免后续重复排查）

| 项 | 初判 | 复核结论 |
|---|---|---|
| `stats()` 的 `_rank` 字典与注释矛盾 | 疑似 P1 | **非缺陷**：注释 `success > fail > crash > not_time` 与代码 `{'success':3,'fail':2,'crash':1,'not_time':0}` 完全一致，实测"先 fail 后 not_time"正确判为 fail |
| `install_task.ps1` 缺少 `-ExecutionTimeLimit` / `-MultipleInstances` | 疑似 P1 | **非缺陷**：**是我的扫描工具有 bug** —— 按 `utf-16` 解码导致输出截断。该文件实际已含 `-WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew` |
| `wifi_auto_login.py` L72 裸 `open` 未用 `with` | 疑似句柄泄漏 | **有意设计**：懒打开的长生命周期日志句柄，已注册 `atexit.register(_close_log)` |
| `wifi_auto_login.py` L184 `opener.open` | 疑似泄漏 | **非缺陷**：已在 `with resp:` 内 |
| 超长函数 `main()`(308 行) / `click_sign_button()`(276 行) | 可读性风险 | **已知结构债，非本批新增**：改动它们风险 > 收益，不符合"不要修坏" |

## 8.4 本批测试与验证

- 测试：**132 → 148 项**（新增 16 项）
- **6 组负向对照全部实测 FAIL**（改坏护栏 → 断言确实失败）：
  ```
  A  _window_contains 退回 start<=now<=end  → FAIL 2 项（含"6/6 点被判不在窗口内"）
  B  _parse_hhm 去掉范围校验               → FAIL 1 项（'25:70' = 1570）
  C  台账忽略 start_ts                     → FAIL 1 项（记成 09-16，期望 09-15）
  D  .bat 改回 UTF-8 正文                  → FAIL 2 项
  E  _save 退回静默 + 非原子               → FAIL 2 项
  F  portal 探测不关响应                   → FAIL 1 项（行号 356）
  ```
- 连跑 5 次结果一致（148/148）
- **负向测试前先保存干净基线并记录 sha256**（这是上一轮踩过的坑：
  备份被前一次注入污染，导致负向测试在错误基线上跑）

## 8.5 本轮我自己的一个失误（记录在案）

复审 `install_task.ps1` 时，扫描脚本按 `utf-16` 解码，输出被截断，
于是"发现"它缺少 `-ExecutionTimeLimit` 和 `-MultipleInstances`，
**差点把一条不存在的缺陷写进报告并去"修复"**。

读原文件才发现两处防护都在，文件本来就是对的。

教训：**扫描工具的输出不能直接当结论** —— 尤其涉及编码时，
先验证"工具是否真的读到了完整内容"，再判断内容对不对。
这与项目里记过的"注释不是证据""合成样本不能验证可用性"是同一类错误：
**把"我的观测方式"当成了"事实本身"。**

---

# 九、全局复审（第三轮，2026-09-16 续）—— 元审查 + CDP 协议复核

本轮专门啃**上一轮诚实声明"未做"的两块**：
① `smoke_test.py` 全部断言的有效性（元审查）；
② `wifi_helper/browser_login.py` 的 CDP 协议实现。

## 9.1 先处理的一件事：工作区残留（本轮开始时发现）

**上一轮被中断时，`signin.py` 被留在了"负向测试注入后"的状态。**

```
try:
    signin_history = None  # 这里本来有 import history 模块，被删掉了
except Exception as _he:
```

这是负向测试用例 B1 的注入产物 —— **真实导入被删掉了**，
意味着台账（`data/signin_history.csv`）会**静默不写**、连续失败告警永不触发。
它被"还原成功"的核对漏掉，是因为上一轮在 B1 用例**跑完后**就中断了，
恢复语句没能执行。

**已还原**：`import history as signin_history` 复位，语法校验通过。

**教训（新增，重要）**：
> **负向测试的"还原"必须写在 `finally` 里，且不能依赖"用例全部跑完"才收尾。**
> 进程被中断（超时/手动取消）时，`try/finally` 内的还原仍会执行，
> 而"跑完再统一还原"的方案一旦中断就会把注入留在文件里。
> 本次驱动器已用 `try/finally`，但中断发生在恢复写入之后、下一用例之前 ——
> 属于"最后一个用例的注入被留存"，靠"收尾核对"发现。
> **→ 收尾核对不能只看末尾一次，`finally` 里每个用例各自核对才有意义。**

## 9.2 元审查结论：两条真·摆设断言（已修复）

用负向测试驱动器把断言声称守护的代码逐个改坏，**改坏了仍 PASS 的即为摆设**。

| 用例 | 注入内容 | 改坏前 | 改坏后 | 裁决 |
|---|---|---|---|---|
| A1 | 去掉 `LOG_DIR` 的环境变量重定向 | — | FAIL 1 项 | ✅ 真护栏 |
| A2 | 去掉某处 `import signin` 前的 env 赋值 | — | FAIL 1 项 | ✅ 真护栏 |
| A3 | 点击后复核退回"只查一次" | — | FAIL 6 项 | ✅ 真护栏 |
| **B1** | 删掉真 `import history`，只在注释里留字眼 | PASS | **PASS** | ❌ **摆设置换** |
| **B2** | `client_white_ratio` 的 `return None` 退回 `return 1.0` | PASS | **PASS** | ❌ **无人守护** |

### B1 —— 假断言（源码字符串 `in`）

原判据：
```python
check("台账调用来自 history 模块",
      lambda: "import history" in src or "history as" in src or ...)
```
它只在**源码字符串**里找字眼。把真导入换成
`signin_history = None  # 这里本来有 import history 模块`
—— 导入彻底失效、台账静默不写 —— **154 项全过**。

这正是本项目在 P0-1 上**已经踩过并记录在案**的坑
（"不能用字符串 `in` 判断代码有没有做某件事"）。
**当时只修了 P0-1 本身，没把规则推广到别处。**

**修复**：改纯 AST —— 必须存在真实的 `ast.Import`（`history`）节点，
且**真实调用**过 `signin_history.append_record()` / `flush_pending()`。
"导入在但没人用"同样判 FAIL。

### B2 —— 修了缺陷却没配护栏

P1-1（`client_white_ratio` 混淆"测不了"与"真白屏"）**已修复**，
但**没有任何断言锁它**：把三处 `return None` 中的一处改回 `return 1.0`
（即"测不了 → 当成白屏 → 触发杀微信重启"这个**代价极高的错误自愈**回归），
154 项**全过**。

**修复**：新增 `test_white_screen_semantics()`，四层判据：
1. **AST**：`client_white_ratio` 内不得有**数值常量** return（`return 1.0` 的形态）
2. **AST**：必须有 ≥2 处 `return None`（"测不了"的显式分支）
3. **运行时**：`client_white_ratio(0)` 必须返回 `None`，`is_white_screen(0)` 必须 `(False, None)`
4. **AST**：必须有地方调用 `is_white_screen()`（防调用方绕过它直接用裸占比判）

**这里我自己又踩了一次坑**（第 3 次同类）：判据 1 第一版写成
`isinstance(n.value, ast.Constant)` —— 而 **`return None` 在 AST 里同样是
`ast.Constant(value=None)`**，于是把正确实现全打成 FAIL。改为只拦
`isinstance(n.value.value, (int, float))`。
→ 又一次印证：**写完就跑一次必须做**，否则这条护栏一上线就是红的。

## 9.3 CDP 协议复核：发现并修复 2 处缺陷

上一轮声明"约 600 行的 CDP 实现未逐行核对 WebSocket 帧解析与超时语义"。本轮逐行读。

### 先说**复核后判定为正确**的部分（避免后续重复排查）

| 项 | 核查方式 | 结论 |
|---|---|---|
| WebSocket 帧**发送**边界 | 复刻帧头逻辑，实测 n=0/125/126/65535/65536 | ✅ 全部正确（126/127 长度分支、掩码、`0x80` 位都对） |
| `recv_text()` **分片状态机** | 构造 6 组帧序列（单帧/两帧/三帧/连发/ping 插中间/孤立续帧） | ✅ 正确拼接，ping/pong 不污染消息体，孤立续帧会报错 |
| 控制帧校验（RFC 6455 §5.5） | 读代码 | ✅ 已校验 `length>125` 与 `FIN=0` 两种违规 |
| 二进制帧 / 孤立续帧 | 读代码 | ✅ 明确拒绝并给可诊断报错 |
| `close()` 幂等 | 读代码 | ✅ `try/except pass` 合理（关闭失败无补救手段） |
| `Browser.close` 只发不等 | 读代码 | ✅ 正确：回包前连接就断了，等它没意义 |

### 缺陷 1（P1，假成功方向）：`_internet_ok()` 漏判门户劫持

**位置**：`wifi_helper/browser_login.py` `_internet_ok()`

**两个独立的洞，方向都是"把被劫持误判成已通"**：

1. **不看最终 URL**。`generate_204` 被 AC 302 到门户时 urllib **自动跟随重定向**，
   于是 `r.status` 是 200、body 是门户页。而判据里
   `r.status in (200, 204)` 这个条件**永远为真** ——
   非 2xx 会抛 `HTTPError`，根本走不到这一行，等于没有校验。
   `signin.py` 的 `net_state()` 正是用 `r.geturl()` 查网关来兜这个的。

2. **特征串只有 3 个**（`eportal` / `ACSetting` / `Dr.COM`），
   而 `signin.py` 的 `PORTAL_MARKERS` 有 7 个。少掉的 `DDDDD` / `upass`
   恰恰是 Dr.COM 门户**登录表单的字段名** —— 也就是被劫持时最稳定出现的字眼。

**实测对照**（假响应驱动，见 9.4）：

| 场景 | 旧代码 | 新代码 | 期望 |
|---|---|---|---|
| 真连通（204 + 空 body） | True | True | True |
| **被 302 到网关（body 无特征串）** | **True ❌** | False | False |
| **门户页含 DDDDD/upass** | **True ❌** | False | False |
| 门户页含 eportal | False | False | False |
| 正常外网站点 | True | True | True |

**为什么这个方向的错误最贵**：判错方向的代价**不对称** ——
把"已通"误判成"没通"只是多等一轮/多登一次；
把"被劫持"误判成"已通"会让上层**报告登录成功**（假成功），
后面所有步骤都建立在错误前提上。

**修复**：补 `r.geturl()` 查网关 + 特征串与 `signin.py` 对齐（7 个）+ 读取上限提到 64KB。

### 缺陷 2（P2，静默失败 + 句柄泄漏）：`_eval_retry()` 两段 except 全 `return None`

**两个后果**：
1. **旧连接不关**：`_pick_page` 返回 None（页面已跳走/浏览器在退出）时直接返回，
   而 `box[0]` 里那个已坏掉的 `_CDP` 从未 `close()`，socket 一直挂着。
2. **根因被掩盖**（更要紧）：调用方拿到 None 只会看到 `state = {}`，
   判断成"页面还没渲染好"，继续 sleep 等下一轮直到 `wait_form` 超时。
   日志最后只有一句"没能识别出登录表单" ——
   **看不出是 WebSocket 断了还是端口没了**，排查方向直接跑偏。

**修复**：失败路径先关旧连接并置 `box[0] = None`，
把原因交给 `_log` 记可诊断日志（含异常类型与消息）。
返回值语义不变（仍返回 None），**不改变调用方的控制流** —— 只让"为什么没拿到"留痕。
`_eval_retry` 开头增加 `if box[0] is None: return None` 保护。

## 9.4 本轮测试与验证

**测试：155 → 156 项**（新增 `test_cdp_portal_hijack_detection`），
全部通过，连跑 5 次结果一致。

### 负向测试 A 组（元审查，5 用例）

```
A1 去掉 LOG_DIR 环境变量重定向          -> REAL（FAIL 1）
A2 去掉 import signin 前的 env 赋值      -> REAL（FAIL 1）
A3 复核退回「只查一次」                  -> REAL（FAIL 6）
B1 删真 import、只在注释留字眼           -> 修复后 REAL（FAIL 1）
B2 白屏「测不了」退回 1.0                -> 修复后 LOCKED（FAIL 1）
收尾核对：文件是否全部还原 -> 是
```

### 负向测试 C 组（CDP 新护栏，4 用例）—— **其中一条抓出了我自己的摆设**

```
C1 _internet_ok 不再检查最终 URL          -> REAL
C2 删掉 r.geturl() 调用本身                -> REAL
C3 PORTAL_MARKERS 退回原来 3 个词          -> REAL
C4 不再用 PORTAL_MARKERS 检查 body         -> 第一版 DUMMY！加强后 REAL
收尾核对：文件是否全部还原 -> 是
```

**C4 值得单独记**：第一版护栏的运行时用例只喂了
"最终 URL 是网关"的样本 → 于是"把 body 特征串检查删掉"这个回退**完全测不出来**。
补上"**门户原地返回 200、最终 URL 未变、只在 body 里露特征串**"的样本，
并加一条 AST 判据（`_internet_ok` 内必须**引用** `PORTAL_MARKERS`）之后才抓住。

→ **通用规则：运行时用例要覆盖"能区分新旧实现"的每一个分支。
没有对应样本的分支，等于没有护栏。**

## 9.5 本轮我自己的失误（记录在案）

1. **开头发现问题：上一轮中断把注入留在了 `signin.py` 里**（见 9.1）。
   根因是"还原依赖用例跑完 + 末尾统一核对"，中断即失守。
2. **判据 1 写错**：`return None` 也是 `ast.Constant`，第一版护栏把正确实现全打成 FAIL。
   再次印证"写完就跑一次"必须做。
3. **第一版 CDP 护栏的 C4 是摆设**，靠负向测试自己抓出来。

三条都指向同一件事：
> **护栏本身也要被验收。** 写完护栏要跑一次（贴 2）；
> 跑完还要故意改坏它守护的代码看它是否 FAIL（贴 3）。
> 少了任何一步，护栏就只是"看起来像测试"的注释。

---

## 9.6 追加发现：防污染护栏有盲区 + 一条"我自己写的摆设断言"

### 发现经过（诚实记录：从"我以为是新发现"到"承认测错了"）

在核对"测试有没有污染真实日志"时发现：当天 `logs/signin_20260916.log` 里有
**74 行测试噪音**（形如 `[台账] base_dir 传的是 data 目录本身（...Temp\smoke_basedir_xxx...）`），
与 1595 行真实运行记录混在一起。

**第一次尝试（失败）**：我判断根因是"`history.py` 用 `signin.history` 子 logger，
`propagate=True` 会上传到 `signin` 的按天日志 handler"，于是写了一条**运行时**断言：
临时关掉 propagate → 触发一次告警 → 核对真实日志字节数不变。

**负向测试立刻打脸**：三个改坏（删 `propagate=False` / 把核对改成 `if False:` /
删掉触发告警那行）**全部 PASS** —— 断言是摆设。

**实测查清真相**：
```
手写 import 探测（设了 override 后）：
  signin LOG_DIR = ...\Temp\probe_xxx
  FileHandler -> ...\Temp\probe_xxx\run_xxx\run.log
  FileHandler -> ...\Temp\probe_xxx\signin_20260916.log
```
—— **所有 handler 都指向临时目录**。也就是说，只要 override 在位，
真实 `logs/` 在那个进程里**收不到任何东西**。
那条"触发告警看它漏不漏"的断言，验证的是一个**当前不可能发生的场景**，
所以无论怎么改坏都会 PASS，**价值为零**。

### 真正的根因（本地测清）

74 行的产生有**两个**来源，第一个是修复前遗留、第二个是我本轮**新发现**的：

1. **修复前的历史遗留**：在 `SIGNIN_LOG_DIR_OVERRIDE` 机制加上之前，
   每次 `import signin` 都会往真实日志写。这部分是存量数据。
2. **override 覆盖不到的那条路径**：`test_history()` 里调 `H._path(data_dir)`
   会触发 `history._LOG.warning(...)`；而 `history.py` 用的是
   `logging.getLogger("signin.history")` —— 它是 `signin` 的**子 logger**。
   只要**任何一处** override 没覆盖到（历史上确实存在过），
   这条子 logger 的告警就会顺着 propagate 落进真实按天日志。

→ **教训**：`import signin` 的 4 处 override 只保护了"import 时的两行归档日志"，
**保护不了"运行期经由子 logger 转发的告警"**。判据必须落在**结果**上，而不是某个入口上。

### 正确修法：判据落在"已落盘的事实"上

改为直接扫真实按天日志的**内容**，只要出现测试专属特征串就是污染：
`smoke_`（临时目录前缀）/ `[测试]` / `probe_` —— 真实运行绝不会产生这些。

**这条断言会 FAIL，所以它是真断言**（负向测试实测）：

```
注入 1 行 noise 到 logs/signin_20260916.log
  -> [FAIL] 测试防污染：真实按天日志无测试特征串
     真实按天日志 signin_20260916.log 里有 1 行**测试特征串** …
  通过 156 项，失败 1 项
还原后 -> 通过 157 项，失败 0 项
```

### 数据清理（已征得用户确认后执行）

- **先备份**：`logs/signin_20260916.log.bak_20260916_1710`（sha256 `666386351dc3d77e`）
- **再清理**：删除 74 行噪音，**1669 → 1595 行**
- **校验**：真实记录完好（`[归档]` 159 条不变）；噪音计数归 0
- 清理后连跑 5 次冒烟，**噪音计数保持 0**（无新增污染）

### 这一条的元教训

> **一条"永远 PASS"的断言，和没有断言是一样的，但它更坏** ——
> 它让人以为这里有守护。**判据要落在"结果/事实"上，不要落在"某个入口有没有做某事"上。**
> 本次两个入口判据（4 处 override）都对，却仍然漏了"运行期子 logger 转发"这条路。

**并且**：我第一版把它写成"不可能发生的场景"，是**没想清楚就动手**；
是负向测试（D 组三个用例全 DUMMY）把我拽回来的。
→ **负向测试的最大价值不是验收护栏，是阻止你自己制造假护栏。**
