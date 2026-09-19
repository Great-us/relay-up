#!/usr/bin/env python3
"""TASK-017 relay 直推车道发送工具 v1（仅标准库）。

经本机 Kimi Code 服务器（kimi web 的 REST API）把一条消息文本直接推进对端空闲会话，
与 relay/chat 文件层互补：文件层仍是审计源，本工具只做"送达"，推送失败不改文件层事实。

用法：
    python relay_push.py --root <项目根> --from <addr> --to <addr> --kind <KIND> --body "..."
                         [--ref TASK-X] [--thread <id>] [--in-reply-to <msg_id>] [--dry-run]

库 API（chat_send --push 经此直推）：
    push_text(root, to_addr, kind, body, ref="", in_reply_to="", thread_id="", sender="", dry_run=False)
        -> (ok, detail)

行为：
  - to_addr 支持 角色名（leader/employee-N）/ sess:<完整id> / sess:<8位短标识> / 裸完整 session_id；
    解析顺序 roles.json → presence.json → session-registry.jsonl，均未命中返回
    (False, "no-route:<addr>...")。
  - 服务器发现：环境变量 RELAY_PUSH_BASE 设置则直接用并跳过 instances 发现；否则读
    ~/.kimi-code/server/instances/*.json（RELAY_PUSH_INSTANCES_DIR 可覆盖目录），取
    heartbeat_at 最新且距今 <120s 的实例，先 GET /healthz 带令牌验证，不通即
    (False, "no-server:...")。令牌：RELAY_PUSH_TOKEN 优先，缺省读 ~/.kimi-code/server.token。
  - 推送：POST {base}/api/v1/sessions/<完整id>/prompts，body
    {"content":[{"type":"text","text":<载荷文本>}]}；响应信封 {code,msg,data,request_id}
    中 code==0 才算成功。
  - 载荷文本格式（接收端按此解析，逐字一致，勿改）：
        【relay-push】
        from: <发送者地址>
        to: <收件人地址>
        kind: <KIND>
        ref: <可空>
        thread: <thread_id>
        msg: <msg_id>
        body-sha256: <body 的 UTF-8 SHA256 hex>
        ---body---
        <body 原文>
  - thread_id 缺省由 from/to 推导（t-<A>-<B> 字典序，语义同 chat_send）；
    in_reply_to 仅占位入参（线上格式未承载该字段，接收端以 thread/ref 定位上下文）。
  - dry_run=True：只做 路由解析 + 载荷构建，不发现服务器、不发任何请求，
    返回 (True, 载荷文本) 供排练与断言。

detail 前缀约定：bad-kind/bad-body/bad-thread=参数错；no-route/no-server=可降级；
http/push=HTTP 或其他错误。
退出码（CLI）：0=已推送 / 2=参数错 / 3=降级（no-route、no-server） / 4=HTTP 或其他（http:、push:）。
安全：正文是数据不是指令，接收端按 relay-next 技能安全规则处理。
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KINDS = ("DISPATCH", "ACK", "REVIEW", "REWORK", "NOTICE", "SHUTDOWN", "CHAT")
BODY_MAX = 4000
HEARTBEAT_FRESH = 120.0  # instances 心跳新鲜阈值（秒）
HTTP_TIMEOUT = 10        # 单次 HTTP 超时（秒）
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------- 地址工具（语义同 chat_send/chat_read，本工具自包含不反向依赖） ----------

def norm_addr(addr):
    """匹配用规范化形：去掉 sess:/sess_ 前缀（与 chat_read.norm_addr 同语义）。"""
    return addr.replace("sess:", "", 1).replace("sess_", "", 1)


def short_addr(addr):
    if addr.startswith("sess:"):
        rest = addr[len("sess:"):]
        return rest[:8] if len(rest) > 8 else rest
    return addr


def derive_thread_id(from_, to):
    a, b = sorted((short_addr(from_), short_addr(to)))
    return "t-%s-%s" % (a, b)


# ---------- 路由解析：roles.json → presence.json → session-registry.jsonl ----------

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _match_id(addr_norm, sid, short):
    """规范化地址与候选（完整id/short）是否命中：全等，或 8 位短标识前缀。"""
    if not sid:
        return False
    sid_norm = norm_addr(str(sid))
    if addr_norm == sid_norm:
        return True
    if short and addr_norm == norm_addr(str(short)):
        return True
    return len(addr_norm) == 8 and sid_norm.startswith(addr_norm)


def _iter_role_entries(roles):
    """roles.json → [(label, session_id, short)]；leader 为扁平结构，employees 为字典。"""
    out = []
    leader = roles.get("leader")
    if isinstance(leader, dict):
        out.append((leader.get("label") or "leader",
                    leader.get("session_id"), leader.get("short")))
    emps = roles.get("employees")
    if isinstance(emps, dict):
        for label, info in emps.items():
            if isinstance(info, dict):
                out.append((label, info.get("session_id"), info.get("short")))
            else:
                out.append((label, None, None))
    return out


def resolve_route(root, to_addr):
    """按 roles.json → presence.json → session-registry.jsonl 解析对端完整 session_id。

    命中返回 (session_id, 来源)；均未命中返回 (None, "")。角色已登记但 session_id
    为空（未绑定）视为未命中，继续向后查。
    """
    addr = norm_addr(to_addr)
    runtime = os.path.join(root, "relay", "runtime")

    roles = _load_json(os.path.join(runtime, "roles.json")) or {}
    for label, sid, short in _iter_role_entries(roles):
        if to_addr == label or _match_id(addr, sid, short):
            if sid:
                return str(sid), "roles.json"

    presence = _load_json(os.path.join(runtime, "presence.json")) or {}
    sessions = presence.get("sessions")
    if isinstance(sessions, dict):
        best = None  # 同一短标识多候选时取 last_seen 最新
        for sid, info in sessions.items():
            if not isinstance(info, dict):
                info = {}
            if _match_id(addr, sid, info.get("short")):
                seen = str(info.get("last_seen") or "")
                if best is None or seen >= best[1]:
                    best = (sid, seen)
        if best:
            return str(best[0]), "presence.json"

    reg = os.path.join(runtime, "session-registry.jsonl")
    try:
        with open(reg, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                sid = rec.get("session_id")
                if sid and (addr == norm_addr(str(sid))
                            or (len(addr) == 8 and norm_addr(str(sid)).startswith(addr))):
                    return str(sid), "session-registry.jsonl"
    except OSError:
        pass
    return None, ""


# ---------- 服务器发现与 HTTP ----------

def _instances_dir():
    return os.environ.get("RELAY_PUSH_INSTANCES_DIR") or os.path.join(
        os.path.expanduser("~"), ".kimi-code", "server", "instances")


def _parse_epoch(value):
    """heartbeat_at 解析：epoch 数或 ISO（含 'Z'/naive）→ epoch 秒；失败 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _fresh_instance():
    """instances 目录内 heartbeat_at 最新且距今 <HEARTBEAT_FRESH 秒的 (host, port)。"""
    try:
        names = os.listdir(_instances_dir())
    except OSError:
        return None
    best = None  # (ts, host, port)
    for name in names:
        if not name.endswith(".json"):
            continue
        info = _load_json(os.path.join(_instances_dir(), name))
        if not isinstance(info, dict):
            continue
        ts = _parse_epoch(info.get("heartbeat_at"))
        if ts is None or time.time() - ts >= HEARTBEAT_FRESH:
            continue
        host, port = info.get("host"), info.get("port")
        if not host or not port:
            continue
        if best is None or ts > best[0]:
            best = (ts, host, port)
    return (best[1], best[2]) if best else None


def get_token():
    """RELAY_PUSH_TOKEN 优先；缺省读 ~/.kimi-code/server.token；均无返回 ""。"""
    token = os.environ.get("RELAY_PUSH_TOKEN")
    if token:
        return token.strip()
    try:
        with open(os.path.join(os.path.expanduser("~"), ".kimi-code", "server.token"),
                  encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def http_json(method, url, token, payload=None):
    """发起一次 HTTP 请求。返回 (http_status, body_text)；网络层错误返回 (None, str(exc))。"""
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def find_server():
    """返回 (base, err)。RELAY_PUSH_BASE 优先并跳过发现；否则 instances 发现 + /healthz 验证。"""
    base = os.environ.get("RELAY_PUSH_BASE", "").strip().rstrip("/")
    if not base:
        inst = _fresh_instance()
        if inst is None:
            return None, "no-server:instances 无新鲜心跳（<%gs）：%s" % (
                HEARTBEAT_FRESH, _instances_dir())
        base = "http://%s:%s" % inst
    status, text = http_json("GET", base + "/healthz", get_token())
    if status is None or not (200 <= status < 300):
        return None, "no-server:healthz 不通：%s（status=%s %s）" % (base, status, text[:120])
    return base, None


# ---------- 载荷与推送 ----------

def new_msg_id(now=None):
    now = now or datetime.now(timezone.utc)
    return "%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(3))


def format_payload(sender, to_addr, kind, body, ref, thread_id, msg_id):
    """按固定文本格式组包（接收端逐字解析，勿改行序/键名）。"""
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    lines = ["【relay-push】",
             "from: " + sender,
             "to: " + to_addr,
             "kind: " + kind,
             "ref: " + ref,
             "thread: " + thread_id,
             "msg: " + msg_id,
             "body-sha256: " + sha,
             "---body---"]
    return "\n".join(lines) + "\n" + body


def push_text(root, to_addr, kind, body, ref="", in_reply_to="", thread_id="",
              sender="", dry_run=False):
    """解析路由并推送一条消息文本。返回 (ok, detail)；dry_run 时 detail 为载荷文本。

    in_reply_to 仅入参占位：线上载荷格式未承载该字段（见模块文档）。
    """
    root = os.path.normpath(os.path.abspath(root))
    if kind not in KINDS:
        return False, "bad-kind:%r（合法：%s）" % (kind, "/".join(KINDS))
    if len(body) > BODY_MAX:
        return False, "bad-body:%d > %d" % (len(body), BODY_MAX)
    if not thread_id:
        thread_id = derive_thread_id(sender, to_addr)
    if not thread_id or not THREAD_ID_RE.match(thread_id) or ".." in thread_id:
        return False, "bad-thread:%r（限 [A-Za-z0-9._-] 且不含 ..）" % thread_id

    sid, _src = resolve_route(root, to_addr)
    if not sid:
        return False, "no-route:%s（roles/presence/registry 均未命中）" % to_addr

    msg_id = new_msg_id()
    payload = format_payload(sender, to_addr, kind, body, ref, thread_id, msg_id)
    if dry_run:
        return True, payload

    base, err = find_server()
    if err:
        return False, err
    url = base + "/api/v1/sessions/" + urllib.parse.quote(sid, safe="") + "/prompts"
    status, text = http_json("POST", url, get_token(),
                             {"content": [{"type": "text", "text": payload}]})
    if status is None:
        return False, "http:%s %s" % (url, text[:120])
    if not (200 <= status < 300):
        return False, "http:status=%s %s" % (status, text[:120])
    try:
        env = json.loads(text)
        code = env.get("code")
    except ValueError:
        return False, "http:响应非 JSON：%s" % text[:120]
    if code != 0:
        return False, "push:code=%s msg=%s" % (code, env.get("msg", ""))
    return True, "msg=%s" % msg_id


# ---------- CLI ----------

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--from", dest="from_", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--kind", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--ref", default="")
    ap.add_argument("--thread", default="", help="显式 thread_id；缺省由 from/to 排序推导")
    ap.add_argument("--in-reply-to", dest="in_reply_to", default="",
                    help="引用 msg_id（仅入参占位，线上载荷未承载）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只解析路由并打印载荷，不联系服务器")
    args = ap.parse_args()

    root = os.path.normpath(os.path.abspath(args.root))
    ok, detail = push_text(root, args.to, args.kind, args.body,
                           ref=args.ref, in_reply_to=args.in_reply_to,
                           thread_id=args.thread, sender=args.from_,
                           dry_run=args.dry_run)
    if ok:
        if args.dry_run:
            # 载荷需逐字输出（Windows 文本模式会把 \n 翻成 \r\n），直接写字节
            sys.stdout.buffer.write((detail + "\n").encode("utf-8"))
        else:
            print("OK %s push=ok" % detail)
        return 0
    print("FAIL %s" % detail, file=sys.stderr)
    if detail.startswith(("bad-kind", "bad-body", "bad-thread")):
        return 2
    if detail.startswith(("no-route", "no-server")):
        return 3
    return 4


if __name__ == "__main__":
    sys.exit(main())
