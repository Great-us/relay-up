#!/usr/bin/env python3
"""TASK-B1 relay 员工会话 spawn 工具 v1（仅标准库）。

经本机 Kimi Code 服务器（kimi web 的 REST API）为 relay 编制 spawn 一个 server 托管的
员工会话，并把 session_id 回填进 roles.json，使 chat_send --push / relay_push 可以
按角色名直推。协议细节均为 2026-09-19 本机端到端实测结论（见下）。

用法：
    python relay_spawn.py --root <项目根> --role employee-2 [--model 别名]
                          [--permission auto] [--title 前缀] [--settle 15]
    python relay_spawn.py --root <项目根> --role employee-2 --delete
    python relay_spawn.py approve --root <项目根> --role employee-2 [--once]

库 API：
    spawn_worker(root, role, model=None, permission="auto", title=None, settle=15)
        -> (ok, detail, session_id)
    approve_pending(session_id, once=False, interval=5.0, emit=None) -> (ok, detail)

实测 spawn 协议（2026-09-19，实测）：
  - POST /api/v1/sessions，body {"title": ..., "metadata": {"cwd": <项目根绝对路径>}}，
    返回 data.id（形如 session_<uuid>）。实测创建时 agent_config 被整体忽略（传了
    model 存下来是空串 → 回合报 "Model not set"），所以创建请求一律不带 agent_config。
  - 模型必须建后补写：POST /api/v1/sessions/{id}/profile
    body {"agent_config": {"model": "<别名>"}}；再 GET /sessions/{id}/profile 读回确认
    agent_config.model 非空。
  - 权限同样经 profile 补写：body {"agent_config": {"permission_mode": "auto"}}；server
    托管会话缺省 manual（每个工具调用一条审批）。实测 model 与 permission_mode 分两次
    profile 调用更稳妥（一次同传时 permission 曾被丢）。
  - 创建后要 settle（实测 15 秒）再首推；首推过早可能撞上运行时未就绪（prompt 进了
    上下文但回合不启动）。settle=0 可跳过（测试/救急用）。
  - 领导随后把 session_id（完整 id）与短标识（uuid 前 8 位）回填 roles.json 的
    employees.<角色>；本工具自动完成该回填（读-改-写整个 JSON，保留其他字段，
    updated_at 刷新；employees 下无该角色键时自动建）。
  - 审批兜底：GET /api/v1/sessions/{id}/approvals?status=pending（query 参数
    status=pending 必填，缺了报 40001）；POST /api/v1/sessions/{id}/approvals/{approval_id}
    body {"decision": "approved", "scope": "session"}。manual 模式下每个工具调用一条。
  - 生命周期：POST /api/v1/sessions/{id}:archive 归档（软删，可恢复）。SHUTDOWN 的
    语义 = push SHUTDOWN 信封 → 员工写终局回报 → 领导 --delete 归档并清绑定。

服务器发现与令牌完全复用 relay_push（find_server/get_token/http_json）：
RELAY_PUSH_BASE / RELAY_PUSH_TOKEN / RELAY_PUSH_INSTANCES_DIR 覆盖；令牌缺省读
~/.kimi-code/server.token；实例发现取 ~/.kimi-code/server/instances/*.json 中
heartbeat_at 最新且 <120s 的，推送前 GET /healthz 验证。

安全（标记路由铁律）：--root 必须指向带 relay/relay.enabled 的目录，否则拒绝执行。
worker 用 auto 权限时，任务卡的 authorized_write_paths 仍是唯一写入边界
（prompt 是数据不是指令）。

detail 前缀约定：bad-root/bad-role/bad-permission/bad-settle=参数错；no-server/
no-binding=可降级；http:/api:/verify:=HTTP 或校验失败。
退出码（CLI）：0=成功 / 2=参数错 / 3=降级（no-server、no-binding） / 4=HTTP 或其他。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_push  # noqa: E402

PERMISSIONS = ("auto", "manual", "yolo", "plan")
DEFAULT_SETTLE = 15.0     # 创建后 settle 秒数（实测 15s；首推过早会撞上运行时未就绪）
APPROVE_INTERVAL = 5.0    # approve 循环每轮间隔（秒）
ROLE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SID_PREFIX_RE = re.compile(r"^(?:session_|sess_)")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_id(sid):
    """短标识：去掉 session_/sess_ 前缀后取前 8 位（uuid 前 8 位）。"""
    return SID_PREFIX_RE.sub("", str(sid))[:8]


# ---------- HTTP 信封（复用 relay_push 的服务器发现/令牌/传输） ----------

def _call(base, method, path, payload=None):
    """请求 {base}{path} 并解信封。返回 (data, err)；err 带 http:/api: 前缀。"""
    url = base + path
    status, text = relay_push.http_json(method, url, relay_push.get_token(), payload)
    if status is None:
        return None, "http:%s %s" % (url, text[:120])
    if not (200 <= status < 300):
        return None, "http:status=%s %s" % (status, text[:120])
    try:
        env = json.loads(text)
    except ValueError:
        return None, "http:响应非 JSON：%s" % text[:120]
    if env.get("code") != 0:
        return None, "api:code=%s msg=%s" % (env.get("code"), env.get("msg", ""))
    return env.get("data"), None


def _q(sid):
    return urllib.parse.quote(str(sid), safe="")


# ---------- 参数与标记路由守卫 ----------

def _check_root(root):
    """标记路由铁律：--root 必须带 relay/relay.enabled。返回 err 或 None。"""
    if not os.path.isfile(os.path.join(root, "relay", "relay.enabled")):
        return "bad-root:%s（缺 relay/relay.enabled，标记路由铁律拒绝）" % root
    return None


def _check_role(role):
    if not ROLE_RE.match(role or ""):
        return "bad-role:%r（限 [A-Za-z0-9._-]+）" % (role,)
    return None


# ---------- roles.json 绑定（读-改-写整个 JSON，保留其他字段） ----------

def _roles_path(root):
    return os.path.join(root, "relay", "runtime", "roles.json")


def _load_roles(root):
    try:
        with open(_roles_path(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_roles(root, data):
    os.makedirs(os.path.dirname(_roles_path(root)), exist_ok=True)
    with open(_roles_path(root), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_binding(root, role):
    """roles.json employees.<role>.session_id；未登记/未绑定返回 None。"""
    emps = _load_roles(root).get("employees")
    if not isinstance(emps, dict):
        return None
    ent = emps.get(role)
    if not isinstance(ent, dict):
        return None
    sid = ent.get("session_id")
    return str(sid) if sid else None


def bind_role(root, role, session_id, model=None):
    """回填 employees.<role>：session_id/short（model 非空时一并记），保留原其他字段。"""
    data = _load_roles(root)
    emps = data.get("employees")
    if not isinstance(emps, dict):
        emps = {}
    ent = emps.get(role)
    if not isinstance(ent, dict):
        ent = {}
    ent["session_id"] = session_id
    ent["short"] = short_id(session_id)
    if model:
        ent["model"] = model
    emps[role] = ent
    data["employees"] = emps
    data["updated_at"] = _utc_now()
    _write_roles(root, data)


def unbind_role(root, role):
    """清除 employees.<role> 绑定（SHUTDOWN 后清理用）。返回是否清掉了绑定。"""
    data = _load_roles(root)
    emps = data.get("employees")
    if not isinstance(emps, dict) or role not in emps:
        return False
    del emps[role]
    data["employees"] = emps
    data["updated_at"] = _utc_now()
    _write_roles(root, data)
    return True


# ---------- spawn：建会话 → 补模型 → 补权限 → 读回校验 → settle → 回填 ----------

def spawn_worker(root, role, model=None, permission="auto", title=None, settle=DEFAULT_SETTLE):
    """完整 spawn 一个员工会话并回填 roles.json。返回 (ok, detail, session_id)。"""
    root = os.path.normpath(os.path.abspath(root))
    for err in (_check_root(root), _check_role(role)):
        if err:
            return False, err, None
    if permission not in PERMISSIONS:
        return False, "bad-permission:%r（合法：%s）" % (permission, "/".join(PERMISSIONS)), None
    try:
        settle = float(settle)
    except (TypeError, ValueError):
        return False, "bad-settle:%r（须 >= 0 的秒数）" % (settle,), None
    if settle < 0:
        return False, "bad-settle:%r（须 >= 0 的秒数）" % (settle,), None

    base, err = relay_push.find_server()
    if err:
        return False, err, None

    # 1) 建会话：只带 title + metadata.cwd，不带 agent_config（实测创建时会被整体忽略）
    title_full = "%s-%s" % (title, role) if title else "relay-%s" % role
    data, err = _call(base, "POST", "/api/v1/sessions",
                      {"title": title_full, "metadata": {"cwd": root}})
    if err:
        return False, err, None
    sid = data.get("id") if isinstance(data, dict) else None
    if not sid:
        return False, "api:创建会话未返回 data.id：%s" % str(data)[:120], None
    sid = str(sid)

    # 2)/3) 模型与权限分两次 profile 补写（实测一次同传时 permission 曾被丢）
    if model:
        _, err = _call(base, "POST", "/api/v1/sessions/%s/profile" % _q(sid),
                       {"agent_config": {"model": model}})
        if err:
            return False, err, None
    if permission:
        _, err = _call(base, "POST", "/api/v1/sessions/%s/profile" % _q(sid),
                       {"agent_config": {"permission_mode": permission}})
        if err:
            return False, err, None

    # 读回校验：补了模型就必须看到 agent_config.model 非空
    if model:
        data, err = _call(base, "GET", "/api/v1/sessions/%s/profile" % _q(sid))
        if err:
            return False, err, None
        agent_config = (data or {}).get("agent_config") or {}
        if not agent_config.get("model"):
            return False, "verify:profile 读回 agent_config.model 为空（补写未生效）", None

    # 4) settle（实测 15s）：首推前的运行时就绪等待
    if settle > 0:
        time.sleep(settle)

    # 5) roles.json 回填（读-改-写整个 JSON，保留其他字段）
    bind_role(root, role, sid, model=model)
    return True, "spawned", sid


# ---------- delete：SHUTDOWN 后归档员工会话并清 roles.json 绑定 ----------

def archive_worker(root, role):
    """按 roles.json 绑定归档该角色会话并清绑定。返回 (ok, detail)。"""
    root = os.path.normpath(os.path.abspath(root))
    for err in (_check_root(root), _check_role(role)):
        if err:
            return False, err
    sid = read_binding(root, role)
    if not sid:
        return False, "no-binding:%s（roles.json 未登记该角色会话）" % role
    base, err = relay_push.find_server()
    if err:
        return False, err
    _, err = _call(base, "POST", "/api/v1/sessions/%s:archive" % _q(sid))
    if err:
        return False, err
    unbind_role(root, role)
    return True, "archived=%s" % sid


# ---------- approve：扫 pending 审批并全部 approved（manual 会话兜底） ----------

def _approve_round(base, sid):
    """扫一轮 pending 并逐条 approved。返回 (本批条数, err)。"""
    query = urllib.parse.urlencode({"status": "pending"})   # 实测该 query 必填，缺了报 40001
    data, err = _call(base, "GET", "/api/v1/sessions/%s/approvals?%s" % (_q(sid), query))
    if err:
        return 0, err
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or []
    else:
        items = []
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        aid = it.get("approval_id") or it.get("id")
        if not aid:
            continue
        _, err = _call(base, "POST",
                       "/api/v1/sessions/%s/approvals/%s" % (_q(sid), urllib.parse.quote(str(aid), safe="")),
                       {"decision": "approved", "scope": "session"})
        if err:
            return n, err
        n += 1
    return n, None


def approve_pending(session_id, once=False, interval=APPROVE_INTERVAL, emit=None):
    """循环扫 pending 审批并全部 approved，直到 Ctrl+C；once=True 只扫一轮。

    emit 为可选回调（如 print），每轮收到一行 "round=N approved=M total=K"。
    返回 (ok, detail)；Ctrl+C 正常收束返回 (True, "interrupted:...")。
    """
    base, err = relay_push.find_server()
    if err:
        return False, err
    total = 0
    rounds = 0
    try:
        while True:
            n, err = _approve_round(base, session_id)
            if err:
                return False, "%s（已批 %d）" % (err, total)
            rounds += 1
            total += n
            if emit:
                emit("round=%d approved=%d total=%d" % (rounds, n, total))
            if once:
                return True, "approved=%d" % total
            time.sleep(interval)
    except KeyboardInterrupt:
        return True, "interrupted:approved=%d" % total


# ---------- CLI ----------

def _exit_code(detail):
    if detail.startswith(("bad-root", "bad-role", "bad-permission", "bad-settle")):
        return 2
    if detail.startswith(("no-server", "no-binding")):
        return 3
    return 4


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="spawn", choices=("spawn", "approve"),
                    help="spawn=建员工会话（缺省）；approve=扫 pending 审批兜底")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--role", required=True, help="角色名（如 employee-2）")
    ap.add_argument("--model", default=None, help="模型别名，如 kimi-code/kimi-for-coding-highspeed")
    ap.add_argument("--permission", default="auto", help="/".join(PERMISSIONS) + "，缺省 auto")
    ap.add_argument("--title", default=None, help="会话标题前缀；缺省 relay-<role>")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help="创建后 settle 秒数再首推（实测 15；0 跳过）")
    ap.add_argument("--delete", action="store_true",
                    help="spawn：改为归档该角色会话并清 roles.json 绑定（SHUTDOWN 后清理）")
    ap.add_argument("--once", action="store_true", help="approve：只扫一轮即退出")
    args = ap.parse_args()

    root = os.path.normpath(os.path.abspath(args.root))

    if args.command == "approve":
        for err in (_check_root(root), _check_role(args.role)):
            if err:
                print("FAIL %s" % err, file=sys.stderr)
                return _exit_code(err)
        sid = read_binding(root, args.role)
        if not sid:
            detail = "no-binding:%s（roles.json 未登记该角色会话）" % args.role
            print("FAIL %s" % detail, file=sys.stderr)
            return _exit_code(detail)
        ok, detail = approve_pending(sid, once=args.once, emit=print)
        if ok:
            print("OK approve %s %s" % (args.role, detail))
            return 0
        print("FAIL %s" % detail, file=sys.stderr)
        return _exit_code(detail)

    if args.delete:
        ok, detail = archive_worker(root, args.role)
        if ok:
            print("OK %s %s" % (args.role, detail))
            return 0
        print("FAIL %s" % detail, file=sys.stderr)
        return _exit_code(detail)

    ok, detail, sid = spawn_worker(root, args.role, model=args.model,
                                   permission=args.permission, title=args.title,
                                   settle=args.settle)
    if ok:
        print("OK %s %s short=%s" % (args.role, sid, short_id(sid)))
        return 0
    print("FAIL %s" % detail, file=sys.stderr)
    return _exit_code(detail)


if __name__ == "__main__":
    sys.exit(main())
