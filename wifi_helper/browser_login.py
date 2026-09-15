# -*- coding: utf-8 -*-
"""
browser_login.py — 用真实浏览器自动完成 Dr.COM 校园网登录页的填写与提交。

为什么需要它：
  AC 的 HTTP 登录接口（/drcom/login、:801/eportal/）在客户端直连时会被
  后端判超时（ret_code=8 / msga="Auth Server Timeout"），换任何参数都一样。
  而登录页自己的 JS 带着完整上下文（cookie、隐藏表单 f0、AC 下发的
  终端参数），只有真浏览器跑一遍才最接近人工操作。

实现方式：零第三方依赖。
  - 用 --remote-debugging-port 启动 Edge/Chrome，再用极简 WebSocket 客户端
    走 Chrome DevTools Protocol（CDP），注入 JS 填表并点登录。
  - 只依赖标准库（socket / subprocess / json / base64）。

对外只有一个函数：auto_login(username, password, ...) -> bool
"""
import base64
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl

GATEWAY = "http://10.123.0.253"
PORTAL_URL = GATEWAY + "/a79.htm"

# 触发 AC 重定向用的外部地址（离线时会被 302 到门户，地址里带终端参数）
REDIRECT_PROBE = "http://www.baidu.com"

BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_LOG = None


def _log(msg):
    if _LOG:
        _LOG(f"[浏览器登录] {msg}")


# ══════════════════════════════════════════════════════════
# 极简 WebSocket 客户端（只做 CDP 需要的部分）
# ══════════════════════════════════════════════════════════
class _WS:
    def __init__(self, host, port, path, timeout=10):
        # Edge/Chrome 的 DevTools 只监听 IPv4 回环，且 Host 头要与之匹配
        if host in ("localhost", "::1", None):
            host = "127.0.0.1"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        # 读握手响应
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败：连接被关闭")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError(f"WebSocket 握手失败：{status}")

    # ── 底层收发 ──
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise RuntimeError("WebSocket 连接已断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])          # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        # 【2026-09-16 加固·控制帧校验】RFC 6455 §5.5：控制帧
        # （0x8 close / 0x9 ping / 0xA pong）必须满足两条：
        #   ① payload <= 125 字节
        #   ② 不能分片（FIN 必须为 1）
        # 原来完全不校验。后果：若对端发一个"声称 6 万字节长的 ping"，
        # 下面的 _recv_exact(length) 会阻塞着等 6 万字节，
        # 直到 socket 超时才抛错 —— 而这里是 CDP 的 call() 循环内部，
        # 一卡就整条会话挂住，最终表现为"浏览器登录失败"，
        # 现场只剩一个超时，**看不出是协议层被喂了脏帧**。
        # 这里主动拒绝，并给出可诊断的报错。
        if opcode >= 0x8:
            if length > 125:
                raise RuntimeError(
                    "WebSocket 控制帧长度非法（opcode=0x%X length=%d > 125）："
                    "对端协议异常，主动断开" % (opcode, length))
            if not fin:
                raise RuntimeError(
                    "WebSocket 控制帧被分片（opcode=0x%X）：违反 RFC 6455 §5.5，主动断开" % opcode)

        mask = self._recv_exact(4) if masked else None
        data = self._recv_exact(length) if length else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, opcode, data

    def recv_text(self):
        """收一条完整文本消息（自动处理分片与 ping/pong）。

        【2026-09-16 加固】原来是"不管 opcode，一律 append data、最后 decode 成 utf-8"。
        两个隐患：
          · 收到**二进制帧**（opcode=0x2）时，会被当文本 decode 再交给 json.loads，
            结果是一个语焉不详的 JSONDecodeError —— 排查时看不出"是帧类型不对"。
          · 首次分片帧（opcode=0x1）后续续帧（opcode=0x0）之外，
            出现"两个独立的首帧"这种协议错误也不报错，会被拼接成一段垃圾。
        现在明确区分，并保留"拼接继续"的宽容行为（DevTools 实际会分片发长 JSON）。
        """
        chunks = []
        started = False
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 0x9:      # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:      # pong
                continue
            if opcode == 0x8:      # close
                raise RuntimeError("WebSocket 已被对端关闭")
            if opcode == 0x2:      # 二进制帧：CDP 从不用它，收到说明协议异常
                raise RuntimeError(
                    "WebSocket 收到二进制帧（长度 %d）—— CDP 只用文本帧，协议异常" % len(data))
            if opcode == 0x1:      # 新文本消息的首帧
                if started and chunks:
                    # 上一个消息还没结束就又来了首帧：协议错误，丢弃残片重新开始
                    chunks = []
                started = True
            elif opcode == 0x0:    # 续帧
                if not started:
                    raise RuntimeError("WebSocket 收到孤立续帧（没有前置首帧），协议异常")
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _CDP:
    """一个 target 上的 CDP 会话。"""

    def __init__(self, ws_url, timeout=15, fallback_port=None):
        u = urllib.parse.urlparse(ws_url)
        self.ws = _WS(u.hostname, u.port or fallback_port or 80, u.path or "/", timeout=timeout)
        self._id = 0
        self.events = []          # 收到的 CDP 事件（供诊断用）

    def call(self, method, params=None, timeout=20):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"CDP {method} 超时")

    def drain(self, seconds=3):
        """收一段时间的事件，不阻塞太久。

        【2026-09-16 修复·静默跳出】
        原来的异常处理是：
            except socket.timeout: continue    ← 超时，继续收
            except Exception:      break       ← 其他异常，静默跳出
        "静默"是真问题：`_read_frame` 现在会为协议异常抛 RuntimeError
        （脏帧、二进制帧、孤立续帧…），这些都会走 `break` 被**无声吃掉**。
        调用方看到的现象是"事件少收了几条"，而不是"WebSocket 协议出问题了" ——
        于是真正的根因被掩盖成"页面加载慢"，排查方向直接跑偏。

        现在把两类分开：
          · 超时 → 继续收（这是 drain 的正常节奏，不是错误）
          · 协议/连接错误 → 记下来（self.events 里塞一条诊断事件）并跳出
            —— 不抛给调用方（drain 本就是"尽力而为"的收事件），
               但**必须留痕**，让后面的人知道连接是在这一刻坏的。
        """
        end = time.time() + seconds
        old = self.ws.sock.gettimeout()
        try:
            self.ws.sock.settimeout(0.4)
            while time.time() < end:
                try:
                    msg = json.loads(self.ws.recv_text())
                except (socket.timeout, TimeoutError):
                    continue
                except Exception as e:
                    # 不静默：把"连接为何中断"记成一条事件，供诊断
                    self.events.append({
                        "method": "_drain_aborted",
                        "params": {
                            "error": "%s: %s" % (type(e).__name__, e),
                            "at": time.time(),
                        },
                    })
                    break
                if "method" in msg:
                    self.events.append(msg)
        finally:
            try:
                self.ws.sock.settimeout(old)
            except Exception:
                pass

    def event_methods(self, prefix=""):
        return [e.get("method") for e in self.events if e.get("method", "").startswith(prefix)]

    def resource_urls(self):
        return [e["params"]["request"]["url"] for e in self.events
                if e.get("method") == "Network.requestWillBeSent"]

    def eval(self, expression, timeout=20):
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        if "exceptionDetails" in r:
            det = json.dumps(r["exceptionDetails"], ensure_ascii=False)
            raise RuntimeError("JS 执行异常: " + det[:400])
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ══════════════════════════════════════════════════════════
# 浏览器进程与 DevTools 端点
# ══════════════════════════════════════════════════════════
def find_browser():
    for p in BROWSER_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_json(url, timeout=3):
    # 注意：不要覆盖 Host 头。Chrome/Edge 会用 Host 拼 webSocketDebuggerUrl，
    # 覆盖成不带端口的值会让返回的 ws 地址缺端口。
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _wait_devtools(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
        except Exception:
            time.sleep(0.4)
    return None


def _pick_page(port, want_substr="10.123.0.253", tries=40):
    """找出目标页面。"""
    for _ in range(tries):
        try:
            targets = _http_json(f"http://127.0.0.1:{port}/json/list", timeout=3)
        except Exception:
            targets = []
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for t in pages:
            if want_substr is None or want_substr in (t.get("url") or ""):
                return t
        time.sleep(0.5)
    return None


def capture_portal_url(timeout=8):
    """离线时请求一个外网地址，AC 会 302 到门户，并把终端参数带在地址里。
    这个地址比直接开 a79.htm 信息更全，交给浏览器最接近真实流程。"""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_SSL_CTX))
    try:
        req = urllib.request.Request(REDIRECT_PROBE, headers={"User-Agent": "Mozilla/5.0"})
        # 【2026-09-16 修复·P2-7】opener.open() 返回的 response 在**非重定向**路径
        # （即真的连通了、没抛 HTTPError）下不会进 except，原来就直接丢掉了 ——
        # 底层 socket 要等 GC 才回收。这个函数在每次 wifi 自愈检查里都会被调用，
        # 攒下来可能耗尽句柄/短暂占用连接。用 with 显式关掉。
        # 注意：走 HTTPError 分支时异常自带已关闭的响应，不需要 with。
        with opener.open(req, timeout=timeout) as _resp:
            pass          # 我们只要"有没有被 302 到门户"这个信号，读 body 无意义
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.headers else None
        if loc and "10.123.0.253" in loc:
            return loc
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════
# 注入用的 JS
# ══════════════════════════════════════════════════════════
_JS_FILL = r"""
(function(){
  var U = %(user)s, P = %(pwd)s;

  function visible(el){
    if(!el) return false;
    if((el.type||'').toLowerCase() === 'hidden') return false;
    if(el.disabled) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function setVal(el, v){
    el.focus();
    el.value = v;
    el.dispatchEvent(new Event('input',  {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.dispatchEvent(new Event('keyup',  {bubbles:true}));
    el.dispatchEvent(new Event('blur',   {bubbles:true}));
  }

  // Dr.COM 页面里 DDDDD / upass 各有 hidden 和可见两份，必须挑可见的
  var ins = Array.prototype.slice.call(document.querySelectorAll('input'));
  function pick(preds){
    for(var i=0;i<preds.length;i++){
      for(var j=0;j<ins.length;j++){
        var el = ins[j];
        if(!visible(el)) continue;
        try{ if(preds[i](el)) return el; }catch(e){}
      }
    }
    return null;
  }
  var user = pick([
    function(e){ return e.name==='DDDDD' && (e.type==='text'||e.type==='tel'); },
    function(e){ return e.name==='DDDDD'; },
    function(e){ return e.type==='text' || e.type==='tel'; },
    function(e){ return e.type==='number' || e.type==='email'; }
  ]);
  var pass = pick([
    function(e){ return e.name==='upass' && e.type==='password'; },
    function(e){ return e.name==='upass'; },
    function(e){ return e.type==='password'; }
  ]);
  if(!user || !pass) return 'noform';

  setVal(user, U);
  setVal(pass, P);

  // 表单真正的提交值是那批 hidden，一并同步
  ins.forEach(function(h){
    if((h.type||'').toLowerCase() !== 'hidden') return;
    if(h.name === 'DDDDD'    || h.name === 'user'  || h.name === 'username') h.value = U;
    if(h.name === 'upass'    || h.name === 'userpwd' || h.name === 'password') h.value = P;
  });

  // 勾上"已阅读并同意"和"记住密码"（用 MouseEvent，不会把 checked 又翻回去）
  ins.forEach(function(e){
    if((e.type||'').toLowerCase() === 'checkbox' && !e.checked){
      e.checked = true;
      e.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
      e.dispatchEvent(new Event('change', {bubbles:true}));
    }
  });

  // 找登录按钮：优先可见的 submit，再按文案找
  var btn = null;
  for(var k=0;k<ins.length;k++){
    if(visible(ins[k]) && (ins[k].type||'').toLowerCase() === 'submit'){ btn = ins[k]; break; }
  }
  if(!btn){
    btn = pick([
      function(e){ return /登\s*录|登\s*入|Login|Log\s*In|签\s*入/i.test(e.value||''); },
      function(e){ return (e.type||'').toLowerCase()==='button' || (e.type||'').toLowerCase()==='image'; }
    ]);
  }
  if(btn){
    btn.click();
    return 'clicked:' + (btn.value || btn.name || btn.tagName);
  }
  var f = document.forms && document.forms[0];
  if(f){
    if(f.DDDDD) f.DDDDD.value = U;
    if(f.upass) f.upass.value = P;
    f.submit();
    return 'submitted';
  }
  return 'nobutton';
})()
"""

_JS_RESOURCES = r"""
JSON.stringify(performance.getEntriesByType('resource')
  .map(function(e){return e.name})
  .filter(function(u){return /eportal|drcom|ACSetting|Portal|login/i.test(u);}))
"""

_JS_STATE = r"""
(function(){
  function visible(el){
    if(!el) return false;
    if((el.type||'').toLowerCase() === 'hidden') return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  var ins = Array.prototype.slice.call(document.querySelectorAll('input'));
  var vu = null, vp = null, vbtn = null;
  ins.forEach(function(e){
    if(!visible(e)) return;
    var t = (e.type||'').toLowerCase();
    if(t === 'password' && !vp) vp = e;
    else if((t === 'text' || t === 'tel') && !vu) vu = e;
    if(t === 'submit' && !vbtn) vbtn = e;
  });
  var txt = ((document.body ? document.body.innerText : '') || '').replace(/\s+/g, ' ');
  return {
    url: location.href,
    title: document.title,
    hasUser: !!vu,
    hasPass: !!vp,
    hasBtn: !!vbtn,
    online: /已经成功登录|登录成功|已在线|当前在线/.test(txt),
    failed: /密码错误|账号不存在|认证失败|认证超时|登录失败|认证不成功|在线用户|请联系网管|账户已停用|系统忙/.test(txt),
    snippet: txt.slice(0, 200)
  };
})()
"""


def _internet_ok(urls, timeout=4):
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                body = r.read(400).decode("utf-8", errors="replace")
                if r.status in (200, 204) and not any(
                        m in body for m in ("eportal", "ACSetting", "Dr.COM")):
                    return True
        except Exception:
            continue
    return False


def _eval_retry(box, port, expression, timeout=15):
    """执行 JS；连接断了就重连一次（页面跳转/重载时会发生）。"""
    try:
        return box[0].eval(expression, timeout=timeout)
    except Exception:
        pass
    try:
        t = _pick_page(port, want_substr="10.123.0.253", tries=3) or _pick_page(port, None, tries=3)
        if not t:
            return None
        try:
            box[0].close()
        except Exception:
            pass
        box[0] = _CDP(t["webSocketDebuggerUrl"], fallback_port=port)
        box[0].call("Runtime.enable", timeout=10)
        return box[0].eval(expression, timeout=timeout)
    except Exception:
        return None


def _close_browser(proc, port, profile, wait=8):
    """彻底关掉这次启动的临时浏览器。

    踩过的坑（2026-09-13 20:55 实测）：Edge 会把手上的启动进程转交给真正的
    浏览器进程，`proc.terminate()` 常常只杀掉启动器，真正的窗口留在桌面上、
    profile 也删不掉 —— 当晚就留下了一个标题为"登录成功页"的 Edge 窗口和一个
    `wifi_login_profile_*` 目录（21:30 还活着）。所以按"能优雅就优雅、不能就硬杀"：
      1) CDP 的 Browser.close —— 让浏览器自己退出（最干净，不依赖进程拓扑）
      2) proc.terminate()    —— 兜底杀启动器
      3) taskkill /T /F      —— 连整棵进程树一起杀
    最后等 profile 的锁释放（浏览器退出是异步的，立刻 rmtree 必失败）。
    """
    # 1) 优雅：通过 DevTools 的浏览器级端点让它自己关
    try:
        ver = _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
        ws_url = (ver or {}).get("webSocketDebuggerUrl")
        if ws_url:
            b = _CDP(ws_url, timeout=5, fallback_port=port)
            try:
                # 只发不等：浏览器收到就自己关，回包之前连接就断了，等它没意义
                b.ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
            finally:
                try:
                    b.close()
                except Exception:
                    pass
    except Exception:
        pass
    # 2) 兜底杀启动器
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
        # 3) 硬杀整棵进程树（启动器已退出时这步会失败，忽略即可）
        #
        # 【2026-09-16】这里没写 text=True，所以输出保持 bytes、不会触发解码，
        # 当前是**安全的**。但仍显式加 encoding + text，原因有二：
        #   ① 一致性：全项目 6 处 taskkill 现在都走同一个模式，
        #      以后有人照着别处改成 text=True 时不会踩坑；
        #   ② 防未来变更：一旦有人为了打日志加上 text=True 而忘了 encoding，
        #      中文 Windows 的 GBK 输出就会在 reader 线程里抛
        #      UnicodeDecodeError（异常抓不到、只往 stderr 喷 traceback）。
        #      把它钉死在"带 encoding 的 text 模式"，这个坑就永远踩不到。
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True,
                           encoding="gbk", errors="ignore", timeout=10)
        except Exception:
            pass
    # 等浏览器真正退出、profile 锁释放，再交给调用方 rmtree
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if not os.path.isdir(profile):
                return
            names = os.listdir(profile)
        except Exception:
            return
        if not any(n.startswith("Singleton") or n == "lockfile" for n in names):
            time.sleep(1.0)   # 再多等一拍，避开尚未释放的文件句柄
            return
        time.sleep(0.5)


def auto_login(username, password, log=None, test_urls=None,
               wait_form=60, wait_after_click=120, keep_open_on_fail=True):
    """在真实浏览器里打开校园网登录页，自动填账号密码并点"登 录"。

    返回 True 表示登录后确认有外网；False 表示没成功。
    成功会自动关掉这个临时浏览器；失败默认把窗口留着，方便手工补登。
    """
    global _LOG
    _LOG = log
    if not username or not password:
        _log("未提供账号密码，跳过浏览器自动登录")
        return False

    urls = test_urls or ("http://connectivitycheck.gstatic.com/generate_204",
                         "http://www.baidu.com")

    exe = find_browser()
    if not exe:
        _log("没找到 Edge/Chrome，无法自动填表")
        return False
    _log(f"浏览器: {os.path.basename(exe)}")

    # 1) 尽量拿到 AC 重定向下发的带参门户地址（比裸开 a79.htm 信息全）
    portal = capture_portal_url()
    if portal:
        _log(f"已从 AC 重定向拿到带参地址: {portal[:120]}")
    else:
        portal = PORTAL_URL
        _log("未捕获到重定向，直接打开门户登录页")

    # 2) 起浏览器（独立临时 profile，不碰你日常的浏览器数据）
    #    顺手清掉历史遗留的 profile（失败时窗口留着，profile 就没删成）
    try:
        for _d in os.listdir(tempfile.gettempdir()):
            if _d.startswith("wifi_login_profile_"):
                shutil.rmtree(os.path.join(tempfile.gettempdir(), _d), ignore_errors=True)
    except Exception:
        pass
    port = _free_port()
    profile = os.path.join(tempfile.gettempdir(), f"wifi_login_profile_{random.randint(1000, 9999)}")
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-extensions", "--disable-popup-blocking",
        # 窗口太窄会被门户判成手机端、渲染另一套模板，固定成 PC 尺寸
        "--window-size=1280,900", "--window-position=80,60",
        "--disable-features=msEdgeFirstRunExperience,msImplicitSignin",
        "--new-window", portal,
    ]
    proc = None
    cdp_box = [None]
    ok = False
    clicked = False
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ver = _wait_devtools(port, timeout=40)
        if not ver:
            _log("浏览器 DevTools 端口没起来，放弃自动填表")
            return False
        _log(f"已连接浏览器: {ver.get('Browser', '?')}")

        target = _pick_page(port, want_substr="10.123.0.253", tries=60) or _pick_page(port, None, tries=10)
        if not target:
            _log("没找到门户页面")
            return False
        cdp_box[0] = _CDP(target["webSocketDebuggerUrl"], fallback_port=port)
        cdp_box[0].call("Runtime.enable", timeout=10)

        script = _JS_FILL % {"user": json.dumps(username), "pwd": json.dumps(password)}

        # 3) 等登录表单渲染出来（门户页靠 JS 拼 DOM，要等几秒）
        _log("等待门户登录页渲染...")
        state = {}
        deadline = time.time() + wait_form
        reloaded = False
        while time.time() < deadline:
            state = _eval_retry(cdp_box, port, _JS_STATE) or {}
            if state.get("online"):
                _log(f"门户页显示已在线：{state.get('snippet', '')[:60]}")
                break
            if state.get("hasUser") and state.get("hasPass"):
                res = _eval_retry(cdp_box, port, script, timeout=20)
                _log(f"表单已就绪，填表结果: {res}")
                if res and (str(res).startswith("clicked") or res == "submitted"):
                    clicked = True
                    break
                if res == "noform":
                    pass          # DOM 还没拼完，继续等
                elif res == "nobutton":
                    _log("有输入框但还没出现登录按钮，继续等")
            elif not reloaded and time.time() > deadline - wait_form + 20:
                # 既没表单也没在线提示，刷新一次再等
                _log("页面既没有登录表单也没有在线提示，刷新一次...")
                _eval_retry(cdp_box, port, "location.reload(); 'ok'", timeout=10)
                reloaded = True
            time.sleep(1.5)

        if not clicked and not state.get("online"):
            _log("没能识别出登录表单，放弃自动填表")
        else:
            if clicked:
                _log("已点击登录")
            # 4) 等登录生效
            _log("等待认证生效...")
            deadline = time.time() + wait_after_click
            retried = False
            while time.time() < deadline:
                if _internet_ok(urls):
                    ok = True
                    break
                st = _eval_retry(cdp_box, port, _JS_STATE) or {}
                if st.get("failed"):
                    _log(f"门户页提示失败：{st.get('snippet', '')[:80]}")
                    break
                # 表单还在，说明没提交成功，再点一次
                if clicked and not retried and st.get("hasUser") and st.get("hasPass") and \
                        time.time() > deadline - wait_after_click + 15:
                    _log("表单仍在，重新填一次")
                    _eval_retry(cdp_box, port, script, timeout=20)
                    retried = True
                time.sleep(2)

        # 5) 诊断：把门户相关的请求列出来，方便判断卡在哪一步
        try:
            res_list = _eval_retry(cdp_box, port, _JS_RESOURCES, timeout=10)
            if res_list:
                for u in json.loads(res_list)[-6:]:
                    _log("  门户请求: " + u[:160])
        except Exception:
            pass

        if not ok:
            ok = _internet_ok(urls)
        _log("外网已通，登录成功" if ok else "自动登录未成功")
        return ok

    except Exception as e:
        _log(f"自动填表异常: {type(e).__name__}: {e}")
        return False
    finally:
        if cdp_box[0]:
            try:
                cdp_box[0].close()
            except Exception:
                pass
        if ok or not keep_open_on_fail:
            # 成功就收工；失败默认把窗口留着，方便手工补登
            _close_browser(proc, port, profile)
            for _ in range(3):
                shutil.rmtree(profile, ignore_errors=True)
                if not os.path.isdir(profile):
                    break
                time.sleep(0.8)


if __name__ == "__main__":
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    try:
        # 【2026-09-16 修复·句柄泄漏】原来是 `json.load(open(_cfg, ...))` ——
        # 一行写法看着干净，但文件句柄**永远不会被显式关闭**，
        # 只能等 GC 回收。在 CPython 上通常没事（引用计数会立刻回收），
        # 但这是"靠实现细节兜底"：一旦这个模块被别的解释器/嵌入式场景用到，
        # 或者以后有人加个循环反复读配置，就会攒下句柄。
        # 用 with 明确释放，零成本。
        with open(_cfg, encoding="utf-8") as _fh:
            _c = json.load(_fh)
        _u, _p = _c.get("wifi_username"), _c.get("wifi_password")
    except Exception:
        _u = _p = None
    print(f"账号={_u} 密码={'已填' if _p else '未填'}")
    ok = auto_login(_u, _p, log=print)
    print("结果:", "成功" if ok else "失败")
