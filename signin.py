# -*- coding: utf-8 -*-
"""
油学通小程序自动签到脚本（搜索路径版 / 增强日志）
流程（签到入口与模板按油学通实际界面采集/校准）：
  先确保有网：检测无外网时自动连接 XSYU_WLAN 并完成校园网认证
    （wifi_helper/wifi_auto_login.py，独立模块，失败不阻塞签到）
  启动/激活微信 -> 处理"进入微信"确认页 -> 顶部搜索框搜索"油学通"
  -> 点"最近使用过的小程序"里的 油学通 -> 顺多级入口进入：日常管理 -> 签到消息 -> 每日签到 -> 每日签到里的进入按钮
  -> 详情页左边"签到" -> 地图定位页"完成签到" -> 硬确认"已签到"
  -> 是否关机由 config.json 的 shutdown_after_success 决定（当前默认 false，不关机）
说明：不依赖微信左侧栏小程序入口（4.x 该入口不固定），改用全局搜索，最稳。
"""

import os
import sys
import json
import re
import time
import subprocess
import shutil
import socket
import urllib.request
import ctypes
from ctypes import wintypes
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pyautogui
import pyperclip
import cv2
import numpy as np
from PIL import ImageGrab

import step_tracer
import self_heal

# 签到历史台账（纯新增，零副作用）：每次运行追加一行到 data/signin_history.csv。
# 导入失败时置 None，主流程所有调用点都要判空——台账绝不能反过来影响签到。
try:
    import history as signin_history
except Exception as _he:
    signin_history = None
    logging.getLogger().warning("history 导入失败（忽略，仅少写台账）: %s" % _he)

# 飞书通知模块（独立文件夹 notify_helper/，失败不影响签到主流程）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify_helper"))
try:
    import feishu_notify
except Exception as _e:
    feishu_notify = None
    logging.getLogger().warning("feishu_notify 导入失败（忽略）: %s" % _e)

# 高分屏/不同缩放适配：让本进程按物理像素工作。否则在系统缩放 125%/150% 的电脑上，
# 截图分辨率与鼠标坐标会整体错位导致点不准。从新版 API 逐级回退，100% 缩放下无副作用。
def _enable_dpi_awareness():
    cands = [
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),  # Per-Monitor V2
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),                          # Per-Monitor
        lambda: ctypes.windll.user32.SetProcessDPIAware(),                               # System DPI
    ]
    for fn in cands:
        try:
            fn(); return
        except Exception:
            continue
_enable_dpi_awareness()

# ==================== 基础配置 ====================
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:  # utf-8-sig 兼容带BOM的配置
    CONFIG = json.load(f)

def abs_path(p):
    return str((SCRIPT_DIR / p).resolve())

WECHAT_PATH = CONFIG["wechat_path"]
ENTER_WECHAT_BTN = abs_path(CONFIG["enter_wechat_btn"])
SEARCH_BOX_TPL = abs_path(CONFIG.get("search_box", "templates/07_search_box.png"))
POPUP_CLOSE = abs_path(CONFIG.get("popup_close", "templates/06_popup_close.png"))
POPUP_TITLE = abs_path(CONFIG.get("popup_title", "templates/08_popup_title.png"))
MINIAPP_SEARCH_ICON = abs_path(CONFIG["miniprogram_search_icon"])
SEARCH_SCALES = (0.62, 0.72, 0.82, 0.92, 1.0, 1.1, 1.22)
# 顶部搜索框模板的多尺度范围（窗口尺寸与采集时不同也能命中）
SEARCH_BOX_SCALES = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)
# 多级入口（21/22/23/24）模板匹配的缩放范围：窗口大小与采集时不一致也能命中
NAV_SCALES = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)
NAV_STEPS = []
for _s in CONFIG.get("signin_nav_steps", []):
    NAV_STEPS.append({
        "name": _s.get("name", "入口"),
        "template": abs_path(_s["template"]),
        "scroll": bool(_s.get("scroll", True)),
        # topmost 必须带进来：open_signin_entry() 靠它决定"这一步是否取列表最上的命中"。
        # 之前这里漏了，config 里写 "topmost": true 完全不起作用（只能靠步骤名撞默认值）。
        "topmost": bool(_s.get("topmost", False)),
    })
SEARCH_KEYWORD = CONFIG.get("search_keyword", "油学通")
MINIAPP_TITLE = CONFIG.get("miniprogram_title", "油学通")   # 小程序独立窗口标题（用作窗口查找/置顶关键词）
SIGNIN_TIME_START = CONFIG.get("signin_time_start", "20:50")  # 签到开放时间（仅提示用，不硬卡）
SIGNIN_TIME_END = CONFIG.get("signin_time_end", "21:30")      # 签到结束时间（仅提示用，不硬卡）

def before_signin_start():
    """当前时间是否在签到开放时间之前。
    用于防止：签到时段前列表/详情页显示的是昨天的'已签到'记录，被误判为今天已签。

    config 里 signin_time_start 写坏时（不是 HH:MM）**保守返回 True**：
    宁可多走一遍详情页/判成"未开始"，也不要静默把昨天的记录当今天已签。
    原来直接 `map(int, ...)`，配置写错会抛异常把整轮打挂。"""
    try:
        h, m = map(int, str(SIGNIN_TIME_START).strip().split(":"))
    except Exception as e:
        logger.warning(f"[前置] config.signin_time_start='{SIGNIN_TIME_START}' 解析失败（应为 HH:MM）: {e}"
                       f"，按'签到未开始'保守处理")
        return True
    now = datetime.now()
    return (now.hour, now.minute) < (h, m)
LOG_DIR = abs_path(CONFIG["log_dir"])
CONFIDENCE = CONFIG.get("confidence", 0.8)
SHUTDOWN_DELAY = CONFIG.get("shutdown_delay", 60)
SHUTDOWN_AFTER_SUCCESS = CONFIG.get("shutdown_after_success", True)
SCREENSHOT_ON_ERROR = CONFIG.get("screenshot_on_error", True)
# 截图压缩质量（JPEG，1~100）。0 或 false 表示不压缩、存原始 PNG。
# 背景：2880x1800 满屏 PNG 平均 1.0MB/张，一次运行 29 张 = 25MB，一天能吃掉几十 MB。
# 实测 JPEG q75 可压到约 37%（1.59MB -> 581KB）且保留原始分辨率，
# 排查问题时按钮文字、界面细节都还看得清——比"缩放"划算得多（缩到 50% 也才省一半）。
# 失败现场（FAIL_*/EXCEPTION）不受此设置影响，永远存原始 PNG，保证现场不失真。
SCREENSHOT_JPEG_QUALITY = CONFIG.get("screenshot_jpeg_quality", 75)
try:
    SCREENSHOT_JPEG_QUALITY = int(SCREENSHOT_JPEG_QUALITY)
except Exception:
    SCREENSHOT_JPEG_QUALITY = 75

# 校园网门户特征（与 wifi_helper/wifi_auto_login.py 保持一致）：
# 用于把"TCP 能连但被门户拦着"和"真能上外网"区分开，见 net_state()
PORTAL_GATEWAY = "10.123.0.253"
PORTAL_MARKERS = ("eportal", "ACSetting", "DDDDD", "upass", "Dr.COMWebLogin",
                  "authloginpath", "authuserfield")

os.makedirs(LOG_DIR, exist_ok=True)

# 每次运行独立归档到 logs/run_YYYYMMDD_HHMMSS/（run.log + 本次全部截图），保留最近 KEEP_RUNS 次
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_DIR = os.path.join(LOG_DIR, f"run_{RUN_ID}")
os.makedirs(RUN_DIR, exist_ok=True)
KEEP_RUNS = int(CONFIG.get("keep_runs", 10))
KEEP_DAYS = int(CONFIG.get("keep_days", 10))  # 日志最多保留天数（超过则删除，至少保留最近KEEP_RUNS次）
# 失败现场单独放宽保留：成功截图看一次就够了，但**失败现场是排查的唯一证据**，
# 而且往往是"过几天才发现漏签"才去翻。所以失败目录按更长的天数保留，
# 且不受 KEEP_RUNS 挤压（否则跑得多时会被"最近10次"挤掉）。
KEEP_FAIL_DAYS = int(CONFIG.get("keep_fail_days", 90))


def _run_outcome(run_dir):
    """判断一次运行的结果：'success' / 'fail' / 'not_time' / 'unknown'。

    优先读 result.txt（收尾时写的，最权威）；读不到就退回看截图文件名。
    判不出来时**保守当作 fail**——宁可多留一个现场，也不能把真失败当成功清掉。
    """
    # 1) result.txt 最权威
    try:
        rp = os.path.join(run_dir, "result.txt")
        if os.path.isfile(rp):
            with open(rp, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(400)
            m = re.search(r"结果:\s*(\w+)", head)
            if m:
                return m.group(1).strip().lower()
    except Exception:
        pass
    # 2) 退回看截图名：失败现场有 FAIL_ / EXCEPTION 标记
    try:
        for n in os.listdir(run_dir):
            if n.startswith("FAIL_") or "EXCEPTION" in n or "_FAIL_" in n:
                return "fail"
            if "签到成功_已签到" in n:
                return "success"
    except Exception:
        pass
    # 3) 判不出来：保守当失败（多留现场，不漏证据）
    return "unknown"


def _prune_old_runs():
    try:
        dirs = sorted([d for d in os.listdir(LOG_DIR)
                       if d.startswith("run_") and os.path.isdir(os.path.join(LOG_DIR, d))], reverse=True)
        now = datetime.now()
        # 失败目录用更长的保留期；成功/未知目录用原来的 KEEP_DAYS
        fail_cutoff = now - timedelta(days=KEEP_FAIL_DAYS)
        ok_cutoff = now - timedelta(days=KEEP_DAYS)

        protected = set()   # 还在保留期内的失败目录，绝不被 KEEP_RUNS 挤掉
        for d in dirs:
            try:
                dt = datetime.strptime(d[4:19], "%Y%m%d_%H%M%S")
            except Exception:
                continue
            outcome = _run_outcome(os.path.join(LOG_DIR, d))
            is_fail = outcome in ("fail", "crash", "unknown")
            if is_fail:
                if dt < fail_cutoff:
                    shutil.rmtree(os.path.join(LOG_DIR, d), ignore_errors=True)
                else:
                    protected.add(d)
            else:
                if dt < ok_cutoff:
                    shutil.rmtree(os.path.join(LOG_DIR, d), ignore_errors=True)

        # 至少保留最近 KEEP_RUNS 次（失败目录已在上面单独判定，这里不参与挤压）
        remaining = sorted([d for d in os.listdir(LOG_DIR)
                            if d.startswith("run_") and os.path.isdir(os.path.join(LOG_DIR, d))
                            and d not in protected], reverse=True)
        for old in remaining[KEEP_RUNS:]:
            shutil.rmtree(os.path.join(LOG_DIR, old), ignore_errors=True)
    except Exception:
        pass
_prune_old_runs()


def _prune_old_logs():
    """清理过期的「按天汇总日志」signin_YYYYMMDD.log。

    原来只清理 run_* 目录，按天日志从来不删、会一直长下去（每天约 40KB）。
    这里按 KEEP_DAYS 删旧日期，**今天的绝不动**（正被日志句柄占用，删也删不掉）。
    注意：`logs/task_run.log` 是 .bat 用 `>>` 重定向写的、脚本运行时一直被占用，
    既删不掉也轮转不了；它约 30KB/天、一年约 11MB，暂不处理。
    """
    try:
        today = datetime.now().strftime("%Y%m%d")
        cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
        for n in os.listdir(LOG_DIR):
            if not (n.startswith("signin_") and n.endswith(".log")):
                continue
            d = n[len("signin_"):-len(".log")]
            if d == today or len(d) != 8 or not d.isdigit():
                continue
            try:
                if datetime.strptime(d, "%Y%m%d") < cutoff:
                    os.remove(os.path.join(LOG_DIR, n))
            except Exception:
                pass
    except Exception:
        pass
_prune_old_logs()

# 步骤轨迹追踪器（纯观察，零副作用）：每次运行写 step_trace.json，机器可读失败定位
TRACE = step_tracer.StepTracer(RUN_ID, RUN_DIR)

# ==================== 日志 ====================
logger = logging.getLogger("signin")
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
# 1) 本次运行的独立完整日志
fh = logging.FileHandler(os.path.join(RUN_DIR, "run.log"), encoding="utf-8")
fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
# 2) 按天汇总日志（追加，不进子目录也能回看当天每次运行）
fh_day = logging.FileHandler(os.path.join(LOG_DIR, f"signin_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
fh_day.setFormatter(fmt); fh_day.setLevel(logging.DEBUG)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt); sh.setLevel(logging.INFO)
logger.addHandler(fh); logger.addHandler(fh_day); logger.addHandler(sh)
logger.info(f"[归档] 本次运行目录 run_{RUN_ID}（自动保留最近 {KEEP_DAYS} 天且至少 {KEEP_RUNS} 次运行）")

# 任何未捕获异常（含 main 之前的初始化阶段）都写进 run.log，避免“只留一行归档就消失、无法排查”
def _log_uncaught(exc_type, exc, tb):
    try:
        logger.error("发生未捕获异常，堆栈如下：", exc_info=(exc_type, exc, tb))
    except Exception:
        sys.__excepthook__(exc_type, exc, tb)
sys.excepthook = _log_uncaught

# 关键里程碑时间线：mark() 既打日志又记录，结束时统一汇总，便于一眼还原全过程
T0 = time.time()
TIMELINE = []
GLOBAL_TIMEOUT = 900   # 全局运行上限15分钟，防止脚本卡死（签到流程本身较短，15 分钟足够）
def mark(event, level="info"):
    el = time.time() - T0
    TIMELINE.append((el, event))
    getattr(logger, level, logger.info)("[里程碑 %7.1fs] %s" % (el, event))

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None

def log_startup_banner():
    import platform
    logger.info("#" * 58)
    logger.info("# 运行环境信息（排查用）")
    logger.info("# Python=%s" % platform.python_version())
    logger.info("# 脚本=%s" % os.path.abspath(__file__))
    logger.info("# 管理员权限=%s 屏幕=%s" % (is_admin(), tuple(pyautogui.size())))
    logger.info("# 配置: 搜索词=%s 置信度=%.2f 成功后关机=%s 关机延迟=%ss 日志保留=%s次"
                % (SEARCH_KEYWORD, CONFIDENCE, SHUTDOWN_AFTER_SUCCESS, SHUTDOWN_DELAY, KEEP_RUNS))
    logger.info("# 本次运行目录=%s" % RUN_DIR)
    logger.info("#" * 58)

# 无人值守定时任务：鼠标位置不可控，关闭"鼠标到屏幕四角即中止"的 fail-safe，
# 否则上一次运行把鼠标留在角落会导致本次一开始就抛 FailSafeException 整体失败。
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.15
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ==================== 无人值守系统级预防 ====================
def keep_awake():
    """整个运行期间阻止系统睡眠/自动熄屏（定时任务跑到一半睡过去，截图和点击会全部落空）。
    ES_CONTINUOUS 让设置持续生效，进程退出后自动还原系统电源策略，无需手动恢复。"""
    try:
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        logger.info("[电源] 已设置：脚本运行期间阻止系统睡眠/熄屏")
    except Exception as e:
        logger.warning(f"[电源] 设置防睡眠失败(忽略): {e}")

def is_workstation_locked():
    """检测 Windows 是否处于锁屏界面（存在 LogonUI.exe）。锁屏下 GUI 自动化必然失败，重试也没用。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LogonUI.exe"],
                           capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=8)
        return "LogonUI.exe" in (r.stdout or "")
    except Exception as _e:
        logger.debug("[锁屏] 检测失败，按未锁屏处理: %s" % _e)
        return False

def net_reachable():
    """TCP 连通性探测（比 ping 通用，校园网常禁 ICMP 但放行 TCP）。任一目标可连即视为有网。
    注意：不要把 53 端口的 DNS 目标放在前面。Windows 的"保留端口段"（Hyper-V/WSL 会占用）
    可能临时把 53 圈进去，此时连 223.5.5.5:53 会稳定报
    WinError 10013「以一种访问权限不允许的方式做了一个访问套接字的尝试」——
    2026-09-07 的日志里就出现过，会让探测变成假阴性。443 目标不受影响，所以放最前面。"""
    targets = (("223.5.5.5", 443),        # AliDNS 的 DoH 端口
               ("www.baidu.com", 443),
               ("www.qq.com", 443),
               ("223.5.5.5", 53),         # 兜底：校园网有时只放 DNS
               ("119.29.29.29", 53))
    last_err = None
    for host, port in targets:
        try:
            s = socket.create_connection((host, port), timeout=3); s.close(); return True
        except Exception as _e:
            last_err = _e
            logger.debug("[网络] 探测 %s:%s 失败: %s" % (host, port, _e)); continue
    logger.debug("[网络] %d 个探测目标均不可达，最后错误=%s" % (len(targets), last_err))
    return False

def ac_power_online():
    """是否接通交流电源（台式机恒为 True）。笔记本靠电池时给日志提示，避免跑到一半休眠/没电。"""
    try:
        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        sps = SPS(); kernel32.GetSystemPowerStatus(ctypes.byref(sps))
        return sps.ACLineStatus == 1, sps.BatteryLifePercent
    except Exception:
        return None, None

def current_wifi_ssid():
    try:
        r = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                           text=True, encoding="gbk", errors="ignore", timeout=8)
        for line in (r.stdout or "").splitlines():
            _s = line.strip().lower()
            if _s.startswith("ssid") and "bssid" not in _s:  # 排除 BSSID 行
                return line.split(":", 1)[1].strip()
    except Exception as _e:
        logger.debug("[WiFi] 读取 SSID 失败: %s" % _e)
    return None

def net_state():
    """联网状态：'ok'(真能上外网) / 'portal'(TCP 通但被校园网门户拦着) / 'off'(完全不通)。

    为什么需要它：net_reachable() 只做 TCP connect，而校园网门户对**未认证**的客户端
    同样放行 TCP 握手，于是同一时刻会既打出「[环境] 联网=是」又打出「[网络] 检测到无外网」
    （2026-09-13 20:55 的 run.log 实测）。日志自相矛盾会直接误导排障。
    这里在 TCP 通的基础上补一次 HTTP：被重定向到门户网关、或返回门户特征页，就算 'portal'。
    """
    if not net_reachable():
        return "off"
    for url in ("http://connectivitycheck.gstatic.com/generate_204", "http://www.baidu.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                final = r.geturl() or ""
                body = r.read(2048).decode("utf-8", "replace")
            if PORTAL_GATEWAY in final or any(m in body for m in PORTAL_MARKERS):
                return "portal"
            return "ok"
        except Exception:
            continue
    # TCP 通、HTTP 全失败：多半是被门户挂起（也可能探测源被墙），按未认证处理
    return "portal"

def log_environment_snapshot():
    """开跑前记录一次环境快照到日志，出问题时能第一时间判断是锁屏/断网/掉电/代理哪一类。全部容错。"""
    try:
        logger.info(f"[环境] 当前时间={datetime.now():%Y-%m-%d %H:%M:%S}  前台='{fg_title()}'")
        _net = {"ok": "是", "portal": "否(门户未认证)", "off": "否"}[net_state()]
        logger.info(f"[环境] 锁屏={'是(无法自动签到)' if is_workstation_locked() else '否'}  WiFi={current_wifi_ssid()}  联网={_net}")
        ac, pct = ac_power_online()
        if ac is not None:
            logger.info(f"[环境] 交流电源={'已接通' if ac else '未接通(用电池!)'} 电量={pct if pct is not None and pct != 255 else '未知'}%")
    except Exception as e:
        logger.warning(f"[环境] 环境快照异常(忽略): {e}")


# ==================== 截图工具 ====================
_step = [0]
def shot(tag):
    """截图存到本次运行目录。

    默认存 JPEG（见 SCREENSHOT_JPEG_QUALITY）以省空间；
    **失败现场**（tag 含 FAIL_ / EXCEPTION）永远存原始 PNG，保证排查时图像不失真。
    任何异常都只打 warning，绝不让截图失败影响签到流程。
    """
    important = ("FAIL_" in tag) or ("EXCEPTION" in tag)
    use_jpeg = (SCREENSHOT_JPEG_QUALITY > 0) and not important
    ext = ".jpg" if use_jpeg else ".png"
    p = os.path.join(RUN_DIR, f"{datetime.now().strftime('%H%M%S')}_{tag}{ext}")
    try:
        if use_jpeg:
            # 先抓到内存再自己编码：pyautogui 只能按扩展名存，控制不了质量参数。
            im = pyautogui.screenshot()
            # cv2 的 imencode 不认中文路径，必须先编到内存再写文件（与项目其它地方一致）。
            bgr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, SCREENSHOT_JPEG_QUALITY])
            if ok:
                with open(p, "wb") as f:
                    f.write(buf.tobytes())
            else:
                # 编码失败就退回存 PNG，不能因为省空间反而丢截图
                p = p[:-4] + ".png"
                im.save(p)
                ext = ".png"
        else:
            pyautogui.screenshot(p)
        logger.info(f"[截图] {tag} -> {os.path.basename(p)}")
    except Exception as e:
        logger.warning(f"[截图] 失败 {tag}: {e}")
    return p

def step_shot(title):
    _step[0] += 1
    return shot(f"step{_step[0]:02d}_{title}")

# ==================== 窗口 ====================
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

def enum_windows(include_hidden=True):
    """枚举顶层窗口，返回[(hwnd,title)]，默认包含隐藏/最小化到托盘的窗口"""
    res = []
    def cb(h, l):
        if include_hidden or user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, b, n + 1)
                if b.value.strip():
                    res.append((h, b.value))
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return res

def find_wins(keyword, exact=False):
    out = []
    for h, t in enum_windows(True):
        if (t == keyword) if exact else (keyword in t):
            out.append((h, t))
    return out

def fg_title():
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, b, n + 1)
    return b.value

def win_rect(h):
    class R(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    rc = R(); user32.GetWindowRect(h, ctypes.byref(rc))
    return rc.l, rc.t, rc.r, rc.b

def _attach_foreground(h):
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    t_tid = user32.GetWindowThreadProcessId(h, None)
    cur = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur, fg_tid, True)
    user32.AttachThreadInput(cur, t_tid, True)
    return fg_tid, t_tid, cur

def _detach(fg_tid, t_tid, cur):
    user32.AttachThreadInput(cur, t_tid, False)
    user32.AttachThreadInput(cur, fg_tid, False)

def activate(keyword, exact=False, prefer_largest=True, logs=True):
    """可靠置顶目标窗口：最小化才Restore；用Alt键技巧解除前台锁并校验重试。返回hwnd或None"""
    wins = find_wins(keyword, exact)
    if not wins:
        if logs:
            logger.error(f"[窗口] 找不到标题'{keyword}'。当前窗口: "
                         + ", ".join(t for _, t in enum_windows(True) if t.strip())[:300])
        return None

    def get_class(h):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        return buf.value

    def area(h):
        l, t, r, b = win_rect(h)
        if l <= -30000 or t <= -30000:
            return -1
        return max(0, r - l) * max(0, b - t)

    def score(item):
        h = item[0]
        l, t, r, b = win_rect(h)
        minimized = 1 if (l <= -30000 or user32.IsIconic(h)) else 0
        qt_bonus = 1 if get_class(h).startswith("Qt") else 0
        return (-minimized, qt_bonus, area(h))

    if prefer_largest:
        wins.sort(key=score, reverse=True)
    hwnd, title = wins[0]
    if logs:
        logger.info(f"[窗口] 候选{len(wins)}个 选中{hwnd} {get_class(hwnd)!r} rect={win_rect(hwnd)}")

    def is_front():
        f = fg_title()
        return (f == title) if exact else (keyword in f)

    try:
        l, t, r, b = win_rect(hwnd)
        if bool(user32.IsIconic(hwnd)) or l <= -30000 or t <= -30000:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # 微信4.x(Qt)最小化窗 ShowWindow 常无效，补 SC_RESTORE 系统命令并校验
            restored = False
            for _ri in range(5):
                user32.PostMessageW(hwnd, 0x0112, 0xF120, 0)  # WM_SYSCOMMAND / SC_RESTORE
                time.sleep(0.8)
                if not user32.IsIconic(hwnd) and user32.IsWindowVisible(hwnd):
                    restored = True; break
                # 兜底：SW_SHOWNORMAL 强制还原（Qt窗口对SW_RESTORE可能无响应）
                user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
                time.sleep(0.4)
            if not restored:
                logger.warning(f"[窗口] 微信最小化恢复失败（IsIconic={user32.IsIconic(hwnd)} Visible={user32.IsWindowVisible(hwnd)}），尝试任务栏点击法")
                # 最后手段：模拟点击任务栏微信图标（坐标不固定，改用键盘Alt+Tab切换）
                try:
                    pyautogui.hotkey("alt", "tab"); time.sleep(0.8)
                    pyautogui.hotkey("alt", "tab"); time.sleep(0.8)
                except Exception:
                    pass
        for k in range(3):
            # Alt 键 trick：一次无害的 Alt 按下/抬起可解除 SetForegroundWindow 的前台锁
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            fg_tid, t_tid, cur = _attach_foreground(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            _detach(fg_tid, t_tid, cur)
            time.sleep(0.6)
            if is_front() and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                break
    except Exception as e:
        logger.warning(f"[窗口] 置顶异常(可忽略): {e}")

    # 最终可见性校验：微信必须真的可见且非最小化，否则搜索框匹配必然失败
    _vis = user32.IsWindowVisible(hwnd)
    _ico = user32.IsIconic(hwnd)
    if not _vis or _ico:
        logger.warning(f"[窗口] 激活后微信仍不可见（Visible={_vis} Iconic={_ico}），本次搜索可能失败")
    if logs:
        logger.info(f"[窗口] 激活后前台='{fg_title()}' 可见={_vis} 最小化={_ico}")
    return hwnd

# ==================== 进程 ====================
def wechat_running():
    """检测微信是否在运行。必须给 timeout：tasklist 偶发卡住会让整个脚本一直挂着，
    直到定时任务 30 分钟上限被强杀。异常一律按"未运行"处理，交给 start_wechat 兜底。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe"],
                           capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=15)
        return "Weixin.exe" in (r.stdout or "")
    except Exception as _e:
        logger.warning("[进程] 检测微信进程失败(按未运行处理，将由 start_wechat 兜底): %s" % _e)
        return False

def resolve_wechat_path():
    """返回可用的微信 exe 路径：优先 config.wechat_path；不存在则在常见安装目录与注册表自动探测，
    便于把整个项目拷到别的电脑（微信装在不同盘/旧版目录）时免改配置。新版=Weixin.exe，旧版=WeChat.exe。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    ld = os.environ.get("LOCALAPPDATA", "")
    cand = [WECHAT_PATH]
    for b in (pf, pf86, ld):
        if not b:
            continue
        cand += [os.path.join(b, "Tencent", "Weixin", "Weixin.exe"),
                 os.path.join(b, "Tencent", "WeChat", "WeChat.exe")]
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\Tencent\Weixin", r"SOFTWARE\WOW6432Node\Tencent\Weixin",
                        r"SOFTWARE\Tencent\WeChat", r"SOFTWARE\WOW6432Node\Tencent\WeChat"):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        ip, _ = winreg.QueryValueEx(k, "InstallPath")
                    if ip:
                        cand += [os.path.join(ip, "Weixin.exe"), os.path.join(ip, "WeChat.exe")]
                except OSError:
                    pass
    except Exception:
        pass
    for p in cand:
        if p and os.path.exists(p):
            return p
    return WECHAT_PATH

def start_wechat():
    wp = resolve_wechat_path()
    if not os.path.exists(wp):
        logger.error(f"[进程] 自动探测仍找不到微信(Weixin.exe/WeChat.exe)。请在 config.json 把 wechat_path 改成该机微信的实际完整路径")
        return False
    logger.info(f"[进程] 微信未运行，启动: {wp}")
    subprocess.Popen(wp)
    deadline = time.time() + CONFIG.get("wechat_start_timeout", 40)
    while time.time() < deadline:
        if find_wins("微信"):
            logger.info("[进程] 微信窗口已出现，等待 6 秒加载")
            time.sleep(6)
            return True
        time.sleep(1)
    logger.error("[进程] 微信启动超时")
    return False

def kill_wechat():
    """结束微信进程（脚本以管理员运行时可结束高权限实例）"""
    for name in ("Weixin.exe", "WeChatAppEx.exe"):
        try:
            # 必须给 timeout：taskkill 偶发卡住会把整个脚本挂到定时任务 30 分钟上限
            subprocess.run(["taskkill", "/F", "/T", "/IM", name],
                           capture_output=True, text=True, timeout=15)
        except Exception as e:
            logger.warning(f"[进程] 结束 {name} 失败(忽略): {e}")
    time.sleep(2)

def restart_wechat_and_enter():
    """白屏无法重绘时的终极自愈：杀掉微信→重启→进主界面→等渲染。返回True/False"""
    logger.warning("[白屏] 重绘无效，重启微信进程")
    try:
        kill_wechat()
    except Exception as e:
        logger.warning(f"[白屏] 结束微信进程异常: {e}")
    if not start_wechat():
        return False
    return step_enter_wechat(allow_restart=False)


# ==================== 图像匹配 ====================
def screen_bgr():
    return cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)

def client_white_ratio(hwnd):
    """返回目标窗口区域内接近纯白像素的占比。微信4.x启动/切换时偶发渲染白屏，占比>0.85即判定白屏。"""
    try:
        l, t, r, b = win_rect(hwnd)
        if l <= -30000 or t <= -30000:
            return 1.0
        full = screen_bgr(); H, W = full.shape[:2]
        x1, x2 = max(0, l), min(W, r); y1, y2 = max(0, t), min(H, b)
        reg = full[y1:y2, x1:x2]
        if reg.size == 0:
            return 1.0
        g = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
        return float((g > 240).mean())
    except Exception:
        return 1.0

def force_repaint(hwnd):
    """最小化再还原，强制窗口重新绘制（对暂时性白屏有效）"""
    try:
        user32.ShowWindow(hwnd, 6); time.sleep(0.8)            # SW_MINIMIZE
        user32.PostMessageW(hwnd, 0x0112, 0xF120, 0); time.sleep(1.8)  # SC_RESTORE
    except Exception as _e:
        logger.debug("[白屏] 最小化/还原重绘失败: %s" % _e)

def wait_wechat_rendered(timeout=18, tag=""):
    """等待微信主界面真正渲染出来（非白屏）。期间白屏则先等、再最小化/还原触发重绘。
    返回渲染好的窗口hwnd；超时仍白屏返回None。"""
    t0 = time.time(); repainted = False
    hwnd = activate("微信", exact=True, logs=False)
    while time.time() - t0 < timeout:
        hwnd = activate("微信", exact=True, logs=False)
        if hwnd:
            wr = client_white_ratio(hwnd)
            if wr < 0.85:
                return hwnd
            logger.warning(f"[白屏] {tag} 窗口白色占比{wr:.2f}，界面尚未渲染，等待...")
        time.sleep(1.2)
        if time.time() - t0 > 4 and not repainted:
            repainted = True
            if hwnd:
                logger.info("[白屏] 尝试最小化→还原强制重绘")
                force_repaint(hwnd)
    hwnd = activate("微信", exact=True, logs=False)
    if hwnd and client_white_ratio(hwnd) < 0.85:
        return hwnd
    logger.error(f"[白屏] {tag} 等待{timeout}s后仍白屏")
    return None


def cv_read(path):
    """Unicode 安全读图：二进制读入后 imdecode，规避 OpenCV 在 Windows 上读中文路径失败的问题。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        logger.warning(f"[读图] 失败 {path}: {e}")
        return None


def match(tpl_path, roi=None, scales=None):
    """返回(置信度,中心x,中心y)。roi=(x1,y1,x2,y2)只在该区域找（坐标换算回全屏）。
    scales=(...)时对模板做多尺度匹配取全局最优，适配微信窗口大小不固定。"""
    if not os.path.exists(tpl_path):
        return -1.0, None, None
    tpl = cv_read(tpl_path)
    if tpl is None:
        return -1.0, None, None
    full = screen_bgr()
    ox = oy = 0
    if roi:
        x1, y1, x2, y2 = roi
        H, W = full.shape[:2]
        x1, y1 = max(0, x1), max(0, y1); x2, y2 = min(W, x2), min(H, y2)
        scr = full[y1:y2, x1:x2]; ox, oy = x1, y1
    else:
        scr = full
    sh, sw = scr.shape[:2]
    if sh <= 0 or sw <= 0:
        return -1.0, None, None
    scale_list = scales if scales else (1.0,)
    best = (-1.0, None, None)
    for sc in scale_list:
        th0, tw0 = tpl.shape[:2]
        nw, nh = max(4, int(round(tw0 * sc))), max(4, int(round(th0 * sc)))
        if nh > sh or nw > sw:
            continue
        t2 = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_AREA) if abs(sc - 1) > 1e-3 else tpl
        r = cv2.matchTemplate(scr, t2, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(r)
        cand = (float(mv), ox + int(ml[0]) + nw // 2, oy + int(ml[1]) + nh // 2)
        if cand[0] > best[0]:
            best = cand
    return best

def match_topmost(tpl_path, roi=None, scales=None, rel_gap=0.06):
    """返回(置信度,中心x,中心y)——在'接近全局最高分'的候选中取【最靠上】的命中。
    用于签到消息列表页：列表按时间倒序，当天进行中的记录永远在最上面。
    与 match 的区别：候选先按置信度过滤（≥ 全局最高分-gap），再取 y 最小，
    避免被屏幕顶部其他弱响应(conf 低但位置靠上)干扰。"""
    if not os.path.exists(tpl_path):
        return -1.0, None, None
    tpl = cv_read(tpl_path)
    if tpl is None:
        return -1.0, None, None
    full = screen_bgr()
    ox = oy = 0
    if roi:
        x1, y1, x2, y2 = roi
        H, W = full.shape[:2]
        x1, y1 = max(0, x1), max(0, y1); x2, y2 = min(W, x2), min(H, y2)
        scr = full[y1:y2, x1:x2]; ox, oy = x1, y1
    else:
        scr = full
    sh, sw = scr.shape[:2]
    if sh <= 0 or sw <= 0:
        return -1.0, None, None
    scale_list = scales if scales else (1.0,)
    mv_global = -1.0
    mbest = None        # 全局最高分的 (r, 左上角, 模板宽, 模板高)，用于无达标候选时退化
    results = []
    for sc in scale_list:
        th0, tw0 = tpl.shape[:2]
        nw, nh = max(4, int(round(tw0 * sc))), max(4, int(round(th0 * sc)))
        if nh > sh or nw > sw:
            continue
        t2 = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_AREA) if abs(sc - 1) > 1e-3 else tpl
        r = cv2.matchTemplate(scr, t2, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(r)
        if mv > mv_global:
            mv_global = mv
            mbest = (r, ml, nw, nh)
        results.append((r, ml, nw, nh))
    thr = max(0.55, mv_global - rel_gap)
    best = None
    for r, ml, nw, nh in results:
        ys, xs = np.where(r >= thr)
        if len(ys) == 0:
            continue
        y_top = int(ys.min())                       # 该尺度下最靠上的命中行
        xs_top = xs[ys == y_top]
        x_mid = int(xs_top.mean())                  # 该行命中点的横向中心
        j = int(np.argmin(np.abs(xs_top - x_mid)))
        conf_at = float(r[y_top, xs_top[j]])
        cand = (conf_at, ox + x_mid + nw // 2, oy + y_top + nh // 2)
        # 优先 y 最小（最靠上）；y 相同取置信度更高
        if best is None or cand[2] < best[2] or (cand[2] == best[2] and cand[0] > best[0]):
            best = cand
    if best is None and mbest is not None:
        r, ml, nw, nh = mbest
        best = (float(mv_global), ox + int(ml[0]) + nw // 2, oy + int(ml[1]) + nh // 2)
    return best if best else (-1.0, None, None)

def daily_signin_top_tpl(tpl_path):
    """「每日签到」模板动态裁剪：原模板把整条记录(含 9/5 日期、已结束状态)框进去了，
    只有 9/5 那条能高置信匹配、当天的记录因日期不同匹配不上。
    这里裁剪出仅含"每日签到"标题的上半部（约前 75% 高度，排除日期/状态行），
    使列表中每条记录的标题区域都能匹配，再配合 topmost 选中最上面(当天)那条。
    返回裁剪后的模板路径（生成一次并缓存到 templates/ 下）。
    注意：cv2.imwrite 不支持中文路径，必须用 imencode + tofile 写入。"""
    crop = os.path.join(os.path.dirname(tpl_path), "23_daily_signin_top.png")
    # 缓存必须跟着原模板走：原模板重新采集过（更新）就重新裁一次，
    # 否则会永远拿旧截图去匹配，重采模板等于白采。
    try:
        if os.path.exists(crop) and os.path.getmtime(crop) >= os.path.getmtime(tpl_path):
            return crop
        if os.path.exists(crop):
            logger.info("[导航] 原模板已更新，重新生成裁剪模板")
    except Exception:
        if os.path.exists(crop):
            return crop
    t = cv_read(tpl_path)
    if t is None:
        return tpl_path
    h, w = t.shape[:2]
    ok, buf = cv2.imencode('.png', t[:int(h * 0.75), :])
    if ok:
        with open(crop, 'wb') as f:
            buf.tofile(f)
        logger.info(f"[导航] 已生成裁剪模板 {os.path.basename(crop)}（去除日期/状态行）")
        return crop
    logger.warning("[导航] 裁剪模板写入失败，回退使用原模板")
    return tpl_path

def wait_click(tpl_path, desc, timeout=12, threshold=None, settle=0.6, roi=None, scales=None):
    """timeout 内反复匹配，达到阈值就点击，返回 (bool, (x,y))"""
    threshold = threshold or CONFIDENCE
    logger.info(f"[匹配] 寻找[{desc}] 阈值{threshold:.2f} 超时{timeout}s 模板={os.path.basename(tpl_path)} "
                f"ROI={roi} 多尺度={bool(scales)}")
    t0 = time.time(); best = -1.0; n = 0; last_log = 0
    while time.time() - t0 < timeout:
        n += 1
        c, x, y = match(tpl_path, roi, scales=scales)
        best = max(best, c)
        if c >= threshold:
            logger.info(f"[匹配] [{desc}] 第{n}次命中 conf={c:.3f} 点击({x},{y})")
            pyautogui.click(x, y); time.sleep(settle)
            return True, (x, y)
        if time.time() - last_log >= 2:
            logger.info(f"[匹配] [{desc}] 等待中 最高conf={best:.3f}(需≥{threshold:.2f}) 前台='{fg_title()}'")
            last_log = time.time()
        time.sleep(0.35)
    logger.error(f"[匹配] [{desc}] 超时，共{n}次，最高conf={best:.3f}")
    if SCREENSHOT_ON_ERROR:
        shot(f"FAIL_{desc}")
    return False, None

# ==================== 辅助 ====================
def paste_text(text):
    pyperclip.copy(text); time.sleep(0.15)
    pyautogui.hotkey("ctrl", "a"); time.sleep(0.1)   # 清空残留
    pyautogui.press("delete"); time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v"); time.sleep(0.2)

# ==================== 主流程各步 ====================
def minimize_wechat_main():
    """小程序打开后，把标题恰为'微信'的主窗最小化，避免遮挡小程序右侧（不影响'油学通'独立窗）"""
    cnt = 0
    for h, t in enum_windows(True):
        if t == "微信":
            user32.ShowWindow(h, 6)  # SW_MINIMIZE
            cnt += 1
    if cnt:
        logger.info(f"[窗口] 已最小化 {cnt} 个微信主窗，避免遮挡小程序")
        time.sleep(0.6)

def close_stray_wechat_windows():
    """关闭微信内部弹出的'搜一搜/微信游戏'等子窗口，避免抢走搜索焦点（保留主窗口和油学通）"""
    bad_keys = ("搜一搜", "微信游戏", "公众号 -", "视频号")
    closed = []
    for h, t in enum_windows(True):
        if t == "微信" or t == MINIAPP_TITLE:
            continue
        if any(k in t for k in bad_keys):
            try:
                user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
                closed.append(t)
            except Exception:
                pass
    if closed:
        logger.info(f"[清理] 关闭残留微信子窗口: {closed}")
        time.sleep(1)

def reset_old_miniprogram():
    """关闭已打开的 油学通 小程序残留窗及其承载进程 WeChatAppEx（不影响微信主窗 Weixin.exe）。
    否则微信会复用旧实例并恢复到上次的详情/子页面，导致找不到工作台的'签到入口'。"""
    found = False
    for h, tt in enum_windows(True):
        if tt == MINIAPP_TITLE:
            try:
                user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
                found = True
            except Exception:
                pass
    # taskkill 既要给 timeout，也要防它自己抛异常：原来直接 subprocess.run 后取
    # r.returncode，一旦 taskkill 起不来（异常）就会 NameError 把整轮打挂。
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/IM", "WeChatAppEx.exe"],
                           capture_output=True, text=True, timeout=15)
        killed = (r.returncode == 0)
    except Exception as e:
        logger.warning(f"[小程序] 清理小程序引擎进程失败(忽略): {e}")
        killed = False
    if found or killed:
        logger.info("[小程序] 已清理旧 油学通/小程序进程，确保从工作台首页冷启动进入")
        time.sleep(1.5)

def close_update_popup(tries=5):
    """关闭'微信4.x 更新说明'弹窗。
    关键：必须先匹配到弹窗标题(08)才认定弹窗存在，再点其右上角 X(06)。
    否则 X 模板会误中微信主窗口自身的关闭钮（两者外观、相对位置几乎相同），反而把微信关成白屏。"""
    for i in range(tries):
        h = activate("微信", exact=True); time.sleep(0.3)
        if not h:
            return True
        l, t, r, b = win_rect(h)
        if l <= -30000:
            time.sleep(0.6); continue
        title_roi = (l, t, r, t + int((b - t) * 0.42))
        ct, _, _ = match(POPUP_TITLE, roi=title_roi, scales=(0.9, 0.95, 1.0, 1.05, 1.1))
        if ct < 0.7:
            logger.info(f"[更新弹窗] 第{i+1}次 无弹窗标题(conf={ct:.3f})，判定无弹窗，绝不点X")
            return True
        x_roi = (r - 170, t, r - 2, t + 95)
        c, x, y = match(POPUP_CLOSE, roi=x_roi, scales=(0.85, 0.95, 1.0, 1.1, 1.2))
        logger.info(f"[更新弹窗] 检测到弹窗标题(conf={ct:.3f})，X conf={c:.3f} 位置=({x},{y})")
        if c >= 0.7:
            activate("微信", exact=True); time.sleep(0.2)
            pyautogui.click(x, y)
            logger.info(f"[更新弹窗] 点击弹窗右上角X({x},{y})")
            time.sleep(1.5)
            h2 = activate("微信", exact=True)
            if h2:
                l2, t2, r2, b2 = win_rect(h2)
                ct2, _, _ = match(POPUP_TITLE, roi=(l2, t2, r2, t2 + int((b2-t2)*0.42)),
                                  scales=(0.9, 1.0, 1.1))
                if ct2 < 0.7:
                    logger.info("[更新弹窗] 标题消失，弹窗已关闭")
                    return True
                logger.info(f"[更新弹窗] 标题仍在(conf={ct2:.3f})，继续关")
        else:
            time.sleep(0.8)
    logger.warning("[更新弹窗] 多次尝试后弹窗可能仍在，继续后续流程")
    return True

# 微信窗口宽度超过这个值，顶部搜索框就会被拉长到模板匹配不上。
# 实测能匹配的宽度：863 / 1118；匹配不上的：2906（手拖大或最大化）。
_WECHAT_MAX_OK_WIDTH = 1400
# 实测能正常匹配的普通尺寸（脚本自己启动微信时就是这个量级）
_WECHAT_NORMAL_SIZE = (1118, 1715)


def _normalize_wechat_size(hwnd, tag=""):
    """把微信窗口恢复成"搜索框模板能匹配"的普通尺寸。返回 True=当前尺寸可用。

    【为什么必须做】微信 4.x 窗口一宽，聊天列表列跟着变宽，**顶部搜索框会被拉成另一个
    长宽比**（模板 345x36 → 2906 宽窗口下约 280x44）。多尺度匹配是**等比缩放**的，
    改不了长宽比，所以 0.5~2.5 倍全部只能到 ~0.59，阈值 0.62 永远够不到 → 搜索必然失败
    （2026-09-14 20:55 定时任务、21:22 / 21:31 手动测试都是这么挂的）。

    【两种"变宽"都要管】这是本函数踩过的坑：
      · 点了最大化按钮 → `IsZoomed()==True` → 先取消最大化；
      · **手动拖大的** → `IsZoomed()==False` 但宽度 2906（2026-09-14 21:31 实测就是这个）
        → 只判断 IsZoomed 会漏掉，必须**按宽度**判断。
    另外微信会记住窗口尺寸，杀进程重启也会还原，所以"重启微信"救不了这个问题。
    """
    if not hwnd:
        return True
    try:
        l, t, r, b = win_rect(hwnd)
    except Exception:
        return True
    w, h = r - l, b - t

    # 1) 如果确实处于"最大化"状态，先取消（顺序按"最接近人工操作"排，每步复核）
    if user32.IsZoomed(hwnd):
        logger.warning(f"[窗口] {tag}检测到微信窗口处于最大化——会改变搜索框长宽比导致匹配失败，"
                       f"正在取消最大化")
        for how, fn in (
            ("SW_RESTORE", lambda: user32.ShowWindow(hwnd, 9)),
            ("SC_RESTORE", lambda: user32.PostMessageW(hwnd, 0x0112, 0xF120, 0)),
            ("Win+↓",      lambda: (activate("微信", exact=True, logs=False),
                                    time.sleep(0.4), pyautogui.hotkey("win", "down"))),
        ):
            try:
                fn()
            except Exception as e:
                logger.warning(f"[窗口] 取消最大化({how})异常: {e}")
            time.sleep(1.2)
            if not user32.IsZoomed(hwnd):
                logger.info(f"[窗口] {tag}已取消最大化（{how}）")
                break
        else:
            logger.warning(f"[窗口] {tag}三种方式都没能取消最大化，改用直接设尺寸")
        try:
            l, t, r, b = win_rect(hwnd); w, h = r - l, b - t
        except Exception:
            return False

    # 2) 宽度仍然超标（手动拖大的，或取消最大化没成功）→ 直接 SetWindowPos 缩回普通尺寸
    if w <= _WECHAT_MAX_OK_WIDTH:
        return True
    nw, nh = _WECHAT_NORMAL_SIZE
    try:
        sw, sh = pyautogui.size()
        nx = max(0, min(l, sw - nw)); ny = max(0, min(t, sh - nh))
    except Exception:
        nx, ny = 0, 0
    logger.warning(f"[窗口] {tag}窗口宽 {w}px 超出可用范围（>{_WECHAT_MAX_OK_WIDTH}）——"
                   f"搜索框会被拉长到模板匹配不上，缩回 {nw}x{nh}")
    try:
        user32.SetWindowPos(hwnd, 0, nx, ny, nw, nh,
                            0x0004 | 0x0010 | 0x0020)   # NOZORDER | NOACTIVATE | FRAMECHANGED
    except Exception as e:
        logger.warning(f"[窗口] SetWindowPos 缩回异常: {e}")
    time.sleep(1.2)
    try:
        l, t, r, b = win_rect(hwnd)
    except Exception:
        return False
    if (r - l) <= _WECHAT_MAX_OK_WIDTH:
        logger.info(f"[窗口] {tag}已缩回 {r-l}x{b-t}")
        return True
    logger.warning(f"[窗口] {tag}缩回失败，当前仍为 {r-l}x{b-t}，搜索可能失败")
    return False


def step_enter_wechat(allow_restart=True):
    """若停在'进入微信'确认页则点进去；等待主界面真正渲染（非白屏）；再关闭'更新说明'弹窗。"""
    hw0 = activate("微信", exact=True)
    # 微信4.x关闭主窗后会隐藏到托盘：窗口句柄还在但 IsWindowVisible=False，或进程在但无主窗。
    # 这种状态 ShowWindow/再次运行唤起都不可靠，9/9 验证：杀进程重启最可靠。
    _hidden = (hw0 and not user32.IsWindowVisible(hw0)) or (not hw0 and wechat_running())
    if _hidden:
        logger.info("[进入微信] 微信隐藏到托盘（不可见或无主窗），杀进程重启（最可靠）")
        kill_wechat()
        if not start_wechat():
            logger.error("[进入微信] 重启微信失败")
            return False
        hw0 = activate("微信", exact=True)
    activate("微信", exact=True)
    c, x, y = match(ENTER_WECHAT_BTN, scales=(0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15))
    logger.info(f"[进入微信] 检测确认页按钮 conf={c:.3f}")
    if c >= 0.8:
        activate("微信", exact=True)
        logger.info(f"[进入微信] 停在确认页，点击({x},{y})进入")
        pyautogui.click(x, y)
        time.sleep(6)  # 进入后窗口句柄重建
        h = wait_wechat_rendered(timeout=14, tag="进入微信后")
        if not h and allow_restart:
            return restart_wechat_and_enter()
        shot("进入微信后")
    else:
        logger.info("[进入微信] 已在主界面，无需确认")
        h = wait_wechat_rendered(timeout=8, tag="主界面")
        if not h and allow_restart:
            return restart_wechat_and_enter()
    # 关闭可能的更新说明弹窗
    close_update_popup()
    h = wait_wechat_rendered(timeout=8, tag="关闭弹窗后")
    if not h and allow_restart:
        return restart_wechat_and_enter()
    h = activate("微信", exact=True)
    # 把窗口恢复成"搜索框模板能匹配"的普通尺寸：窗口一宽，搜索框就被拉成另一个长宽比，
    # 等比多尺度匹配永远够不到阈值（详见 _normalize_wechat_size 注释）。
    # 放在这里是为了让后续整个流程都在普通窗口下跑。
    if h:
        _normalize_wechat_size(h, tag="进入主界面后")
    shot("微信主界面就绪")
    return True

# ===== 清遮挡时的系统窗口排除表（minimize_clutter 与 _minimize_windows_overlapping 共用）=====
_COVER_SKIP_CLASS_PREFIXES = (
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "ToolbarWindow32", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
    "Windows.Internal.SystemTray", "tooltips_class32", "msctfIME",
    "ATL:", "CtrlNotifySink", "ReBarWindow32", "#32768",
    "DUIWindow", "CaretWindow", "MSCTFIME UI", "IME",
    "Windows.Internal.Shell", "TrayInputWnd", "TopLevelWindowForOverflow",
)
_COVER_SKIP_PROCS = {
    "explorer.exe", "shellexperiencehost.exe", "searchui.exe", "searchhost.exe",
    "runtimebroker.exe", "svchost.exe", "dwm.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "smss.exe", "winlogon.exe", "system",
    "registry", "memory compression", "vmmem", "vmmemwsl",
    "securityhealthservice.exe", "securityhealthsystray.exe",
    "widgetservice.exe", "gamebar.exe", "gamebarftserver.exe",
    "textinputhost.exe", "ctfmon.exe", "tabtip.exe", "pen_tip.exe",
    "sihost.exe", "taskhostw.exe", "dllhost.exe", "conhost.exe",
    "openwith.exe", "pickershost.exe", "applicationframehost.exe",
    "systemsettings.exe", "lockapp.exe", "startmenuexperiencehost.exe",
    "shellappruntime.exe", "permissionshost.exe", "printdialog.exe",
}
# 微信主窗自身与小程序窗，清遮挡时绝不能动
_COVER_KEEP_TITLES = {MINIAPP_TITLE, "微信", "油学通", "Program Manager",
                      "任务栏", "运行", "开始"}


def _minimize_windows_overlapping(wx_hwnd, min_area=8000):
    """最小化任何与微信窗口有实质重叠的可见窗口（排除微信自身与系统窗口），返回数量。

    【为什么需要】微信即使被设为 TOPMOST 且在前台，"总在最前"的小悬浮窗
    （典型：QQ音乐桌面歌词）仍可能压在它上面——两个都是 TOPMOST 时 z 序不受
    SetWindowPos 控制。而老的兜底逻辑只最小化"占微信整窗面积 >50%"的窗口，
    这种几百像素宽的小悬浮窗永远够不到；可它盖住搜索框那一小块，
    就足以让搜索框模板匹配失败（2026-09-14 20:55 的定时任务就是这么挂的，
    匹配 conf 只有 0.522，阈值是 0.62）。

    只在"搜索框体检不通过"时调用，所以正常情况下不会去动用户的任何窗口。
    """
    try:
        wxl, wxt, wxr, wxb = win_rect(wx_hwnd)
    except Exception:
        return 0
    if wxr - wxl < 100 or wxb - wxt < 100:
        return 0
    n = 0
    for h, t in enum_windows(True):
        title = t.strip()
        if h == wx_hwnd or title in _COVER_KEEP_TITLES:
            continue
        try:
            cls = _cls(h)
        except Exception:
            continue
        if any(cls.startswith(sc) for sc in _COVER_SKIP_CLASS_PREFIXES):
            continue
        if _proc_name(h) in _COVER_SKIP_PROCS:
            continue
        if user32.IsIconic(h) or not user32.IsWindowVisible(h):
            continue
        try:
            l, tt, r, b = win_rect(h)
        except Exception:
            continue
        if l <= -30000 or (r - l) < 40 or (b - tt) < 20:
            continue
        ox1, oy1 = max(l, wxl), max(tt, wxt)
        ox2, oy2 = min(r, wxr), min(b, wxb)
        ov = max(0, ox2 - ox1) * max(0, oy2 - oy1)
        if ov < min_area:
            continue
        try:
            user32.ShowWindow(h, 6)   # SW_MINIMIZE
            logger.warning(f"[前台] 最小化盖在微信上的窗口 {title!r} cls={cls!r} "
                           f"rect=({l},{tt},{r},{b}) 重叠={ov}px²")
            n += 1
        except Exception:
            pass
    return n


def minimize_clutter():
    """保证微信前台不被遮挡：优先置顶微信（不最小化任何窗口，任务栏无变化）；
    置顶失败时才最小化真正遮挡微信的窗口（兜底）。严格排除系统窗口。"""
    # 先尝试置顶微信——置顶后微信永远在最前面，不需要最小化其他窗口
    wx_hwnd = None
    for h, t in enum_windows(True):
        if t.strip() == "微信":
            wx_hwnd = h; break
    if wx_hwnd:
        try:
            # HWND_TOPMOST = -1, SWP_NOMOVE|SWP_NOSIZE = 0x0001|0x0002
            user32.SetWindowPos(wx_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.3)
            if user32.IsWindowVisible(wx_hwnd) and fg_title() == "微信":
                # 【重要】置顶 ≠ 没被挡。别的"总在最前"的悬浮窗（QQ音乐桌面歌词、
                # 各种通知条）同样能压在微信上面；而脚本是拿**屏幕截图**做匹配的，
                # 被挡住的区域就是匹配不上（2026-09-14 20:55 的定时任务就是这么挂的：
                # 聊天窗口 + QQ音乐歌词盖住搜索框，模板 conf 只有 0.522，阈值 0.62）。
                # 所以这里不再直接 return，照样清一遍真正盖在微信上的窗口。
                n = _minimize_windows_overlapping(wx_hwnd)
                if n:
                    logger.info(f"[前台] 微信已置顶；另清理了 {n} 个盖在微信上的窗口")
                else:
                    logger.info("[前台] 微信已置顶，且没有被遮挡的窗口（任务栏无变化）")
                return n
        except Exception:
            pass
    # 置顶失败（微信不可见/无法置顶），兜底：最小化遮挡微信的窗口
    me = fg_title()
    kept_titles = set(_COVER_KEEP_TITLES) | {me}
    skip_class_prefixes = _COVER_SKIP_CLASS_PREFIXES
    skip_procs = _COVER_SKIP_PROCS
    if not wx_hwnd:
        logger.info("[前台] 未找到微信窗口，跳过遮挡清理")
        return 0
    wxl, wxt, wxr, wxb = win_rect(wx_hwnd)
    wx_area = max(0, wxr - wxl) * max(0, wxb - wxt)
    n = 0; skipped_sys = 0; skipped_noclip = 0
    for h, t in enum_windows(True):
        title = t.strip()
        if not title or title in kept_titles:
            continue
        try:
            cls = _cls(h)
        except Exception:
            continue
        if any(cls.startswith(sc) for sc in skip_class_prefixes):
            skipped_sys += 1; continue
        pname = _proc_name(h)
        if pname in skip_procs:
            skipped_sys += 1; continue
        if user32.IsIconic(h):
            continue
        try:
            l, tt, r, b = win_rect(h)
        except Exception:
            continue
        if l <= -30000 or (r - l) < 150 or (b - tt) < 80:
            continue
        ox1, oy1 = max(l, wxl), max(tt, wxt)
        ox2, oy2 = min(r, wxr), min(b, wxb)
        overlap = max(0, ox2 - ox1) * max(0, oy2 - oy1)
        if wx_area > 0 and overlap < wx_area * 0.50:
            skipped_noclip += 1; continue
        try:
            user32.ShowWindow(h, 6)  # SW_MINIMIZE
            n += 1
        except Exception:
            pass
    if n:
        logger.info(f"[前台] 置顶失败，兜底最小化 {n} 个遮挡微信的窗口（重叠>50%）")
    return n

def _search_box_conf(hwnd):
    """当前微信窗口上部 30% 区域内、搜索框模板的最高匹配置信度；窗口异常时返回 -1。"""
    try:
        l, t, r, b = win_rect(hwnd)
        if l <= -30000 or (r - l) < 200 or (b - t) < 200:
            return -1.0
        roi = (l, t, r, t + int((b - t) * 0.30))
        c, _, _ = match(SEARCH_BOX_TPL, roi, scales=SEARCH_BOX_SCALES)
        return c
    except Exception:
        return -1.0


def _ensure_search_box_matchable():
    """开跑前窗口体检：搜索框模板能不能匹配上；不行就分两步抢救，最后才放弃。

    【为什么需要】2026-09-14 20:55 的定时任务整轮失败，原因不是代码而是**屏幕被挡**：
    QQ音乐的桌面歌词悬浮窗（"总在最前"）压在微信上，正好盖住搜索框，
    模板 conf 只有 0.522（阈值 0.62），后面粘贴、点结果全部落空。
    同一台机器、普通窗口、没有悬浮窗时，同一模板 conf 是 0.78~0.80。

    关键认知：脚本是拿**屏幕截图**做匹配的，看的是"屏幕上这块像素长什么样"，
    不是"窗口内部长什么样"。所以任何盖在微信上的东西都会让它失效。

    抢救顺序（从轻到重，都有实测依据）：
      1) 清遮挡：最小化所有与微信有实质重叠的可见窗口（排除系统窗口）——
         解决"被悬浮歌词/弹窗/别的窗口盖住"。2026-09-14 21:25 实测有效：
         清掉 2 个遮挡窗口后，搜索框 conf 从 0.53x 回到 0.782；
      2) 取消最大化：微信最大化后聊天列表列变宽，搜索框被拉成另一个长宽比，
         等比多尺度匹配永远够不到阈值（实测 0.5~2.5 倍最高只有 0.589）。
         必须主动取消——**重启微信救不了**，因为微信会记住最大化状态；
      3) 重启微信：兜底（窗口尺寸/渲染真的异常时）。

    返回 True=可匹配（或抢救后已恢复）；False=仍不行（多半要重采模板）。
    """
    hwnd = activate("微信", exact=True, logs=False)
    if not hwnd:
        logger.warning("[搜索] 窗口体检：找不到微信窗口")
        return False
    c = _search_box_conf(hwnd)
    if c >= 0.62:
        logger.info(f"[搜索] 窗口体检通过：搜索框可匹配 (conf={c:.3f})")
        return True

    # ---- 第 1 步：清掉盖在微信上的窗口 ----
    logger.warning(f"[搜索] 窗口体检未通过：搜索框 conf={c:.3f}（阈值0.62）。"
                   f"先清理盖在微信上的窗口再试（常见元凶：悬浮歌词、通知、别的窗口）")
    try:
        shot("搜索框体检失败_清遮挡前")
    except Exception:
        pass
    n = _minimize_windows_overlapping(hwnd)
    if n:
        time.sleep(1.2)
        hwnd2 = activate("微信", exact=True, logs=False) or hwnd
        c = _search_box_conf(hwnd2)
        if c >= 0.62:
            logger.info(f"[搜索] 清理 {n} 个遮挡窗口后搜索框可匹配 (conf={c:.3f})，继续搜索")
            return True
        logger.warning(f"[搜索] 清理 {n} 个遮挡窗口后仍只有 conf={c:.3f}")
    else:
        logger.warning("[搜索] 没有找到盖在微信上的窗口")

    # ---- 第 2 步：把窗口尺寸恢复成模板能匹配的普通尺寸 ----
    # （不只看 IsZoomed：手动拖大的窗口 IsZoomed=False 但同样会让搜索框变形，见该函数注释）
    if hwnd:
        if _normalize_wechat_size(hwnd, tag="体检失败后"):
            time.sleep(0.8)
            hwnd2 = activate("微信", exact=True, logs=False) or hwnd
            c = _search_box_conf(hwnd2)
            if c >= 0.62:
                logger.info(f"[搜索] 恢复窗口尺寸后搜索框可匹配 (conf={c:.3f})，继续搜索")
                return True
            logger.warning(f"[搜索] 恢复窗口尺寸后仍只有 conf={c:.3f}")
        hwnd = activate("微信", exact=True, logs=False) or hwnd

    # ---- 第 3 步：重启微信（窗口尺寸/渲染异常时的兜底手段）----
    logger.warning(f"[搜索] 重启微信以恢复窗口状态（当前 conf={c:.3f}）")
    try:
        shot("搜索框体检失败_重启微信前")
    except Exception:
        pass
    try:
        kill_wechat()
    except Exception as e:
        logger.warning(f"[搜索] 结束微信进程异常: {e}")
    if not start_wechat():
        logger.error("[搜索] 重启微信失败，无法恢复窗口状态")
        return False
    if not step_enter_wechat(allow_restart=False):
        logger.error("[搜索] 重启微信后未能进入主界面")
        return False
    hwnd = activate("微信", exact=True, logs=False)
    c = _search_box_conf(hwnd) if hwnd else -1.0
    if c >= 0.62:
        logger.info(f"[搜索] 重启微信后搜索框可匹配 (conf={c:.3f})，继续搜索")
        return True
    logger.warning(f"[搜索] 重启微信后搜索框仍匹配不上（conf={c:.3f}）——"
                   f"模板可能已失效，建议运行 1_采集模板.bat 重采搜索框模板")
    return False


def open_miniprogram_by_search(retries=4):
    """直接点击微信顶部搜索框->粘贴->点'最近使用过的小程序'里的油学通。
    实测微信4.x下 Ctrl+F 打不开搜索、反复 Esc 会白屏，故两者都不用。"""
    minimize_clutter()
    close_stray_wechat_windows()
    reset_old_miniprogram()
    # 窗口体检：搜索框匹配不上（典型是微信被最大化后半透明未渲染）就重启微信恢复，
    # 否则后面 4 次重试全都会在同一个坏窗口上白费功夫（见该函数注释）。
    _ensure_search_box_matchable()
    for attempt in range(1, retries + 1):
        logger.info(f"[搜索打开油学通] 第{attempt}/{retries}次尝试")
        hwnd = activate("微信", exact=True)
        if not hwnd:
            time.sleep(1); continue
        # 搜索前确认主界面已渲染（白屏则等待重绘/重启，不拿白屏去匹配）
        if not wait_wechat_rendered(timeout=10, tag=f"搜索第{attempt}次"):
            logger.warning(f"[搜索] 第{attempt}次前主界面白屏，重启微信后继续")
            if not restart_wechat_and_enter():
                continue
            hwnd = activate("微信", exact=True)
        # 取得有效窗口矩形（句柄切换瞬间可能为 0 或 -32000 最小化坐标，需等其稳定）
        l = t = r = b = 0
        for _ in range(6):
            hwnd = activate("微信", exact=True)
            if hwnd:
                l, t, r, b = win_rect(hwnd)
                if l > -30000 and (r - l) > 200 and (b - t) > 200:
                    break
            time.sleep(0.6)
        # 【关键】绝不能按 Esc——实测微信4.x 按一次 Esc 主界面会整块白屏且无法自恢复。
        # 首次尝试时主界面是干净空搜索框，直接匹配模板置信度最高；
        # 仅重试时上一轮可能残留搜索词/下拉，才用鼠标点搜索框→Ctrl+A/Delete 清回空态。
        if attempt > 1:
            try:
                sx = l + int((r - l) * 0.31); sy = t + int((b - t) * 0.095)
                pyautogui.click(sx, sy); time.sleep(0.35)
                pyautogui.hotkey("ctrl", "a"); time.sleep(0.12)
                pyautogui.press("delete"); time.sleep(0.3)
                activate("微信", exact=True); time.sleep(0.2)
            except Exception:
                pass
        # 1) 模板匹配定位顶部搜索框（不依赖窗口尺寸/阴影），ROI 限定窗口上部 30%
        top_roi = (l, t, r, t + int((b - t) * 0.30))
        ok_sb, _ = wait_click(SEARCH_BOX_TPL, "顶部搜索框", timeout=6, threshold=0.62,
                              settle=1.0, roi=top_roi,
                              scales=SEARCH_BOX_SCALES)
        if not ok_sb:
            logger.warning("[搜索] 未匹配到搜索框模板，仍尝试继续粘贴")
        time.sleep(0.4)
        # 2) 清空残留再粘贴
        pyautogui.hotkey("ctrl", "a"); time.sleep(0.15)
        pyautogui.press("delete"); time.sleep(0.15)
        paste_text(SEARCH_KEYWORD)
        logger.info(f"[搜索] 已输入'{SEARCH_KEYWORD}'，等待结果")
        # 粘贴后立即重新激活微信，防止焦点被其他窗口抢走（任务管理器等弹窗会导致搜索结果截到别的窗口）
        activate("微信", exact=True, logs=False)
        time.sleep(0.3)
        # 3) 搜索下拉只占窗口上部约45%，ROI限定于此避免误中下方聊天头像
        roi = (l, t, r, t + int((b - t) * 0.45))
        opened = False; mx = my = -1
        for _ in range(8):
            time.sleep(0.5)
            # 每次匹配前确认前台是微信，不是则重新激活（防止焦点丢失）
            if fg_title() != "微信":
                activate("微信", exact=True, logs=False)
                time.sleep(0.3)
            best, bx, by = match(MINIAPP_SEARCH_ICON, roi, scales=SEARCH_SCALES)
            if best >= 0.6:
                opened = True; mx, my = bx, by; break
        # 截图前再次确认前台是微信
        if fg_title() != "微信":
            activate("微信", exact=True, logs=False); time.sleep(0.3)
        shot(f"搜索结果_{attempt}")
        if not opened:
            logger.warning(f"[搜索] 结果未出现(最高conf={best:.3f})，立即重试")
            continue
        logger.info(f"[搜索] 结果出现(conf={best:.3f}) 首个匹配=({mx},{my})")
        # 记录点击前已有窗口：小程序会弹出独立新窗口；微信4.x服务号也弹独立窗口、标题也叫"油学通"，
        # 所以不能只看"有没有新窗口"，必须验证窗口内容是不是小程序工作台。
        before_wins = set(h for h, _ in enum_windows(True))
        # 搜索结果里服务号(第1个)和小程序(第2个)纵向间距约110px。每次尝试用不同偏移，
        # 且每次都重新搜索（点击后下拉会关闭，不能在同一下拉里点多个位置）。
        pos_offsets = [0, 110, 0, 110]  # 交替尝试第1个/第2个结果位置
        yoff = pos_offsets[(attempt - 1) % len(pos_offsets)] if attempt <= 4 else 0
        cy = my + yoff
        logger.info(f"[搜索] 点击结果位置 ({mx},{cy}) 偏移={yoff}")
        pyautogui.click(mx, cy); time.sleep(0.5)
        ix = None
        t0 = time.time()
        while time.time() - t0 < 8:
            time.sleep(0.8)
            for h, wt in enum_windows(True):
                if wt == MINIAPP_TITLE and h not in before_wins:
                    ll, tt, rr, bb = win_rect(h)
                    if ll > -30000 and (rr - ll) > 200:
                        ix = h; break
            if ix:
                break
        if not ix:
            logger.warning("[搜索] 点击后未弹出新窗口，本轮重试")
            shot(f"FAIL_点击后无新窗口_{attempt}")
            continue
        logger.info(f"[搜索] 弹出了标题为'油学通'的独立窗口，验证是否为小程序工作台")
        time.sleep(2.5)   # 等工作台首页内容渲染
        minimize_wechat_main()       # 最小化微信主窗，避免遮挡小程序右侧
        time.sleep(0.8)
        ix = activate(MINIAPP_TITLE, exact=True)
        # 白屏检测：小程序冷启动偶发白屏，最多等15秒
        if ix:
            _wr = client_white_ratio(ix)
            if _wr > 0.85:
                logger.info(f"[搜索] 窗口白屏(白色占比{_wr:.2f})，等待加载（最多15秒）")
                _wt0 = time.time()
                while time.time() - _wt0 < 15:
                    time.sleep(1.5)
                    ix = activate(MINIAPP_TITLE, exact=True)
                    if ix and client_white_ratio(ix) <= 0.85:
                        logger.info(f"[搜索] 窗口加载完成（白色占比{client_white_ratio(ix):.2f}）")
                        break
                else:
                    logger.warning("[搜索] 窗口15秒仍白屏，关闭后本轮重试")
                    close_miniprogram()
                    time.sleep(1)
                    continue
        # 内容验证：服务号也叫"油学通"也弹独立窗，但服务号界面没有"日常管理"菜单。
        # 匹配不到则判定为服务号/错误窗口，关闭后重新搜索。
        if ix and NAV_STEPS:
            _verify_tpl = NAV_STEPS[0]["template"]
            if os.path.exists(_verify_tpl):
                _vc, _, _ = match(_verify_tpl, scales=NAV_SCALES)
                if _vc < 0.6:
                    logger.warning(f"[搜索] 窗口没有'日常管理'菜单(conf={_vc:.3f})——点到了服务号，关闭后重新搜索")
                    close_miniprogram()
                    time.sleep(1)
                    continue
                logger.info(f"[搜索] 内容验证通过：检测到'日常管理'菜单(conf={_vc:.3f})，确认为小程序工作台")
        shot("油学通工作台")
        if ix:
            return True
        logger.warning("[搜索] 点击后未发现 油学通 窗口，重试")
    logger.error("[搜索] 多次尝试仍无法打开 油学通")
    return False

def first_card_status_green(hwnd, title_cx, title_cy):
    """检测「每日签到」标题所在卡片（列表第一张=当天记录）状态行的绿色'已签到'文字。
    状态行固定在标题上方约 55~132px、标题右方 380~700px（2026-09-07/08 全屏截图标定，
    与窗口尺寸无关，比固定比例 ROI 更稳）。未签='进行中-未签到'(红)；当天已签=
    '进行中-已签到'(绿)；历史已结束='已结束-已签到'(绿+橙)。返回 (bool, str 描述)。
    【2026-09-12 修复】必须同时检测红色'未签到'：红存在则强制返回 False，
    防止 ROI 偏移到下方已签卡片或绿色 UI 元素造成假阳性。
    【2026-09-15 修复·假成功】ROI 必须**夹在小程序窗口矩形内**！
    当天 20:55 那次误判为成功，实测根因：本次微信窗口只有 1119 宽（以往全屏 2893），
    小程序窗口在 x[1012,1872]，而 ROI 按"标题中心+380~700"算出来是 x[1813,2133]，
    右半边**伸到窗口外**，扫到了桌面上青绿色斜纹壁纸（绿像素 3218px，窗口内只有 80px），
    于是被判成"绿块=已签到"→ 假成功、推绿卡、实际没签到。
    现在 ROI 与窗口矩形求交集；交集太小就直接返回 False（宁可走完整流程去详情页验证，
    也不能靠窗口外的像素判成功）。另外把 ROI 坐标打进日志，下次排查可直接对照。
    纯只读，不点击。"""
    try:
        full = screen_bgr(); Hpx, Wpx = full.shape[:2]
        # 窗口矩形（小程序窗口）；拿不到就退回整屏（保守：不因此判成功）
        rect = win_rect(hwnd) if hwnd else None
        if rect:
            wl, wt, wr, wb = rect
            logger.info(f"[列表已签] 窗口rect=({wl},{wt},{wr},{wb}) 标题中心=({title_cx},{title_cy})")
        else:
            wl, wt, wr, wb = 0, 0, Wpx, Hpx
            logger.warning("[列表已签] 拿不到小程序窗口矩形，ROI 不做窗口约束（本判定将更保守）")
        y1 = max(0, title_cy - 132); y2 = min(Hpx, title_cy - 55)
        x1 = max(0, title_cx + 380); x2 = min(Wpx, title_cx + 700)
        # ★核心修复：把 ROI 夹进窗口内，绝不允许读到窗口外的桌面像素
        cx1, cx2 = max(x1, wl), min(x2, wr)
        cy1, cy2 = max(y1, wt), min(y2, wb)
        clipped = (cx1 != x1 or cx2 != x2 or cy1 != y1 or cy2 != y2)
        if clipped:
            logger.warning(f"[列表已签] ROI 越出小程序窗口，已夹紧：原x[{x1},{x2}]y[{y1},{y2}] "
                           f"-> 夹后x[{cx1},{cx2}]y[{cy1},{cy2}]")
        x1, x2, y1, y2 = cx1, cx2, cy1, cy2
        roi_w, roi_h = x2 - x1, y2 - y1
        logger.info(f"[列表已签] 实际ROI x[{x1},{x2}] y[{y1},{y2}] 尺寸={roi_w}x{roi_h}")
        # 夹紧后太窄/太扁 = ROI 基本落在窗口外，这种位置本来就不可信
        if roi_w <= 40 or roi_h <= 10:
            return False, "ROI被窗口裁剪到过小(%dx%d)，不敢据此判成功" % (roi_w, roi_h)
        sub = full[y1:y2, x1:x2]
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        # 绿色检测（已签到）
        mask_g = cv2.inRange(hsv, (35, 70, 60), (87, 255, 255))
        kern = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 5))
        m_g = cv2.morphologyEx(mask_g, cv2.MORPH_CLOSE, kern)
        total_g = int((m_g > 0).sum())
        cnts, _ = cv2.findContours(m_g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_g = None
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            px = int((m_g[y:y+h, x:x+w] > 0).sum())
            if 14 <= w <= 180 and 10 <= h <= 60 and px >= 40 and (best_g is None or px > best_g[2]):
                best_g = (w, h, px)
        # 红色检测（未签到）——红在HSV中分两段：0-10 和 170-180
        mask_r1 = cv2.inRange(hsv, (0, 70, 60), (10, 255, 255))
        mask_r2 = cv2.inRange(hsv, (170, 70, 60), (180, 255, 255))
        mask_r = cv2.bitwise_or(mask_r1, mask_r2)
        m_r = cv2.morphologyEx(mask_r, cv2.MORPH_CLOSE, kern)
        total_r = int((m_r > 0).sum())
        cnts_r, _ = cv2.findContours(m_r, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_r = None
        for c in cnts_r:
            x, y, w, h = cv2.boundingRect(c)
            px = int((m_r[y:y+h, x:x+w] > 0).sum())
            if 14 <= w <= 180 and 10 <= h <= 60 and px >= 40 and (best_r is None or px > best_r[2]):
                best_r = (w, h, px)
        has_green = total_g >= 120 and best_g is not None
        has_red = total_r >= 80 and best_r is not None
        green_desc = ("绿块%dx%d/%dpx" % best_g) if best_g else ("无绿块/绿像素%d" % total_g)
        red_desc = ("红块%dx%d/%dpx" % best_r) if best_r else ("无红块/红像素%d" % total_r)
        # 红色'未签到'优先：只要检测到红色，强制判定为未签（防止绿色假阳性）
        if has_red:
            return False, f"检测到红色'未签到'({red_desc})，强制未签 | {green_desc}"
        # ★第二道防线（2026-09-15）：绿块必须"像一段状态文字"，才敢判已签到。
        # 状态行是「进行中 · 已签到」这类横排文字，特征：明显宽扁 + **填充极实** + 不像零散斑块。
        # 阈值依据（2026-09-15 实测，不是拍脑袋）：
        #   · 真状态文字「已签到」: 77x28px 宽高比 2.75 填充率 **0.83**（近乎纯实心）
        #   · 桌面壁纸青绿极光    : 77x34px 宽高比 2.26 填充率 **0.34~0.40**（斜纹有大量空隙）
        # 两者在"填充率"上分离得很干净，取 0.55 居中：离壁纸上限 0.40 有 37% 余量，
        # 离真值 0.83 也有充足空间。**不要把这条门槛调低**——它和防线①是互补关系。
        # 另外要求绿块整体落在窗口内（双保险：万一 ROI 夹紧逻辑被改坏，这条还能兜住）。
        if has_green:
            w, h, px = best_g
            aspect = w / float(h) if h else 0
            fill = px / float(w * h) if (w and h) else 0
            # 绿块绝对坐标（ROI 左上角 + 块内偏移），用于校验它真的在窗口里
            try:
                _sub = full[y1:y2, x1:x2]
                _hsv = cv2.cvtColor(_sub, cv2.COLOR_BGR2HSV)
                _m = cv2.inRange(_hsv, (35, 70, 60), (87, 255, 255))
                _m = cv2.morphologyEx(_m, cv2.MORPH_CLOSE, kern)
                _cs, _ = cv2.findContours(_m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                bx = by = None
                for _c in _cs:
                    _x, _y, _w, _h = cv2.boundingRect(_c)
                    if (_w, _h) == (w, h) and int((_m[_y:_y+_h, _x:_x+_w] > 0).sum()) == px:
                        bx, by = x1 + _x, y1 + _y
                        break
                if bx is not None and rect:
                    if bx < wl or bx + w > wr:
                        logger.warning(f"[列表已签] 绿块@x[{bx},{bx+w}] 越出窗口 x[{wl},{wr}]（填充率{fill:.2f}），"
                                       f"不予采信，按未签处理")
                        return False, (f"绿块越出窗口@x[{bx},{bx+w}]，不判成功 | {green_desc}")
            except Exception:
                pass  # 坐标复核失败不阻断，仍由下面的形状门槛把关
            if aspect < 1.8 or fill < 0.55:
                logger.warning(f"[列表已签] 绿块形状不像状态文字（{w}x{h} 宽高比={aspect:.2f} 填充率={fill:.2f} "
                               f"门槛=≥1.8/≥0.55），不予采信，按未签处理走完整流程验证")
                return False, (f"绿块形状可疑({w}x{h} 比={aspect:.2f} 填={fill:.2f})，不判成功 | {green_desc}")
            return True, f"{green_desc}（宽比{aspect:.1f} 填充{fill:.2f} 无红色）"
        return False, f"无绿无红 | {green_desc} | {red_desc}"
    except Exception as e:
        logger.warning(f"[列表已签] 检测异常（按未签处理，不影响主流程）: {e}")
        return False, f"异常:{e}"

def open_signin_entry():
    """按 config.signin_nav_steps 顺序，一路点进多级入口（日常管理 -> 签到消息 -> 每日签到）。
    每一级：先首屏找，找不到就小步向下滚动找；找到点击后停留等待该级页面加载。"""
    if not NAV_STEPS:
        logger.error("[导航] config.json 未配置 signin_nav_steps，无法进入签到页")
        return False
    for i, step in enumerate(NAV_STEPS):
        tpl, name, scroll = step["template"], step["name"], step["scroll"]
        if not os.path.exists(tpl):
            logger.error(f"[导航] 缺少第{i+1}步入口模板「{name}」({tpl})，请先运行 1_采集模板.bat 采集")
            return False
        activate(MINIAPP_TITLE, exact=True); time.sleep(1.2)
        # 「每日签到」入口在签到消息列表页：列表按时间倒序，当天进行中的记录永远在最上面，
        # 必须取最靠上的命中，否则会点到下方旧记录（已结束）导致后续「进入按钮」找不到。
        # 判定方式（防止改配置里的步骤名后这里静默失效）：
        #   1) 该步在 config.signin_nav_steps 里标了 "topmost": true  → 直接生效
        #   2) 否则按名字比对，名字可用 config.signin_list_step_name 覆盖（默认"每日签到"）
        _list_step_name = CONFIG.get("signin_list_step_name", "每日签到")
        topmost = bool(step.get("topmost")) or (name == _list_step_name)
        if topmost:
            # 原 23 模板含 9/5 日期/状态，动态裁剪为仅"每日签到"标题区域，保证当天记录也能匹配
            tpl = daily_signin_top_tpl(tpl)
        # 1) 首屏直接找（多尺度匹配，窗口大小变化也能命中）
        if topmost:
            c, x, y = match_topmost(tpl, scales=NAV_SCALES)
            logger.info(f"[导航] 第{i+1}/{len(NAV_STEPS)}步「{name}」首屏(取最上) conf={c:.3f}")
        else:
            c, _, _ = match(tpl, scales=NAV_SCALES)
            logger.info(f"[导航] 第{i+1}/{len(NAV_STEPS)}步「{name}」首屏 conf={c:.3f}")
        if c >= CONFIDENCE:
            if topmost:
                # 【2026-09-15 架构调整·消灭假成功路径】
                # 这里原本是"列表已签短路"：看到首卡绿色就 return "already" 直接判成功。
                # 9-15 那天它误判了一次（ROI 越出窗口扫到桌面壁纸）→ 假成功、推绿卡、实际没签到。
                #
                # 复盘结论：**颜色判定不足以作为"宣布成功"的依据**，只能作为参考。
                # 理由：屏幕像素会受壁纸、窗口尺寸、主题、缩放、遮挡影响，本质不可靠；
                # 而它唯一的收益只是"今天已签时少走一遍详情页、省 20~30 秒"，
                # 风险与收益完全不对等（省 30 秒 vs 静默漏签）。
                # 历史数据：11 次真实运行中，这个短路只触发过 1 次，且就是那次误判 →
                # **从未带来过收益，却制造了唯一一次假成功。**
                #
                # 现在改为：颜色判定**只写日志、不参与决策**，一律点击进入详情页，
                # 由详情页（能看到签到场次/时间/服务器回执）做权威判定。
                # 这样做的代价是今日已签时多花约 20~30 秒 —— 用这点时间换掉一整类假成功，值得。
                h = activate(MINIAPP_TITLE, exact=True)
                gok, gdesc = first_card_status_green(h, x, y)
                logger.info(f"[导航] 第{i+1}步「{name}」首屏列表状态参考：{gdesc}"
                            f"（仅供参考，不据此判成功，一律进详情页核实）")
                pyautogui.click(x, y)
                logger.info(f"[导航] 第{i+1}步「{name}」命中并点击({x},{y})（列表最上=当天记录）")
                time.sleep(3); shot(f"nav{i+1}_{name}")
                continue
            ok, _ = wait_click(tpl, f"{name}(首屏)", timeout=6, scales=NAV_SCALES)
            if ok:
                time.sleep(3); shot(f"nav{i+1}_{name}")
                continue
        # 2) 小步向下滚动找
        if not scroll:
            logger.error(f"[导航] 第{i+1}步「{name}」未找到且不滚动，失败")
            return False
        found = False
        for k in range(6):
            h = activate(MINIAPP_TITLE, exact=True); time.sleep(0.2)
            if h:
                l, t, r, b = win_rect(h)
                pyautogui.moveTo((l + r) // 2, (t + b) // 2)
            pyautogui.scroll(-180)
            time.sleep(0.9)
            if topmost:
                c, x, y = match_topmost(tpl, scales=NAV_SCALES)
            else:
                c, x, y = match(tpl, scales=NAV_SCALES)
            logger.info(f"[导航] 第{i+1}步「{name}」下滚第{k+1}次 conf={c:.3f}")
            shot(f"找_{name}_{k}")
            if c >= CONFIDENCE:
                if topmost:
                    # 同首屏分支：颜色判定仅作参考日志，不再短路判成功（见上方 2026-09-15 架构调整说明）
                    h = activate(MINIAPP_TITLE, exact=True)
                    gok, gdesc = first_card_status_green(h, x, y)
                    logger.info(f"[导航] 第{i+1}步「{name}」下滚后列表状态参考：{gdesc}"
                                f"（仅供参考，不据此判成功，一律进详情页核实）")
                pyautogui.click(x, y)
                logger.info(f"[导航] 第{i+1}步「{name}」命中并点击({x},{y})")
                time.sleep(3); found = True
                break
        if not found:
            # 先把页面滚回顶部，避免停在底部导致下一轮首屏匹配也失败
            try:
                h = activate(MINIAPP_TITLE, exact=True)
                if h:
                    l, t, r, b = win_rect(h)
                    pyautogui.moveTo((l + r) // 2, (t + b) // 2)
                    for _ in range(8):
                        pyautogui.scroll(300)
                    time.sleep(0.5)
            except Exception:
                pass
            # 签到完成后退出重进：当天记录详情页会直接显示灰色"已签到"、不再有"进入按钮"，
            # 此时应视为已到达签到详情页（确认阶段会识别"已签到"并判成功），而不是导航失败。
            hh = activate(MINIAPP_TITLE, exact=True)
            if hh:
                _btns = scan_buttons(hh)
                if signed_detail_button(hh, _btns):
                    # 时间守卫：签到开始前检测到灰色'已签到'，极可能是昨天的记录
                    if before_signin_start():
                        logger.info(f"[导航] 第{i+1}步「{name}」未找到'进入按钮'，但检测到灰色'已签到'——签到未开始，这是昨天的记录，返回 not_time")
                        shot(f"nav{i+1}_{name}_疑似昨天已签")
                        return "not_time"
                    logger.info(f"[导航] 第{i+1}步「{name}」未找到'进入按钮'，但详情页已是灰色'已签到'（重进刷新成功），视为已到达")
                    shot(f"nav{i+1}_{name}_已签到态")
                    return True
                # "未开始"状态：不在签到时段，详情页只有绿色"请假"按钮，没有蓝色"进入"按钮。
                # 此时不应判失败，应返回 not_time（正常现象，等签到时段再跑）。
                _has_blue = any(b["kind"] == "blue" for b in _btns)
                _has_green = any(b["kind"] == "green" for b in _btns)
                _has_gray = any(b["kind"] == "gray" for b in _btns)
                if _has_green and not _has_blue and not _has_gray:
                    logger.info(f"[导航] 第{i+1}步「{name}」未找到'进入按钮'，详情页只有绿色'请假'按钮——签到未开始（不在时段），返回 not_time")
                    shot(f"nav{i+1}_{name}_未开始")
                    return "not_time"
            logger.error(f"[导航] 第{i+1}步「{name}」滚动后仍未找到，终止")
            return False
    logger.info("[导航] 已按顺序走完所有入口，到达签到详情页")
    return True

def scan_buttons(hwnd, y_frac=0.46):
    """在 油学通 窗口【下半部】扫描宽扁纯色按钮，返回 list[dict(kind,cx,cy,w,h,fill)]。
    kind=blue(蓝色'签到')/green('请假'或地图页'完成签到')/gray('已签到'或定位中/'不在区域内')。
    只看下半部，彻底排除顶部同色蓝色标题栏被误判成按钮。DEBUG 日志记录每个色块落选原因。"""
    l, t, r, b = win_rect(hwnd)
    full = screen_bgr(); Hpx, Wpx = full.shape[:2]  # 彩色图shape为(H,W,3)，只取前两维
    x1, x2 = max(0, l + 6), min(Wpx, r - 6)
    y1, y2 = max(0, t + int((b - t) * y_frac)), min(Hpx, b - 6)
    if y2 - y1 <= 10 or x2 - x1 <= 10:
        logger.debug("[扫描] 窗口裁剪区异常 rect=(%d,%d,%d,%d) 裁剪x[%d,%d]y[%d,%d]，返回空" % (l,t,r,b,x1,x2,y1,y2))
        return []
    sub = full[y1:y2, x1:x2]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    min_w = int((r - l) * 0.26)
    masks = {
        "blue":  cv2.inRange(hsv, (95, 70, 70), (122, 255, 255)),
        "green": cv2.inRange(hsv, (40, 70, 60), (87, 255, 255)),
        # gray 覆盖两种灰按钮：已签到(中灰) 与 定位页"不在区域内/定位中"(浅灰,V≈244)；
        # V 上限 253 用于排除纯白背景(255)，S≤25 保证只收近灰、不收彩色。
        "gray":  cv2.inRange(hsv, (0, 0, 150), (179, 25, 253)),
    }
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 9))
    out = []
    for kind, mask in masks.items():
        m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kern)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rej = []
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (w >= min_w and 24 <= h <= 130 and w >= h * 1.5):
                # 只把“差一点达标(宽度达阈值70%)”的落选块记进调试日志，避免海量小色块刷屏
                if w >= min_w * 0.7:
                    why = []
                    if w < min_w: why.append("宽%d<%d" % (w, min_w))
                    if not (24 <= h <= 130): why.append("高%d越界[24,130]" % h)
                    if w < h * 1.5: why.append("宽高比%.1f<1.5" % (w / max(1, h)))
                    rej.append("%dx%d(%s)" % (w, h, "/".join(why)))
                continue
            # 纯色按钮内部填充率很高(实测>0.98)；窗口圆角外透出的桌面壁纸/文字会被闭运算虚连成大块，
            # 但原始掩码填充率极低(实测0.04)，据此剔除，避免误点窗口底部空白区。
            fill = float((mask[y:y+h, x:x+w] > 0).mean())
            touch_both = (x <= 2) and (x + w >= sub.shape[1] - 2)  # 取宽度 sub.shape[1]
            if fill < 0.6:
                rej.append("%dx%d(填充率%.2f<0.6)" % (w, h, fill)); continue
            if touch_both:
                rej.append("%dx%d(横跨贴左右边)" % (w, h)); continue
            out.append(dict(kind=kind, cx=x1 + x + w // 2, cy=y1 + y + h // 2,
                            w=w, h=h, fill=round(fill, 3)))
        logger.debug("[扫描] %s色: 原始轮廓%d个, 落选=%s" % (kind, len(cnts), rej if rej else "无"))
    desc = ["%s@(%d,%d)%dx%d填充%.2f" % (o["kind"], o["cx"], o["cy"], o["w"], o["h"], o["fill"]) for o in out]
    logger.debug("[扫描] 窗口rect=(%d,%d,%d,%d) 扫描区x[%d,%d]y[%d,%d] min_w=%d 识别=%s"
                 % (l, t, r, b, x1, x2, y1, y2, min_w, desc if desc else "空"))
    return out

def _pick(btns, kind):
    c = [x for x in btns if x["kind"] == kind]
    return max(c, key=lambda z: z["w"] * z["h"]) if c else None

def _tap_return_confirm(hwnd):
    """处理地图页 wx.showModal('签到成功,是否返回?')：先按 Enter，再点弹窗右半'确定'。"""
    try:
        pyautogui.press("enter"); time.sleep(0.4)
    except Exception:
        pass
    l, t, r, b = win_rect(hwnd)
    cx = l + (r - l) * 3 // 4          # '确定'在弹窗右半
    cy = t + int((b - t) * 0.56)
    pyautogui.click(int(cx), int(cy)); time.sleep(0.8)

def tap_relocate(hwnd):
    """点腾讯地图右下角'回到我的位置/重新定位'圆形按钮，触发系统重新定位。
    用于 WiFi/IP 定位偶发漂移（页面显示灰色'不在区域内'）时主动刷新定位。
    位置按小程序窗口相对比例（在真实失败截图上标定：x≈0.91W, y≈0.64H）。"""
    l, t, r, b = win_rect(hwnd)
    cx = l + int((r - l) * 0.91)
    cy = t + int((b - t) * 0.64)
    pyautogui.click(int(cx), int(cy))

def reconnect_wifi():
    """断开并重连当前 WiFi，强制 Windows 重新扫描周边热点、重算 WiFi 定位。
    用于人在校区但 PC 定位漂移到校外（地图页灰色'不在区域内'）的正当排障——
    重连当前已连同名热点、强制系统重扫周边热点后，定位多数会跳回校区辐射范围。
    只重连当前已连接的同名网络，任何异常都吞掉并返回 False，绝不影响主流程。"""
    try:
        enc = "gbk"
        def _netsh(*args):
            p = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=12,
                               encoding=enc, errors="ignore")
            return (p.stdout or "") + (p.stderr or "")
        info = _netsh("show", "interfaces")
        ssid = profile = None
        for line in info.splitlines():
            s = line.split(":", 1)
            if len(s) != 2:
                continue
            key, val = s[0].strip().lower(), s[1].strip()
            if not val:
                continue
            if key in ("ssid",) and ssid is None:
                ssid = val
            if key in ("profile", "配置文件") and profile is None:
                profile = val
        if not profile:
            logger.warning("[WiFi重连] 未能解析当前 WiFi 配置文件，跳过重连")
            return False
        logger.info(f"[WiFi重连] 当前 SSID={ssid} profile={profile}，断开重连以刷新定位...")
        _netsh("disconnect"); time.sleep(3)
        if ssid and ssid != profile:
            _netsh("connect", f"name={profile}", f"ssid={ssid}")
        else:
            _netsh("connect", f"name={profile}")
        linked = False
        for _ in range(12):  # 最多等 24 秒恢复链路关联
            time.sleep(2)
            stat = _netsh("show", "interfaces")
            if ("已连接" in stat) or ("connected" in stat.lower()):
                linked = True; break
        if not linked:
            logger.warning("[WiFi重连] 重连后未在限定时间内恢复链路连接")
            return False
        # 链路关联不等于能上网（DHCP/网关就绪有数秒延迟），再等 TCP 真正可达
        for _ in range(8):
            if net_reachable():
                logger.info("[WiFi重连] 链路已连且公网可达，等待定位收敛")
                time.sleep(3); return True
            time.sleep(2)
        logger.warning("[WiFi重连] 已关联热点但暂未探测到公网连通（仍继续尝试定位）")
        return True
    except Exception as e:
        logger.warning(f"[WiFi重连] 异常（忽略，不影响主流程）: {e}")
        return False

def tap_detail_refresh(hwnd):
    """点详情页'基本信息'行右侧的圆形刷新按钮(↻)，重新向服务器拉取签到状态。
    位置在真实已签/未签详情页截图上标定：x≈0.92W, y≈0.374H。"""
    l, t, r, b = win_rect(hwnd)
    cx = l + int((r - l) * 0.92)
    cy = t + int((b - t) * 0.374)
    pyautogui.click(int(cx), int(cy))

def signed_detail_button(hwnd, btns):
    """判断当前是否为'已签到'详情页（签到成功的硬依据）：
    无蓝色'签到'、无绿色'请假'，且在详情页按钮行存在一个足够宽的灰色按钮。
    已签灰按钮位于窗口约 0.64~0.80 高度；地图页'不在区域内'灰按钮在约 0.83 高度，据此区分。
    返回该灰色按钮 dict；不是已签详情页则返回 None。"""
    kinds = [x["kind"] for x in btns]
    if "blue" in kinds or "green" in kinds:   # 详情页未签是 蓝签到+绿请假；已签两者都消失
        return None
    l, t, r, b = win_rect(hwnd); H = max(1, b - t)
    for x in btns:
        if x["kind"] != "gray":
            continue
        yf = (x["cy"] - t) / H
        if 0.64 <= yf <= 0.80 and x["w"] >= (r - l) * 0.6:
            return x
    return None

def reopen_miniprogram_to_refresh():
    """关闭当前油学通小程序→重新搜索打开→重新导航到签到详情页。
    用于签到请求提交后，小程序页面缓存不刷新、必须退出重进才能看到'已签到'的情况。
    返回 True/False。"""
    logger.info("[刷新] 关闭油学通小程序，准备重新进入以刷新签到状态")
    for h, t in enum_windows(True):
        if t == MINIAPP_TITLE:
            try:
                user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
    time.sleep(2)
    if not open_miniprogram_by_search(retries=3):
        logger.warning("[刷新] 重新打开小程序失败")
        return False
    _re = open_signin_entry()
    if _re is False:
        logger.warning("[刷新] 重新导航到详情页失败")
        return False
    if _re == "not_time":
        logger.info("[刷新] 重进后签到未开始（不在时段）")
        return "not_time"
    if _re == "already":
        logger.info("[刷新] 重进后列表第一张卡片已显示绿色'已签到'，刷新成功")
        return "already"
    logger.info("[刷新] 已重新进入油学通并到达签到详情页")
    return True

def click_sign_button():
    """完整两页流程，返回 success/not_time/fail。
    详情页: 绿色'请假'+蓝色'签到'(并排) -> 点蓝色签到进入地图定位页；
    地图页: 底部'完成签到'定位成功后由灰变绿 -> 点绿色完成签到 -> 处理'是否返回'弹窗。"""
    h = activate(MINIAPP_TITLE, exact=True); time.sleep(1)
    TRACE.begin_step("S5", "详情页")
    if not h:
        TRACE.end_step("fail", "DETAIL_NO_WINDOW")
        return "fail"

    # ---------- 阶段A：详情页（等待蓝色签到按钮，最多12秒，自愈信号触发时延长到20秒） ----------
    DETAIL_WAIT = 20 if self_heal.consume_signal("wait_longer_detail") else 12
    blue = green = gray = None
    t0 = time.time()
    while time.time() - t0 < DETAIL_WAIT:
        h = activate(MINIAPP_TITLE, exact=True)
        btns = scan_buttons(h) if h else []
        blue, green, gray = _pick(btns, "blue"), _pick(btns, "green"), _pick(btns, "gray")
        logger.info(f"[详情页] 按钮={[(x['kind'], x['cx'], x['cy']) for x in btns]}")
        if blue or green:
            break
        time.sleep(1.2)
    shot("详情页按钮扫描")
    if not blue:
        if signed_detail_button(h, btns):
            # 时间守卫：签到开始前(20:50前)检测到灰色'已签到'，极可能是昨天的记录，不能判今天成功
            if before_signin_start():
                logger.warning("[详情页] 签到未开始但检测到灰色'已签到'——这是昨天的记录，不是今天，返回 not_time")
                shot("详情页_疑似昨天已签")
                TRACE.end_step("skipped", "YESTERDAY_RECORD")
                return "not_time"
            # 【2026-09-15 新增·第二道时间守卫】
            # 已知限制：灰色「已签到」和灰色「已结束」在像素上无法区分（都是同款灰宽按钮）。
            # 若页面停在【昨天那条"已结束"】记录上（小程序缓存没刷新），会被误判成"今天已签到"→ 假成功。
            # 现在唯一的物理约束是时间：签到只可能在 [signin_time_start, signin_time_end] 内进行，
            # 时段之外的"已签到"记录**不可能是今天签的**（今天的记录在时段外还没有"已签到"状态）。
            # 因此：超出时段仍显示"已签到"→ 一律视为历史记录，判 not_time，绝不判成功。
            if not _within_signin_window():
                logger.warning(f"[详情页] 检测到灰色'已签到'，但当前不在签到时段"
                               f"[{SIGNIN_TIME_START}~{SIGNIN_TIME_END}]内——该记录必然不是今天的，"
                               f"判 not_time（防止把昨天'已结束'记录误判成今天已签）")
                shot("详情页_时段外已签_疑历史记录")
                TRACE.end_step("skipped", "OUT_OF_WINDOW_SIGNED")
                return "not_time"
            logger.info("[详情页] 当前已是灰色'已签到'（今日已签/上一轮已签成功），直接判定成功")
            shot("详情页_已是已签到")
            TRACE.end_step("short_circuit", "DETAIL_ALREADY_SIGNED")
            return "success"
        if not green and gray:
            logger.warning("[详情页] 只有灰色宽按钮、无蓝/绿且非'已签到'形态——未到签到时段/已结束，不点击不关机")
            TRACE.end_step("skipped", "NOT_TIME")
            return "not_time"
        if not green:
            logger.error(f"[详情页] {DETAIL_WAIT}秒内未出现任何签到按钮，页面可能异常")
            TRACE.end_step("fail", "DETAIL_NO_BUTTON")
            return "fail"
        logger.info("[详情页] 未见蓝色签到但有绿色按钮，判断已在地图定位页，直接进入阶段B")
    else:
        logger.info(f"[详情页] 点击蓝色'签到'({blue['cx']},{blue['cy']})，进入地图定位页")
        pyautogui.click(blue["cx"], blue["cy"]); time.sleep(3.2)

    TRACE.end_step("success")
    TRACE.begin_step("S6", "地图定位并提交")

    # ========== 阶段B-1：地图定位页，等绿色'完成签到'并点击（只点一次） ==========
    # 状态机：
    #  · 详情页 = 蓝色'签到'+绿色'请假'并排；蓝色在=还在详情页，此刻绿色是'请假'绝不能点。
    #  · 地图页 = 蓝色消失，底部'完成签到'定位后由灰变绿，这个绿色才点，且【只点一次】。
    #  · 定位漂移（灰色'不在区域内'）时：点'重新定位'，仍不行就重连一次 WiFi。
    #  · 点到绿色只代表'请求已发起'，不代表成功——必须进入 B-2 刷出灰色'已签到'才算。
    t0 = time.time(); finish_clicks = 0; last_g = -99
    entered_map = False; blue_clicks = 0
    last_relocate = -99; relocate_clicks = 0; wifi_refreshed = False
    # 自愈信号：上次定位超时则本次延长到120秒（消费一次即失效）
    LOCATE_WAIT = 120 if self_heal.consume_signal("extend_locate_wait") else 75
    submitted = False
    while time.time() - t0 < LOCATE_WAIT:
        h = activate(MINIAPP_TITLE, exact=True)
        if not h:
            time.sleep(1); continue
        btns = scan_buttons(h)
        b2, g2, gr2 = _pick(btns, "blue"), _pick(btns, "green"), _pick(btns, "gray")
        logger.info(f"[定位页] {time.time()-t0:4.1f}s entered_map={entered_map} 已点完成={finish_clicks} "
                    f"按钮={[(x['kind'], x['cx'], x['cy']) for x in btns]}")
        if not btns:
            _wr = client_white_ratio(h)
            logger.info(f"[定位页] 本轮未扫到按钮，窗口白色占比={_wr:.2f}"
                        + ("，>0.85 疑似白屏/页面未渲染" if _wr > 0.85 else "，非白屏：多为定位中且按钮颜色未达阈值，详见[扫描]调试行"))

        # 1) 尚未提交：蓝色签到还在 = 仍停在详情页（旁边绿色是请假），补点蓝色进入地图页
        if finish_clicks == 0 and b2 and not entered_map:
            blue_clicks += 1
            if blue_clicks > 5:
                logger.error("[定位页] 多次点击蓝色签到仍未进入地图页，判定失败")
                shot("FAIL_进不了地图页")
                TRACE.signal("blue_clicks", blue_clicks)
                TRACE.end_step("fail", "MAP_CANNOT_ENTER")
                return "fail"
            logger.info(f"[定位页] 仍在详情页，补点蓝色签到({b2['cx']},{b2['cy']}) 第{blue_clicks}次")
            pyautogui.click(b2["cx"], b2["cy"]); time.sleep(3.0); continue
        if btns and not b2:
            # 只有"确实扫到按钮、且其中没有蓝色"才算已进地图页。
            # 原写法是 `if not b2`：扫描整页失败(btns 为空)时 b2 同样是 None，会被误判成
            # "蓝色已消失=进了地图页"；而那一刻若人还停在详情页，旁边的绿色"请假"就会被
            # 当成地图页的"完成签到"点下去，直接走进请假流程。
            entered_map = True

        # 2) 尚未提交 + 已进地图页 + 绿色'完成签到'出现：点它，只点一次，然后进入硬确认
        if finish_clicks == 0 and entered_map and g2 and time.time() - last_g > 2.5:
            logger.info(f"[定位页] 定位完成，点击绿色'完成签到'({g2['cx']},{g2['cy']})")
            pyautogui.click(g2["cx"], g2["cy"]); finish_clicks = 1; submitted = True; last_g = time.time()
            mark("地图页定位在校内，已点绿色'完成签到'")
            time.sleep(1.6)
            _tap_return_confirm(h)
            shot("完成签到后")
            break
        if finish_clicks == 0 and (not entered_map) and g2:
            logger.info("[定位页] 蓝色签到尚未消失，该绿色是'请假'，忽略不点")
        if finish_clicks == 0 and gr2 and entered_map:
            logger.info("[定位页] 底部按钮仍灰色（定位中/'不在区域内'），等待定位...")
        # 2.5) 进了地图页却迟迟没有绿色完成签到（WiFi/IP 定位漂移或偏慢）：
        #      每 6 秒点一次地图右下角'重新定位'主动重算；连续 3 次仍无绿色（人在校内却漂到校外），
        #      自动重连一次当前 WiFi 强制系统重新扫描热点、刷新定位（2026-09-01 实测有效），只重连一次。
        if finish_clicks == 0 and entered_map and not g2 and time.time() - last_relocate > 6:
            relocate_clicks += 1
            if relocate_clicks >= 3 and not wifi_refreshed:
                logger.warning("[定位页] 多次重新定位仍无绿色完成签到，疑似定位漂移，重连 WiFi 刷新定位")
                shot("定位漂移_重连WiFi前")
                reconnect_wifi(); wifi_refreshed = True
                relocate_clicks = 0; last_relocate = -99   # 重连后重新给重新定位机会
                tap_relocate(h); time.sleep(1.5); continue
            if relocate_clicks <= 6:
                logger.info(f"[定位页] 未出现绿色完成签到，点地图'重新定位'刷新 第{relocate_clicks}次")
                tap_relocate(h); last_relocate = time.time(); time.sleep(1.2)
        time.sleep(1.5)

    if not submitted:
        logger.error(f"[结果] {LOCATE_WAIT}秒内未出现可点的绿色'完成签到'：定位未进入校区围栏（页面灰色'不在区域内'）或定位失败。"
                     "请确认人在校内、关闭代理/VPN/加速器、尽量连校区网络后重试；仍不行请先用手机签到。")
        shot("FAIL_定位页超时")
        TRACE.signal("relocate_clicks", relocate_clicks)
        TRACE.signal("wifi_reconnected", wifi_refreshed)
        TRACE.end_step("fail", "MAP_LOCATE_TIMEOUT")
        return "fail"

    TRACE.signal("finish_clicks", finish_clicks)
    TRACE.signal("relocate_clicks", relocate_clicks)
    TRACE.signal("wifi_reconnected", wifi_refreshed)
    TRACE.end_step("success")
    TRACE.begin_step("S7", "提交后硬确认")

    # ========== 阶段B-2：硬确认——刷新详情页直到出现灰色'已签到'，否则判失败 ==========
    # 点到绿色只代表请求发出，服务器可能没登记；必须重新拉取状态、亲眼见到'已签到'才算成功。
    logger.info("[确认] 完成签到请求已点，开始刷新详情页核对服务器状态——必须连续见到灰色'已签到'才判成功")
    time.sleep(2.2)
    tv = time.time(); confirm_rounds = 0; last_refresh = -99; refresh_clicks = 0
    last_back = -99; seen_detail = False; reentered = False
    CONFIRM_WAIT = 50
    while time.time() - tv < CONFIRM_WAIT:
        h = activate(MINIAPP_TITLE, exact=True)
        if not h:
            time.sleep(1); continue
        btns = scan_buttons(h)
        has_blue = _pick(btns, "blue") is not None
        sb = signed_detail_button(h, btns)
        if has_blue or sb:
            seen_detail = True     # 已回到详情页（未签旧缓存=有蓝；已签=有灰已签到）
        logger.info(f"[确认] {time.time()-tv:4.1f}s 按钮={[(x['kind'], x['cx'], x['cy']) for x in btns]} "
                    f"已签到依据={'有' if sb else '无'}")
        if not btns:
            _wr = client_white_ratio(h)
            logger.info(f"[确认] 本轮未扫到按钮，窗口白色占比={_wr:.2f}"
                        + ("，>0.85 疑似白屏/未回到详情页" if _wr > 0.85 else "，非白屏，详见[扫描]调试行"))
        if sb:
            confirm_rounds += 1
            if confirm_rounds >= 2:
                evi = shot("签到成功_已签到")
                mark("详情页连续2次显示灰色'已签到'，证据截图=%s" % os.path.basename(evi))
                logger.info("[结果] 详情页连续显示灰色'已签到'（有界面依据），判定签到成功")
                TRACE.signal("evidence", "detail_gray")
                TRACE.end_step("success")
                return "success"
        else:
            confirm_rounds = 0
            if not seen_detail and time.time() - last_back > 5:
                # 还停在地图页/返回弹窗：再处理一次'是否返回'，回到详情页才好刷新核对
                logger.info("[确认] 尚未回到详情页，再处理一次'是否返回'弹窗")
                _tap_return_confirm(h); last_back = time.time()
            elif seen_detail and time.time() - last_refresh > 8:
                # 旧缓存（蓝签到+绿请假）说明服务器新状态还没显示，周期性点详情页刷新按钮重新拉取
                refresh_clicks += 1
                # 刷新3次仍不见'已签到'：小程序页面缓存不刷新，必须退出重进才能拉到最新状态
                if refresh_clicks >= 3 and not reentered:
                    logger.info("[确认] 连续3次刷新未见到'已签到'，退出小程序重新进入以刷新服务器状态")
                    reentered = True
                    _re = reopen_miniprogram_to_refresh()
                    if _re == "already":
                        evi = shot("签到成功_重进列表已签")
                        mark("重进后列表第一张卡片绿色'已签到'，证据截图=%s" % os.path.basename(evi))
                        logger.info("[结果] 重进后列表已显示绿色'已签到'（服务器状态），判定签到成功")
                        TRACE.signal("evidence", "reopen_list_green")
                        TRACE.end_step("success")
                        return "success"
                    if _re == "not_time":
                        logger.info("[确认] 重进后签到未开始（不在时段），返回 not_time")
                        TRACE.end_step("not_time", "REOPEN_NOT_TIME")
                        return "not_time"
                    if _re:
                        confirm_rounds = 0; seen_detail = False; last_refresh = time.time()
                        tv = time.time()   # 重进后给足确认时间
                        shot("重进后详情页")
                        continue
                    else:
                        logger.warning("[确认] 退出重进失败，继续点刷新按钮")
                logger.info(f"[确认] 尚未见到'已签到'，点详情页刷新按钮重新加载 第{refresh_clicks}次")
                _wr = client_white_ratio(h)
                if _wr > 0.85:
                    logger.info(f"[确认] 窗口白色占比{_wr:.2f} 疑似白屏，本轮不点刷新，等待自然恢复（教训：白屏时点刷新只会更白）")
                    time.sleep(1.6); continue
                tap_detail_refresh(h); last_refresh = time.time()
                shot(f"确认中_刷新{refresh_clicks}")
        time.sleep(1.6)

    logger.error(f"[结果] 已点完成签到，但{CONFIRM_WAIT}秒内多次刷新始终未出现灰色'已签到'，"
                 "无法证实签到成功——判失败、不关机并保留现场，请人工核查或用手机补签。")
    shot("FAIL_未确认签到成功")
    TRACE.signal("detail_refresh_clicks", refresh_clicks)
    TRACE.end_step("fail", "CONFIRM_TIMEOUT")
    return "fail"



def close_miniprogram():
    """签到完成后关闭油学通小程序独立窗口（保留微信主窗，不影响用户继续用微信）。"""
    closed = False
    for h, t in enum_windows(True):
        if t == MINIAPP_TITLE:
            try:
                user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
                closed = True
            except Exception:
                pass
    if closed:
        logger.info("[小程序] 签到完成，已关闭油学通小程序窗口（微信主窗保留）")
        time.sleep(1)

def shutdown_pc():
    if not SHUTDOWN_AFTER_SUCCESS:
        logger.info("[关机] 配置为不关机，跳过")
        return
    logger.info(f"[关机] 签到成功，{SHUTDOWN_DELAY} 秒后关机；如需取消执行 shutdown /a")
    try:
        # 给 timeout：shutdown 正常立即返回，但万一卡住不能让它拖着脚本不放
        subprocess.run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY), "/c", "油学通签到完成，即将关机"],
                       capture_output=True, timeout=15)
    except Exception as e:
        logger.warning(f"[关机] 调用 shutdown 失败(忽略，请手动关机): {e}")

# ==================== 主流程 ====================
def _cls(h):
    b = ctypes.create_unicode_buffer(256); user32.GetClassNameW(h, b, 256); return b.value

def _ttl(h):
    b = ctypes.create_unicode_buffer(256); user32.GetWindowTextW(h, b, 256); return b.value

def _wpid(h):
    pid = wintypes.DWORD(); user32.GetWindowThreadProcessId(h, ctypes.byref(pid)); return pid.value

def _proc_name(h):
    """取窗口所属进程名（小写），失败返回空串。用于排除系统进程窗口。"""
    try:
        pid = _wpid(h)
        if not pid:
            return ""
        hproc = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not hproc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            sz = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass
    return ""

def clear_foreground_blockers(rounds=4):
    """清除劫持全局前台的'屏幕外幽灵模态框'（如联想电脑管家 lxwp 弹窗）。
    现象：某高权限进程把无标题模态窗移到屏幕外(如-500,-500)并占据前台，导致合成点击全部落空。
    先 PostMessage WM_CLOSE；若脚本以管理员运行则 taskkill 其进程。"""
    for _ in range(rounds):
        fg = user32.GetForegroundWindow()
        if not fg:
            return
        cls, ttl, rect = _cls(fg), _ttl(fg), win_rect(fg)
        l, t, r, b = rect
        minimized = l <= -32000 or t <= -32000
        offscreen = (not minimized) and (l < -300 or t < -300)
        ghost = ("Shadow" in cls) or cls.startswith("ATL:") or (offscreen and not ttl.strip())
        if not ghost:
            return  # 前台正常
        pid = _wpid(fg)
        logger.warning(f"[清障] 发现劫持前台幽灵窗 hwnd={fg} class={cls!r} rect={rect} pid={pid}，尝试关闭")
        user32.PostMessageW(fg, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.6)
        if user32.IsWindow(fg) and user32.GetForegroundWindow() == fg:
            try:
                p_ = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True, text=True, timeout=8)
                logger.info(f"[清障] taskkill pid={pid} rc={p_.returncode} {p_.stdout.strip()} {p_.stderr.strip()}")
            except Exception as e:
                logger.warning(f"[清障] taskkill 失败: {e}")
            time.sleep(1.0)
        # 不按 Esc（微信4.x 收到 Esc 会整块白屏）；焦点在窗口关闭后自然转移，下一步会主动激活微信

def attempt_once(rnd, total_rounds):
    """完整走一遍：进微信主界面 -> 搜索打开 油学通 -> 多级入口进入签到详情页 -> 签到并硬确认。
    返回 success/not_time/fail。open_miniprogram_by_search 会先清理旧小程序再冷启动，保证每轮全新进入。"""
    logger.info("#" * 58)
    logger.info(f"# 第 {rnd}/{total_rounds} 轮完整签到尝试（冷启动重新走一遍）")
    logger.info("#" * 58)
    mark(f"第{rnd}/{total_rounds}轮开始")
    clear_foreground_blockers()                       # 每轮开头再清一次可能新弹出的幽灵窗
    TRACE.begin_round(rnd)
    # ---- S0 前置与环境 ----
    TRACE.begin_step("S0", "前置与环境")
    if is_workstation_locked():                       # 跑到一半被锁屏则本轮无意义
        logger.error(f"[前置] 第{rnd}轮开始前屏幕处于锁定状态，终止本轮")
        TRACE.end_step("fail", "PRE_LOCKED")
        TRACE.end_round("fail")
        return "fail"
    TRACE.end_step("success")
    # ---- S1 进入微信主界面 ----
    TRACE.begin_step("S1", "进入微信主界面")
    _s1 = step_enter_wechat()
    if not _s1:
        logger.error(f"[流程] 第{rnd}轮进入微信主界面失败，终止本轮")
        TRACE.end_step("fail", "WX_ENTER_TIMEOUT")
        TRACE.end_round("fail")
        return "fail"
    TRACE.end_step("success")
    step_shot(f"{rnd}_1_微信主界面")
    # ---- S2 搜索打开油学通 ----
    TRACE.begin_step("S2", "搜索打开油学通")
    if not open_miniprogram_by_search():
        TRACE.end_step("fail", "SEARCH_OPEN_FAIL")
        TRACE.end_round("fail")
        return "fail"
    TRACE.end_step("success")
    step_shot(f"{rnd}_2_油学通已打开")
    # ---- S3 进入签到消息列表 ----
    TRACE.begin_step("S3", "进入签到消息列表")
    _ent = open_signin_entry()
    # 【2026-09-15】原先这里有个 `if _ent == "already": return "success"` 分支——
    # 那是"列表看到绿色就判成功"的短路出口，9-15 因扫到桌面壁纸而误判过一次。
    # 该分支已随架构调整一并移除：open_signin_entry() 不再返回 "already"，
    # 签到结果一律由详情页核对给出，不再有任何"凭颜色直接判成功"的路径。
    if _ent == "not_time":
        logger.info("[流程] 签到未开始（不在时段），返回 not_time")
        TRACE.end_step("not_time", "SIGNIN_NOT_STARTED")
        TRACE.end_round("not_time")
        return "not_time"
    if not _ent:
        TRACE.end_step("fail", "NAV_ENTRY_FAIL")
        TRACE.end_round("fail")
        return "fail"
    TRACE.end_step("success")
    step_shot(f"{rnd}_3_到达签到详情页")
    # ---- S5/S6/S7 在 click_sign_button 内部按阶段边界埋点 ----
    _res = click_sign_button()
    TRACE.end_round(_res if _res in ("success", "not_time") else "fail")
    return _res

# 一次运行只推一条飞书：异常路径可能重复调用，用这个标记兜住
_FEISHU_DONE = [False]

def _within_signin_window():
    """当前是否还在签到时间窗内（用于决定要不要跑第三轮补救）。

    时间配置写坏/缺失时保守返回 True——宁可多跑一轮，也不要因为配置读不出来就放弃补救。
    """
    try:
        hh1, mm1 = [int(x) for x in str(SIGNIN_TIME_START).split(":")]
        hh2, mm2 = [int(x) for x in str(SIGNIN_TIME_END).split(":")]
        now = datetime.now().hour * 60 + datetime.now().minute
        return hh1 * 60 + mm1 <= now <= hh2 * 60 + mm2
    except Exception:
        return True


def notify_start():
    """开跑时推一条"开始签到"（第 4 项：让"它到底跑没跑"不再靠猜）。

    与 notify_feishu 分开：这条是"开始"、那条是"结果"，各自独立发。
    not_time 等提前退出情况不会有结果，最坏就是多一条开始通知，无副作用。
    """
    if feishu_notify is None:
        return
    try:
        feishu_notify.send(
            "油学通签到：开始执行 · %s" % datetime.now().strftime("%m-%d %H:%M"),
            ["🟦 已启动，正在自动打开微信并搜索「%s」" % SEARCH_KEYWORD,
             "若无后续结果通知，说明流程卡住（全局超时 %d 秒）" % GLOBAL_TIMEOUT],
            "info")
        logger.info("[通知] 已推送开始提醒")
    except Exception as _se:
        logger.warning(f"[通知] 开始提醒发送异常（忽略）: {_se}")


def notify_feishu(reason=None):
    """把本次签到结果推飞书（成功 / 失败 / 不在时段 / 流程提前中断都会推）。

    幂等：一次运行只发第一条，避免异常路径把同一次运行推两遍。
    任何异常都吞掉——通知绝不能反过来影响签到。

    reason 非空表示流程还没走到收尾（锁屏、微信起不来、脚本异常）就退出了，
    此时 result.txt 还没写，走 notify_early_exit 单独组稿。
    """
    if _FEISHU_DONE[0]:
        return
    _FEISHU_DONE[0] = True
    if feishu_notify is None:
        logger.warning("[通知] feishu_notify 未导入，跳过飞书通知")
        return
    try:
        if reason:
            feishu_notify.notify_early_exit(reason, run_dir=RUN_DIR)
        else:
            feishu_notify.notify_signin_result(run_dir=RUN_DIR)
        logger.info("[通知] 飞书通知已发送")
    except Exception as _ne:
        logger.warning(f"[通知] 飞书通知发送异常（忽略，不影响签到）: {_ne}")


def main():
    # 自愈预热：根据上次失败的 failure_code 自动执行安全清理/延长等待（纯规则，不依赖LLM）
    try:
        for _line in self_heal.preheat():
            logger.info(_line)
    except Exception:
        pass
    logger.info("=" * 58)
    logger.info("油学通自动签到开始（搜索路径版 / 两轮重试 / 成功硬确认）")
    sw, sh = pyautogui.size()
    logger.info(f"[环境] 屏幕={sw}x{sh} 初始前台='{fg_title()}'")
    logger.info("=" * 58)
    log_startup_banner(); mark("脚本启动")
    result = "fail"  # 提前初始化，finally 块引用时不会 NameError
    code = 1         # 同上：提前初始化，finally 里的 step_trace / 自愈都要用真实退出码
    notify_start()   # 开跑先推一条，让"它到底跑没跑"不用靠猜
    try:
        # 0. 运行全程阻止睡眠/熄屏；清除劫持前台的第三方幽灵弹窗（联想电脑管家等）
        keep_awake()
        clear_foreground_blockers()
        # 0.1 锁屏直接快速失败：锁屏界面下截图/点击全部无效，跑两轮也没有意义
        if is_workstation_locked():
            logger.error("[前置] 检测到屏幕已锁定，无法自动操作微信。请在 Windows 电源设置里关闭'自动锁屏/睡眠'、"
                         "保持登录且未锁屏后再跑；本次直接判失败、不关机")
            try:
                shot("FAIL_屏幕锁定")
            except Exception:
                pass
            # 这条最容易静默漏掉：定时任务唤醒了机器，但屏幕锁着，签到其实没开始。
            # 一定要推一条，否则你会以为今晚一切正常。
            notify_feishu("屏幕处于锁定状态，签到无法开始（请检查 Windows 的自动锁屏/睡眠设置）")
            return 1
        # 0.2 开跑前记录环境快照（锁屏/网络/WiFi/电源），并提示签到时间窗
        log_environment_snapshot()
        def _hhm(text):
            try:
                hh, mm = str(text).split(":")
                return int(hh) * 60 + int(mm)
            except Exception:
                return None
        _s, _e = _hhm(SIGNIN_TIME_START), _hhm(SIGNIN_TIME_END)
        _hm = datetime.now().hour * 60 + datetime.now().minute
        _near = (_s is None or _e is None) or (_s - 10 <= _hm <= _e + 5)
        if not _near:
            logger.warning(f"[前置] 当前时间不在晚点名签到窗口({SIGNIN_TIME_START}-{SIGNIN_TIME_END})附近，此时按钮可能是灰的，最终仍以界面按钮为准")
        # 0.5 补丁：自动连接 XSYU_WLAN 并完成校园网网页认证（独立模块，失败不影响签到）
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_helper"))
            import wifi_auto_login
            if not wifi_auto_login.has_internet():
                logger.info("[网络] 检测到无外网，尝试自动连接 XSYU_WLAN 并认证（失败不影响签到）")
                _net_ok = wifi_auto_login.ensure_network()
                logger.info(f"[网络] 自动连接与认证结果={_net_ok}"
                            + ("（现在有外网）" if _net_ok else
                               "（仍未通，签到可能因此失败；详见 wifi_helper\\last_run.log）"))
                log_environment_snapshot()   # 补一条环境快照，便于对照前后状态
            else:
                logger.info("[网络] 已有外网，跳过WiFi认证")
        except Exception as _werr:
            logger.warning(f"[网络] WiFi认证补丁异常（不影响签到）: {_werr}")
        # 1. 启动微信（只做一次；第二轮复用已启动的微信）
        if not wechat_running():
            if not start_wechat():
                notify_feishu("微信启动失败，签到无法开始（详见 run.log 的 [进程] 行）")
                return 1
        # 2. 最多完整走三轮：前两轮间隔 8 秒；若仍未确认且时间窗还有余量，再补第三轮
        #    （时间窗 20:50-21:30 共 40 分钟，两轮约 6~8 分钟，第三轮放得下）
        MAX_ROUNDS = 3
        for rnd in range(1, MAX_ROUNDS + 1):
            # 全局超时保护：防止任何阶段卡死导致脚本无限运行
            if time.time() - T0 > GLOBAL_TIMEOUT:
                logger.error(f"[超时] 全局运行超过{GLOBAL_TIMEOUT}秒，强制终止（防卡死）")
                shot("FAIL_全局超时")
                result = "fail"
                break
            try:
                result = attempt_once(rnd, MAX_ROUNDS)
            except Exception as e:
                logger.exception(f"第{rnd}轮流程异常: {e}")
                try:
                    shot(f"第{rnd}轮_EXCEPTION")
                except Exception:
                    pass
                result = "fail"
            if result == "success":
                logger.info(f"第{rnd}轮已确认签到成功，结束重试")
                break
            if result == "not_time":
                logger.warning("未到签到时段/任务已结束，重试无意义，直接结束")
                break
            if rnd < MAX_ROUNDS:
                # 第三轮是"补救轮"：先冷静 90 秒，且只在时间窗内才跑——
                # 过了 SIGNIN_TIME_END 再点也没意义，不如早点收工发失败通知。
                wait_s = 8 if rnd < MAX_ROUNDS - 1 else 90
                if rnd == MAX_ROUNDS - 1 and not _within_signin_window():
                    logger.warning(f"第{rnd}轮未成功，但已超出签到时间窗({SIGNIN_TIME_START}-{SIGNIN_TIME_END})，"
                                   "放弃第三轮补救（补了也点不动）")
                    break
                logger.warning(f"第{rnd}轮未能确认'已签到'，{wait_s} 秒后冷启动小程序、完整重走一遍"
                               f"（第{rnd+1}/{MAX_ROUNDS}轮）")
                time.sleep(wait_s)
        code = {"success": 0, "not_time": 3}.get(result, 1)
        meaning = {"success": "签到成功（已亲眼见到灰色'已签到'）",
                   "not_time": "不在签到时段/任务已结束（并非失败）"}.get(
                   result, "签到失败（两轮都未确认到'已签到'，不关机）")
        mark("流程结束，结果=%s" % result, "info" if result == "success" else "warning")
        logger.info("=" * 58)
        logger.info("运行总结：")
        logger.info("  开始=%s 结束=%s 总耗时=%.1f 秒"
                    % (datetime.fromtimestamp(T0).strftime("%H:%M:%S"),
                       datetime.now().strftime("%H:%M:%S"), time.time() - T0))
        logger.info("  最终结果=%s  退出码=%d  含义：%s" % (result, code, meaning))
        logger.info("  运行目录=%s（run.log 为完整日志，FAIL_*.png 为失败现场）" % RUN_DIR)
        logger.info("  ---- 关键时间线（秒: 事件）----")
        for _el, _ev in TIMELINE:
            logger.info("    %7.1f  %s" % (_el, _ev))
        logger.info("=" * 58)
        # 写 result.txt：一行快速结果，不用翻日志
        try:
            with open(os.path.join(RUN_DIR, "result.txt"), "w", encoding="utf-8") as _rf:
                _rf.write(f"结果: {result}\n退出码: {code}\n含义: {meaning}\n")
                _rf.write(f"耗时: {time.time()-T0:.1f}秒\n")
                _rf.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                for _el, _ev in TIMELINE:
                    _rf.write(f"  {_el:7.1f}  {_ev}\n")
        except Exception:
            pass
        # 飞书通知：正常收尾（成功/失败/不在时段都发，失败不影响主流程）
        notify_feishu()
        if result == "success":
            close_miniprogram()
            shutdown_pc()
            return 0
        elif result == "not_time":
            close_miniprogram()
            logger.warning(f"不在签到时段/已签到，不关机。请确认定时任务时间是否在 {SIGNIN_TIME_START}-{SIGNIN_TIME_END} 内")
            return 3
        else:
            logger.error("两轮尝试均未能确认签到成功，判定失败、不关机并保留现场；"
                         "请打开上面的运行目录查看 run.log 与 FAIL_*.png 定位原因")
            return 1
    except Exception as e:
        logger.exception(f"脚本异常: {e}")
        code = 2
        try:
            shot("EXCEPTION")
        except Exception:
            pass
        notify_feishu(f"脚本运行中抛出异常：{type(e).__name__}: {e}")
        return 2
    finally:
        # 取消微信置顶（脚本运行期间可能置顶了微信，结束后恢复正常）
        try:
            for _h, _t in enum_windows(True):
                if _t.strip() == "微信":
                    user32.SetWindowPos(_h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST
                    break
        except Exception:
            pass
        # 无论成功/失败/异常都写 step_trace.json（纯观察，异常静默忽略）
        # 必须把最终结果与退出码传进去：原来是无参调用，导致每次 step_trace.json 里
        # final_result / exit_code 都是 null（2026-09-13 成功那次也一样），机器可读的定位等于废了一半。
        try:
            TRACE.flush(final_result=result, exit_code=code)
        except Exception:
            pass
        # 自愈：记录本次结果（not_time 不算失败，不触发预热）
        # _fstep/_fcode 先在上层初始化：下面 try 里任何一步抛错都能被捕获，
        # 但捕获后变量就没了，后面的台账写入会 NameError（被吞掉→台账静默丢失）。
        _fstep = None
        _fcode = None
        try:
            _ec = code
            if _ec not in (0, 3):
                # 只有真正失败（1=两轮未确认 / 2=脚本异常）才从 step_trace 提取失败步骤
                import json as _json
                _stp = os.path.join(RUN_DIR, "step_trace.json")
                if os.path.exists(_stp):
                    with open(_stp, encoding="utf-8") as _f:
                        _j = _json.load(_f)
                    for _r in reversed(_j.get("rounds", [])):
                        if _r.get("status") == "fail":
                            for _s in reversed(_r.get("steps", [])):
                                if _s.get("status") in ("fail", "exception"):
                                    _fstep = _s.get("step_id")
                                    _fcode = _s.get("failure_code")
                                    break
                            break
            _hmsg = self_heal.record_result(_ec, _fstep, _fcode)
            if _hmsg:
                logger.info(_hmsg)
        except Exception:
            pass
        # 台账：把这次的成败写进 data/signin_history.csv（纯新增，失败静默）。
        # 要在 finally 里写而不是收尾处写——异常路径（如抛错退出）同样要留痕，
        # 否则"漏签"最容易漏记的恰恰是异常那几次。
        try:
            if signin_history is not None:
                _row = signin_history.append_record(
                    result=result, code=code, cost_sec=time.time() - T0,
                    start_ts=T0, end_ts=time.time(),
                    fail_step=_fstep, fail_code=_fcode,
                    run_dir=os.path.basename(RUN_DIR))
                if _row:
                    logger.info("[台账] 已记录：%s %s → %s"
                                % (_row.get("date"), _row.get("time"), result))
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
