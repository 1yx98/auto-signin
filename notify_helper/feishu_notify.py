# -*- coding: utf-8 -*-
"""
feishu_notify.py — 把签到结果推送到飞书（成功发、失败也发）。

独立文件夹：D:/签到系统/notify_helper/
手动测试：双击 手动测试通知.bat

支持两种接入方式，按 notify_config.json 里"填了哪一种"自动选择：

  1) webhook 模式（推荐，最省事）
     在飞书目标群里 → 设置 → 群机器人 → 添加机器人 → 自定义机器人，
     拿到形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxxx 的地址填进来。
     一个 URL 就够，不用建应用、不用配权限。
     如果机器人开了"签名校验"，把密钥填到 webhook_secret；
     如果开了"自定义关键词"，把关键词填到 webhook_keyword，
     标题里会自动带上它（否则飞书会拒收）。

  2) app 模式（复用你 chaoxing-feishu-morning 那套自建应用）
     填 app_id / app_secret / receive_id / receive_id_type。
     本机这个仓库里没有凭据（它全走 GitHub Secrets），所以需要你自己从
     飞书开放平台 / 群设置里取。

设计原则：
  - 纯标准库（urllib），不依赖 requests，零安装。
  - 任何失败都不抛异常给主流程，只返回 True/False。
  - 发卡片失败自动降级成纯文本；网络失败重试一次。
  - 每次结果追加写同目录 last_notify.log。
  - 凭据只放在 notify_config.json（不进 git、不写日志）。

命令行：
  python feishu_notify.py --self-test                 发一条测试消息
  python feishu_notify.py --dry-run                   只打印将发送的内容，不真发
  python feishu_notify.py --title 标题 --text 内容     发自定义文本
  python feishu_notify.py --latest                    读最近一次签到结果并推送
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import os
import json
import time
import hmac
import base64
import hashlib
import ssl
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "notify_config.json")
LOG_PATH = os.path.join(HERE, "last_notify.log")
# 签到系统的根目录（logs/ 在它下面）
ROOT = os.path.dirname(HERE)

# 历史台账（根目录的 history.py）。只用于在通知里附一句"近 N 天"统计，
# 导入失败或统计异常一律降级为不显示，绝不阻断通知。
try:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import history as _history
except Exception:
    _history = None

API_BASE = "https://open.feishu.cn/open-apis"
REQ_TIMEOUT = 20

_SSL_CTX = ssl.create_default_context()

_LEVEL_TEMPLATE = {
    "success": "green",
    "fail": "red",
    "warning": "orange",
    "info": "blue",
}


# ══════════════════════════════════════════════════════════
# 配置 / 日志
# ══════════════════════════════════════════════════════════
def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    except Exception:
        return {}


def log(msg):
    line = "[飞书通知] %s" % msg
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except Exception:
        pass


def _http_post_json(url, payload, headers=None, timeout=REQ_TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"_err": "%s: %s" % (type(e).__name__, e)}


# ══════════════════════════════════════════════════════════
# 两种发送通道
# ══════════════════════════════════════════════════════════
def _webhook_sign(secret, timestamp):
    """飞书自定义机器人的"签名校验"：用 secret 对 "{timestamp}\\n{secret}" 做 HMAC-SHA256 再 base64。"""
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _send_via_webhook(cfg, msg_type, content):
    url = (cfg.get("webhook") or "").strip()
    if not url:
        return False, "未配置 webhook"
    secret = (cfg.get("webhook_secret") or "").strip()
    payload = {"msg_type": msg_type}
    if msg_type == "interactive":
        payload["card"] = content
    else:
        payload["content"] = {"text": content}
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _webhook_sign(secret, ts)
    status, data = _http_post_json(url, payload)
    code = data.get("code", data.get("StatusCode"))
    if status == 200 and code in (0, None):
        return True, "ok"
    return False, "HTTP %s code=%s msg=%s %s" % (status, code, data.get("msg", data.get("StatusMessage", "")),
                                                 data.get("_err", ""))


def _get_tenant_token(cfg):
    url = API_BASE + "/auth/v3/tenant_access_token/internal"
    payload = {"app_id": (cfg.get("app_id") or "").strip(),
               "app_secret": (cfg.get("app_secret") or "").strip()}
    if not payload["app_id"] or not payload["app_secret"]:
        return None, "未配置 app_id / app_secret"
    status, data = _http_post_json(url, payload)
    if status == 200 and data.get("code") == 0:
        return data.get("tenant_access_token"), "ok"
    return None, "HTTP %s code=%s msg=%s %s" % (status, data.get("code"), data.get("msg", ""),
                                                data.get("_err", ""))


def _send_via_app(cfg, msg_type, content, token):
    receive_id = (cfg.get("receive_id") or "").strip()
    rid_type = (cfg.get("receive_id_type") or "chat_id").strip() or "chat_id"
    if not receive_id:
        return False, "未配置 receive_id"
    if not token:
        return False, "没有 tenant_access_token"
    if msg_type == "interactive":
        body = {"receive_id": receive_id, "msg_type": "interactive",
                "content": json.dumps(content, ensure_ascii=False)}
    else:
        body = {"receive_id": receive_id, "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False)}
    url = API_BASE + "/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": rid_type})
    status, data = _http_post_json(url, body, headers={"Authorization": "Bearer %s" % token})
    if status == 200 and data.get("code") == 0:
        return True, "ok"
    # token 过期：刷新后重试一次
    if data.get("code") == 99991663:
        new_token, msg = _get_tenant_token(cfg)
        if new_token:
            return _send_via_app(cfg, msg_type, content, new_token)
        return False, "token 过期且刷新失败: %s" % msg
    hint = {230002: "机器人不在目标群聊里，请把机器人加进群",
            230013: "接收者不在机器人可用范围",
            230006: "应用未开启机器人能力",
            230027: "缺少 im:message 权限"}.get(data.get("code"), "")
    return False, "HTTP %s code=%s msg=%s %s %s" % (status, data.get("code"), data.get("msg", ""),
                                                   hint, data.get("_err", ""))


# ══════════════════════════════════════════════════════════
# 对外发送接口
# ══════════════════════════════════════════════════════════
def _build_card(title, lines, level="info", keyword=None):
    head = title if not keyword or keyword in title else "%s %s" % (keyword, title)
    elements = [{"tag": "markdown", "content": ln} for ln in lines if ln is not None]
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": True},
        "header": {"title": {"tag": "plain_text", "content": head},
                   "template": _LEVEL_TEMPLATE.get(level, "blue")},
        "body": {"elements": elements or [{"tag": "markdown", "content": "（无内容）"}]},
    }


def send(title, lines, level="info", dry_run=False):
    """发一条飞书消息。返回 True/False。

    title : 卡片标题
    lines : 正文行列表（支持飞书 markdown）
    level : success / fail / warning / info，决定卡片配色
    """
    cfg = _load_config()
    lines = [ln for ln in (lines or []) if ln is not None]
    plain = "\n".join([title] + [str(ln) for ln in lines])

    if dry_run:
        log("DRY-RUN 将发送：")
        for ln in [title] + [str(x) for x in lines]:
            log("  " + str(ln))
        return True

    mode = "webhook" if (cfg.get("webhook") or "").strip() else ("app" if cfg.get("app_id") else "")
    if not mode:
        log("未配置飞书凭据：请编辑 notify_helper\\notify_config.json，"
            "填 webhook（推荐）或 app_id/app_secret/receive_id")
        return False

    keyword = (cfg.get("webhook_keyword") or "").strip()
    card = _build_card(title, lines, level, keyword)

    token = None
    if mode == "app":
        token, msg = _get_tenant_token(cfg)
        if not token:
            log("获取 tenant_access_token 失败: %s" % msg)
            return False

    def _do(msg_type, content):
        if mode == "webhook":
            return _send_via_webhook(cfg, msg_type, content)
        return _send_via_app(cfg, msg_type, content, token)

    # 卡片 → 失败降级纯文本；每种各重试一次
    for attempt in (1, 2):
        ok, info = _do("interactive", card)
        if ok:
            log("已发送（%s/卡片）" % mode)
            return True
        log("卡片发送失败（第%d次）: %s" % (attempt, info))
        if attempt == 1:
            time.sleep(2)

    for attempt in (1, 2):
        ok, info = _do("text", plain)
        if ok:
            log("已发送（%s/降级文本）" % mode)
            return True
        log("纯文本发送失败（第%d次）: %s" % (attempt, info))
        if attempt == 1:
            time.sleep(2)

    log("飞书推送最终失败，请检查 notify_config.json 与机器人配置")
    return False


# ══════════════════════════════════════════════════════════
# 读签到结果，自动组稿
# ══════════════════════════════════════════════════════════
def latest_run_dir():
    """logs/ 下按名字倒序取最新的 run_* 目录。"""
    logs = os.path.join(ROOT, "logs")
    try:
        dirs = [d for d in os.listdir(logs) if d.startswith("run_") and os.path.isdir(os.path.join(logs, d))]
    except Exception:
        return None
    return os.path.join(logs, sorted(dirs)[-1]) if dirs else None


def read_run_summary(run_dir=None):
    """从 run_dir 里读 result.txt / step_trace.json，返回结构化摘要。"""
    run_dir = run_dir or latest_run_dir()
    out = {"run_dir": run_dir, "result": None, "code": None, "meaning": None,
           "cost": None, "time": None, "timeline": [], "fail_step": None,
           "fail_code": None, "screenshots": []}
    if not run_dir or not os.path.isdir(run_dir):
        return out

    rt = os.path.join(run_dir, "result.txt")
    if os.path.isfile(rt):
        try:
            with open(rt, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("结果:"):
                        out["result"] = line.split(":", 1)[1].strip()
                    elif line.startswith("退出码:"):
                        out["code"] = line.split(":", 1)[1].strip()
                    elif line.startswith("含义:"):
                        out["meaning"] = line.split(":", 1)[1].strip()
                    elif line.startswith("耗时:"):
                        out["cost"] = line.split(":", 1)[1].strip()
                    elif line.startswith("时间:"):
                        out["time"] = line.split(":", 1)[1].strip()
                    elif line and line[0].isdigit():
                        out["timeline"].append(line)
        except Exception:
            pass

    st = os.path.join(run_dir, "step_trace.json")
    if os.path.isfile(st):
        try:
            with open(st, encoding="utf-8") as f:
                j = json.load(f)
            for r in reversed(j.get("rounds", [])):
                if r.get("status") == "fail":
                    for s in reversed(r.get("steps", [])):
                        if s.get("status") in ("fail", "exception"):
                            out["fail_step"] = s.get("step_id")
                            out["fail_code"] = s.get("failure_code")
                            break
                    break
        except Exception:
            pass

    try:
        out["screenshots"] = [n for n in os.listdir(run_dir) if n.lower().endswith((".png", ".jpg"))
                              and n.startswith("FAIL_")]
    except Exception:
        pass
    return out


def should_escalate():
    """是否需要升级告警（连续失败 ≥2 天 或 近 7 天失败 ≥3 次）。

    读不到台账就返回 False——宁可少告警，也不能因为统计模块出问题就误报。
    """
    try:
        if _history is None:
            return False, {}
        return _history.should_escalate(7)
    except Exception:
        return False, {}


def notify_signin_result(run_dir=None, extra_lines=None, dry_run=False):
    """读最近一次（或指定）签到结果，推送到飞书。"""
    s = read_run_summary(run_dir)
    result = s.get("result")
    if result == "success":
        title, level, icon = "油学通签到成功", "success", "✅"
    elif result == "not_time":
        title, level, icon = "油学通签到：不在时段", "warning", "🕘"
    elif result:
        title, level, icon = "油学通签到失败", "fail", "❌"
    else:
        title, level, icon = "油学通签到：没读到结果", "warning", "⚠️"

    # 连续失败升级：单次失败可能只是偶然，连续失败才是真出问题了。
    # 只在"本次也失败"时升级配色与标题，成功不会被历史拖累成红色。
    esc = False
    if result and result != "success":
        esc, _st = should_escalate()
        if esc:
            level = "fail"
            title = "油学通签到：连续失败！"

    lines = ["%s **%s**" % (icon, (s.get("meaning") or result or "无结果说明"))]
    meta = []
    if s.get("time"):
        meta.append("时间 %s" % s["time"])
    if s.get("cost"):
        meta.append("耗时 %s" % s["cost"])
    if s.get("code") is not None:
        meta.append("退出码 %s" % s["code"])
    if meta:
        lines.append("　".join(meta))
    if s.get("fail_code"):
        lines.append("失败步骤：`%s`（%s）" % (s.get("fail_step"), s["fail_code"]))
    if s.get("screenshots"):
        lines.append("现场截图：%s" % "、".join(s["screenshots"][:3]))
    if s.get("run_dir"):
        lines.append("目录：`%s`" % os.path.basename(s["run_dir"]))
    # 历史统计：让"这学期漏了几次"不用翻日志
    try:
        if _history is not None:
            _desc = _history.stats(7).get("streak_desc")
            if _desc:
                lines.append("📊 %s" % _desc)
    except Exception:
        pass
    if extra_lines:
        lines.extend(extra_lines)
    if esc:
        lines.append("⚠️ **需要人工检查**：设置「离开后要求登录」是否放宽、WiFi 是否连接、微信是否已登录")
    return send("%s · %s" % (title, time.strftime("%m-%d %H:%M")), lines, level, dry_run=dry_run)


def notify_early_exit(reason, detail_lines=None, run_dir=None, level="fail",
                      title="油学通签到：流程提前中断", dry_run=False):
    """签到流程还没走到收尾就退出了（锁屏 / 微信起不来 / 脚本抛异常）。

    这类情况 result.txt 还没写，所以不能走 read_run_summary()，直接按 reason 组稿。
    这是"失败也发"里最容易被静默漏掉的一类——尤其锁屏。
    """
    lines = ["❌ **%s**" % reason,
             "时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if run_dir and os.path.isdir(run_dir):
        lines.append("目录：`%s`" % os.path.basename(run_dir))
        try:
            shots = sorted(n for n in os.listdir(run_dir) if n.lower().endswith((".png", ".jpg")))
            if shots:
                lines.append("截图：%s" % "、".join(shots[-3:]))
        except Exception:
            pass
    if detail_lines:
        lines.extend(detail_lines)
    return send("%s · %s" % (title, time.strftime("%m-%d %H:%M")), lines, level, dry_run=dry_run)


# ══════════════════════════════════════════════════════════
# 命令行
# ══════════════════════════════════════════════════════════
def _main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    if "--self-test" in args:
        cfg = _load_config()
        mode = "webhook" if (cfg.get("webhook") or "").strip() else ("app" if cfg.get("app_id") else "未配置")
        ok = send("签到助手 · 连通性测试",
                  ["这是一条来自 **签到系统 notify_helper** 的测试消息。",
                   "接入方式：`%s`" % mode,
                   "如果你看到这条，说明飞书推送已经打通。"],
                  "info", dry_run=dry)
        print("结果:", "成功" if ok else "失败")
        return 0 if ok else 1
    if "--latest" in args:
        ok = notify_signin_result(dry_run=dry)
        print("结果:", "成功" if ok else "失败")
        return 0 if ok else 1
    if "--early-exit" in args:
        # 测试"流程提前中断"这条通道。可带自定义原因：--early-exit "屏幕已锁定..."
        i = args.index("--early-exit")
        reason = args[i + 1] if len(args) > i + 1 and not args[i + 1].startswith("--") else "（测试）流程提前中断通道"
        ok = notify_early_exit(reason, run_dir=latest_run_dir(), dry_run=dry)
        print("结果:", "成功" if ok else "失败")
        return 0 if ok else 1
    if "--title" in args:
        i = args.index("--title")
        title = args[i + 1] if len(args) > i + 1 else "签到通知"
        text = ""
        if "--text" in args:
            j = args.index("--text")
            text = args[j + 1] if len(args) > j + 1 else ""
        ok = send(title, [text], "info", dry_run=dry)
        print("结果:", "成功" if ok else "失败")
        return 0 if ok else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
