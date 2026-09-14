# -*- coding: utf-8 -*-
"""
wifi_auto_login.py — 独立补丁模块：自动连接 XSYU_WLAN 并完成校园网网页认证。

独立文件夹：D:/签到系统/wifi_helper/
手动测试：双击 手动测试.bat

设计原则：
  - 完全可选：主程序调用时任何异常都被吞掉，失败不影响签到流程。
  - 纯标准库（urllib），不依赖 requests，零安装。
  - 直接打 AC 的认证接口，不模拟浏览器点击。
  - 全部接口失败时才兜底用系统默认浏览器打开认证页（用户可手动补登）。
  - 已有外网时直接跳过，不重复认证。
  - 界面大小不影响连接：WiFi 连接用 netsh，认证用 HTTP，均不依赖窗口尺寸。

2026-09-13 修复（依据对 10.123.0.253 门户的实际探测结果）：
  1. 【关键】登录请求超时从 8s 提到 40s。
     AC 内核 /drcom/login 实测要 16 秒才回（它要等后端的认证服务器）。
     原代码 8 秒就抛 TimeoutError 并断开连接，认证根本没走完，
     所以"等数据面 25 秒"永远等不到东西——这是本次认证失败的直接原因。
  2. 不再把"超时"当作"请求已发出=成功"。改为读取真实响应，
     解析 msga / ret_code 并写进日志，失败原因一目了然。
  3. 不再一上来就注销。原来每轮开头无条件注销，会打死 AC 上可能已经
     存在的有效会话；现在只在"两轮之间"才做一次兜底注销。
  4. 登录接口按探测结果分三路依次尝试：
       a. /drcom/login                       （AC 内核，loginMethod=0 本地认证）
       b. :801/eportal/?c=Portal&a=login     （PORTAL 协议，loginMethod=1）
       c. :801/eportal/?c=ACSetting&a=Login  （旧版 eportal）
     本机 IP 10.129.6.111 落在 xsyupublic-副本1 段，门户 config.js 算出
     认证方式=1，但内核接口同样可用，两条路都保留。
  5. 网关页面是 GBK，原来一律按 utf-8 解码，日志里中文全是乱码。
     现在优先按 gbk 解码。
  6. 兜底浏览器从"等 20 秒"放宽到 90 秒（登录页要加载 JS 再提交）。
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import os
import re
import json
import time

# 日志文件：每次运行都记录，方便排查
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.log")
_log_file = None
try:
    _log_file = open(_LOG_PATH, "w", encoding="utf-8")
except Exception:
    pass
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import ssl

# ── 配置 ──────────────────────────────────────────────
SSID = "XSYU_WLAN"
GATEWAY = "http://10.123.0.253"
EPORTAL = GATEWAY + ":801/eportal/"

# 外网探测：国内外各留一个，避免单一探测源被墙导致误判
PROBE_URLS = (
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.baidu.com",
    "http://www.qq.com",
)

HTTP_TIMEOUT = 8      # 普通请求（网关页面、状态查询）
LOGIN_TIMEOUT = 40    # 登录请求：AC 内核要 ~16s 才回，必须给足，别省
BROWSER_WAIT = 90     # 浏览器兜底等待秒数

# 配置文件在上级目录（D:\签到系统\config.json）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

# Dr.COM 认证页特征串（用于区分"真外网"和"被劫持到认证页"）
DRCOM_MARKERS = ("eportal", "ACSetting", "DDDDD", "upass", "Dr.COMWebLogin",
                 "authloginpath", "authuserfield")

# Dr.COM ret_code -> 人话（源自门户 a42.js 注释）
RET_CODE_TEXT = {
    "0": "成功", "1": "账号或密码不对", "2": "IP已经在线", "3": "系统忙",
    "4": "未知错误", "5": "REQ_CHALLENGE失败", "6": "REQ_CHALLENGE超时",
    "7": "认证失败", "8": "认证超时（AC 联系不上后端认证服务器）",
    "9": "下线失败", "10": "下线超时", "11": "其他错误",
}

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _load_credentials():
    """从 config.json 读取校园网账号密码。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        user = cfg.get("wifi_username") or cfg.get("campus_username")
        pwd = cfg.get("wifi_password") or cfg.get("campus_password")
        return user, pwd
    except Exception:
        return None, None


def _log(msg):
    line = f"[WiFi认证] {msg}"
    print(line, flush=True)
    if _log_file:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except Exception:
            pass


def _decode(raw):
    """网关页面是 GBK，优先按 gbk 解，再退回 utf-8。"""
    if not raw:
        return ""
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _request(url, timeout=HTTP_TIMEOUT, headers=None, data=None, opener=None):
    """统一的 HTTP 请求。返回 (status, final_url, text)。失败返回 (-1, url, 错误串)。"""
    hdr = {"User-Agent": _UA, "Accept": "*/*"}
    if headers:
        hdr.update(headers)
    try:
        req = urllib.request.Request(url, data=data, headers=hdr)
        if opener is not None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        with resp:
            return resp.status, resp.geturl(), _decode(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = _decode(e.read())
        except Exception:
            body = ""
        return e.code, (e.geturl() if hasattr(e, "geturl") else url), body
    except Exception as e:
        return -1, url, f"{type(e).__name__}: {e}"


def _http_get(url, params=None, timeout=HTTP_TIMEOUT):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _request(url, timeout=timeout)


def _is_real_internet(url, timeout):
    """向单个地址发请求，判断是不是真的通外网。"""
    status, final_url, text = _request(url, timeout=timeout)
    # 被重定向到认证网关 = 没网
    if "10.123.0.253" in (final_url or ""):
        return False
    # 200/204 但内容是 Dr.COM 认证页 = 没网
    if status in (200, 204) and any(m in (text or "") for m in DRCOM_MARKERS):
        return False
    return status in (200, 204)


# ── 1. 网络连通性检测 ─────────────────────────────────
def has_internet(fast=False):
    """检测是否已有外网。Dr.COM 认证页可能返回 200，需排除认证页特征。

    fast=True 时用较短超时，供认证后的高频轮询使用。
    """
    timeout = 3 if fast else 5
    for url in PROBE_URLS:
        try:
            if _is_real_internet(url, timeout):
                return True
        except Exception:
            continue
    return False


# ── 2. 连接 WiFi ─────────────────────────────────────
def _wifi_profile_xml():
    return f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{SSID}</name>
  <SSIDConfig><SSID><name>{SSID}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM><security><authEncryption>
    <authentication>open</authentication>
    <encryption>none</encryption>
  </authEncryption></security></MSM>
</WLANProfile>'''


def _netsh(*args, timeout=15):
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=timeout)
    return _decode(r.stdout or b"")


def connect_wifi():
    """用 netsh 连接 XSYU_WLAN（命令行操作，不依赖界面大小）。"""
    try:
        if SSID not in _netsh("show", "profiles", timeout=10):
            tmp = os.path.join(os.environ.get("TEMP", "."), f"{SSID}_profile.xml")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_wifi_profile_xml())
            _netsh("add", "profile", f"filename={tmp}", timeout=10)
            try:
                os.remove(tmp)
            except Exception:
                pass
        _netsh("connect", f"name={SSID}")
        for _ in range(10):
            time.sleep(1)
            if _current_ssid() == SSID:
                _log(f"已连接 {SSID}")
                return True
        _log(f"连接 {SSID} 超时")
        return False
    except Exception as e:
        _log(f"连接WiFi异常: {e}")
        return False


def _current_ssid():
    try:
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", _netsh("show", "interfaces", timeout=8), re.M)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _local_ip():
    """取本机在 WLAN 上的 IPv4。"""
    try:
        out = _decode(subprocess.run(["ipconfig"], capture_output=True, timeout=10).stdout or b"")
        m = re.search(r"IPv4 地址[^:]*:\s*([\d.]+)", out) or re.search(r"IPv4 Address[^:]*:\s*([\d.]+)", out)
        return m.group(1) if m else "0.0.0.0"
    except Exception:
        return "0.0.0.0"


# ── 3. HTTP 认证（Dr.COM） ────────────────────────────
def _make_opener():
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_SSL_CTX),
    )


def _chkstatus(opener):
    """查 AC 内核会话状态。返回 (online, uid)。online 为 True/False/None(查询失败)。"""
    status, _fu, text = _request(
        f"{GATEWAY}/drcom/chkstatus?callback=cb&_={int(time.time() * 1000)}",
        timeout=8, opener=opener)
    m = re.search(r'"result"\s*:\s*"?(\d+)"?', text or "")
    if not m:
        return None, ""
    uid = re.search(r'"uid"\s*:\s*"([^"]*)"', text or "")
    return m.group(1) == "1", (uid.group(1) if uid else "")


def _logout(opener):
    """兜底注销，清掉 AC 上的残留会话（仅在两轮之间调用）。"""
    for url in (f"{EPORTAL}?c=Portal&a=logout",
                f"{EPORTAL}?c=ACSetting&a=Logout&ver=1.0&url=drappall"):
        s, _fu, t = _request(url, timeout=10, opener=opener)
        _log(f"  注销 {url.split('?')[1][:32]} -> {s} {(t or '')[:60]!r}")


def _login_drcom(opener, username, password):
    """AC 内核 /drcom/login（loginMethod=0 本地认证）。
    实测要 16 秒左右才回，必须等满 —— 提前断开连接会让认证半途而废。"""
    params = {
        "callback": f"dr{int(time.time() * 1000)}",
        "DDDDD": username, "upass": password, "0MKKey": "123456",
        "R1": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
        "v": str(int(time.time() * 1000) % 100000),
    }
    url = f"{GATEWAY}/drcom/login?" + urllib.parse.urlencode(params)
    t0 = time.time()
    s, _fu, text = _request(url, timeout=LOGIN_TIMEOUT, opener=opener,
                            headers={"Referer": f"{GATEWAY}/a79.htm"})
    dt = round(time.time() - t0, 1)
    if s == -1:
        _log(f"  /drcom/login 连接失败（{dt}s）: {text}")
        return False, ""
    m = re.search(r'"result"\s*:\s*"?(\d+)"?', text or "")
    res = m.group(1) if m else "?"
    msga = re.search(r'"msga"\s*:\s*"([^"]*)"', text or "")
    _log(f"  /drcom/login -> {s} ({dt}s) result={res}"
         + (f' msga="{msga.group(1)}"' if msga else ""))
    return res == "1", (msga.group(1) if msga else "")


def _login_portal(opener, username, password):
    """:801/eportal/?c=Portal&a=login（PORTAL 协议，loginMethod=1）。"""
    params = {
        "c": "Portal", "a": "login",
        "callback": f"dr{int(time.time() * 1000)}",
        "login_method": "1",
        "user_account": username, "user_password": password,
        "wlan_user_ip": _local_ip(), "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "", "wlan_ac_name": "",
        "jsVersion": "3.0", "v": str(int(time.time() * 1000) % 100000),
    }
    url = EPORTAL + "?" + urllib.parse.urlencode(params)
    t0 = time.time()
    s, _fu, text = _request(url, timeout=LOGIN_TIMEOUT, opener=opener,
                            headers={"Referer": f"{GATEWAY}/a79.htm"})
    dt = round(time.time() - t0, 1)
    if s == -1:
        _log(f"  Portal登录 连接失败（{dt}s）: {text}")
        return False, ""
    res = re.search(r'"result"\s*:\s*"?(\w*)"?', text or "")
    code = re.search(r'"ret_code"\s*:\s*"?(\d+)"?', text or "")
    res_v = res.group(1) if res else "?"
    code_v = code.group(1) if code else "?"
    _log(f"  Portal登录 -> {s} ({dt}s) result={res_v} ret_code={code_v}"
         f" ({RET_CODE_TEXT.get(code_v, '未知')})")
    # ret_code 2 = IP已经在线，也算成功
    return res_v in ("1", "ok") or code_v == "2", code_v


def _login_acsetting(opener, username, password):
    """旧版 :801/eportal/?c=ACSetting&a=Login。"""
    params = {
        "c": "ACSetting", "a": "Login", "ver": "1.0", "url": "drappall",
        "DDDDD": username, "upass": password, "0MKKey": "123456",
        "R1": "0", "R3": "0", "R6": "0", "para": "00", "v6ip": "",
    }
    url = EPORTAL + "?" + urllib.parse.urlencode(params)
    t0 = time.time()
    s, _fu, text = _request(url, timeout=LOGIN_TIMEOUT, opener=opener,
                            headers={"Referer": f"{GATEWAY}/a79.htm"})
    dt = round(time.time() - t0, 1)
    if s == -1:
        _log(f"  ACSetting登录 连接失败（{dt}s）: {text}")
        return False, ""
    msga = re.search(r"msga\s*=\s*'?(\d+)'?", text or "")
    ok = "succeed" in (text or "").lower()
    tail = ""
    if not ok:
        idx = (text or "").find("Login")
        tail = " " + repr((text or "")[idx:idx + 20] if idx >= 0 else (text or "")[:40])
    _log(f"  ACSetting登录 -> {s} ({dt}s) msga={msga.group(1) if msga else '?'}{tail}")
    return ok, (msga.group(1) if msga else "")


def _wait_online(seconds, tag):
    """认证后等数据面建立。"""
    deadline = time.time() + seconds
    n = 0
    while time.time() < deadline:
        time.sleep(1.5)
        n += 1
        if has_internet(fast=True):
            _log(f"  {tag} 之后外网已通（约 {n * 1.5:.0f} 秒）")
            return True
    _log(f"  {tag} 之后 {seconds} 秒内仍无外网")
    return False


def _is_backend_timeout(code):
    """判断接口是不是在说"AC 联系不上后端认证服务器"。
    这种情况换个接口/再试一轮都没用，应该尽快让位给浏览器。"""
    c = str(code or "")
    return c == "8" or "Auth Server Timeout" in c or "认证超时" in c


def try_http_auth(username, password):
    """依次尝试三个真实登录接口。只要有一个把外网打通就返回 True。

    各接口都是实测存在的（2026-09-13 探测 10.123.0.253 确认）：
      /drcom/login                      —— AC 内核，返回 msga
      :801/eportal/?c=Portal&a=login    —— PORTAL 协议
      :801/eportal/?c=ACSetting&a=Login —— 旧版
    """
    if not username or not password:
        return False
    opener = _make_opener()

    # 先建上下文，顺便看看 AC 是否认为我们已在线
    _request(f"{GATEWAY}/a79.htm", timeout=10, opener=opener)
    online, uid = _chkstatus(opener)
    _log(f"AC 会话状态: online={online} uid={uid or '(空)'}")

    attempts = (
        ("内核 /drcom/login", _login_drcom),
        ("PORTAL 协议", _login_portal),
        ("旧版 ACSetting", _login_acsetting),
    )
    for round_num in (1, 2):
        detail = []
        backend_timeout = False
        for name, fn in attempts:
            if has_internet(fast=True):
                _log("外网已经通了")
                return True
            _log(f"第{round_num}轮 · 尝试{name}...")
            try:
                ok, code = fn(opener, username, password)
            except Exception as e:
                _log(f"  {name} 异常: {e}")
                detail.append(f"{name}=异常")
                continue
            detail.append(f"{name}={'OK' if ok else (code or 'FAIL')}")
            if _is_backend_timeout(code):
                # 后端认证服务器超时，其余接口/轮次都救不了
                backend_timeout = True
                break
            if _wait_online(15, name):
                return True

        online, uid = _chkstatus(opener)
        _log(f"第{round_num}轮结束：AC 会话 online={online} uid={uid or '(空)'}"
             f"；各接口：{' | '.join(detail)}")

        if backend_timeout:
            _log("AC 联系不上后端认证服务器（校园侧问题），HTTP 这条路不再重试")
            break

        if round_num == 1:
            # 三个接口都没打通，做一次兜底注销再重来一遍
            _log("本轮未通，注销清残留会话后重试...")
            try:
                _logout(opener)
            except Exception as e:
                _log(f"  注销异常(忽略): {e}")
            time.sleep(3)

    return has_internet(fast=True)


# ── 4. 兜底：用真实浏览器自动填表登录 ─────────────
def open_browser_auth(username=None, password=None):
    """HTTP 接口打不通时的兜底：起一个真浏览器打开校园网登录页，
    自动把账号密码填进去、勾上"已阅读"、点"登 录"。

    走的是浏览器自己的 JS（它带着 AC 下发的终端参数和 cookie），
    比脚本手搓请求更接近人工操作。
    """
    user, pwd = username, password
    if not (user and pwd):
        user, pwd = _load_credentials()

    try:
        import browser_login
    except Exception as e:
        _log(f"浏览器自动化模块不可用（{e}），退化为直接打开登录页")
        browser_login = None

    if browser_login and user and pwd:
        _log("改用真实浏览器自动填表登录...")
        try:
            if browser_login.auto_login(user, pwd, log=_log, test_urls=PROBE_URLS):
                return True
        except Exception as e:
            _log(f"浏览器自动登录异常: {e}")
        _log("浏览器自动登录未成功（窗口已留着，可以手工把密码补上）")
        return has_internet(fast=True)

    # 没有自动化模块或没有凭据时，至少把登录页打开
    _log(f"打开默认浏览器到登录页（最多等 {BROWSER_WAIT} 秒）...")
    try:
        os.startfile(f"{GATEWAY}/a79.htm")
    except Exception as e:
        _log(f"打开浏览器失败: {e}")
        return has_internet()
    deadline = time.time() + BROWSER_WAIT
    while time.time() < deadline:
        time.sleep(3)
        if has_internet(fast=True):
            _log("浏览器打开后外网已通")
            return True
    _log(f"浏览器打开后 {BROWSER_WAIT} 秒外网仍未通")
    return has_internet()


# ── 主入口 ────────────────────────────────────────────
def ensure_network(force=False):
    """
    确保网络可用。任何环节失败都返回 False，不抛异常。
      1. 已有外网 → 跳过
      2. 连接 XSYU_WLAN
      3. HTTP 认证 → 失败则打开浏览器兜底
    """
    try:
        if not force and has_internet():
            _log("已有外网，跳过WiFi认证")
            return True

        if _current_ssid() != SSID:
            if not connect_wifi():
                return False
            # 等 DHCP 拿到 IP；AC 若已按 MAC 免认证，这里就通了
            for i in range(12):
                time.sleep(1)
                if has_internet(fast=True):
                    _log(f"连接后外网已通（{i + 1}秒），无需认证")
                    return True

        username, password = _load_credentials()
        if not (username and password):
            _log("config.json 未配置 wifi_username/wifi_password，跳过HTTP认证")
            return open_browser_auth()

        if try_http_auth(username, password):
            _log("网络已通（登录接口明细见上）")
            return True

        _log("HTTP 认证未成功，改用浏览器自动登录")
        return open_browser_auth(username, password)
    except Exception as e:
        _log(f"ensure_network 异常（不影响主程序）: {e}")
        return False


if __name__ == "__main__":
    # --auto：给定时任务用。不等回车，并把结果写进退出码（0=有网，1=没连上，2=异常）。
    # 手动双击 手动测试.bat 时不带这个参数，保持原来的"跑完停一下"行为。
    AUTO = any(a in ("--auto", "--no-pause") for a in sys.argv[1:])
    code = 0
    try:
        # 时间戳放在这里打，而不是让 .bat 用 %date% 打 —— cmd 的 %date% 含"星期"，
        # 会以 GBK 写进日志，和 Python 输出的 UTF-8 正文混在一起变成乱码。
        print("---- %s ----" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 50)
        print("校园网自动连接与认证" + ("（定时任务模式）" if AUTO else " - 手动测试"))
        print("=" * 50)
        u, p = _load_credentials()
        print(f"配置: 账号={u} 密码={'已填' if p else '未填'}")
        print(f"当前WiFi: {_current_ssid()}")
        print(f"本机IP: {_local_ip()}")
        print(f"当前有外网: {has_internet()}")
        print("-" * 50)
        # 定时任务模式用 force=False：已经有网就立刻返回，不去打扰本来好好的连接；
        # 手动测试用 force=True：显式走一遍认证，方便排查。
        ok = ensure_network(force=not AUTO)
        print("-" * 50)
        print(f"结果: {'成功' if ok else '失败'}")
        print(f"最终有外网: {has_internet()}")
        code = 0 if ok else 1
        if _log_file:
            _log_file.write(f"\n最终结果: success={ok} ssid={_current_ssid()} "
                            f"ip={_local_ip()} internet={has_internet()}\n")
            _log_file.close()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"\n异常: {e}")
        print(err)
        code = 2
        if _log_file:
            _log_file.write(f"\n异常: {e}\n{err}\n")
            _log_file.close()
    if AUTO:
        sys.exit(code)
    try:
        input("\n按回车键退出...")
    except Exception:
        pass
    sys.exit(code)
