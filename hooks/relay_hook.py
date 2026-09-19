import sys, json, os, hashlib
from datetime import datetime, timezone

# relay_hook v4 —— TASK-016B：游标式未读注入 + presence 心跳（安全边界与 v3 完全一致）
# v4.1 文案（直推升级）：注入指引改指 .kimi-code/skills/relay-next/SKILL.md（技能新址），
#   并提示【relay-push】直推载荷的接收处理。relay-push 服务端直推与 hook 注入并存：
#   对端在线时直推先达，hook/cron 文件层路径照旧兜底，行为不变。
# v4 变更（其余同 v3）：
#   presence 心跳：ROOT 解析与会话身份（sid）确认后、任何提前退出（UPS 门控关闭 / Stop
#     链长上限 / leader 排除 / 无内容静默）之前，刷新 relay/runtime/presence.json
#     （经 chat_state.presence_set：last_seen/model/cwd；无 model 字段的事件不覆盖已知
#     model；UPS 关闭不等于停止记录活动；刷新失败不阻断 hook，fail-open）
#   游标式未读注入：UserPromptSubmit 与 Stop 的【relay 消息】改为合并注入——
#     以 relay/chat/threads/<tid>/messages.jsonl 线程日志为准，按本会话身份过滤
#     『入站未读』（thread_seq > 游标）：自发消息不算；身份不明（无 sid）拒绝领取；
#     直接按 short8/完整 id 寻址的消息随时可收；角色名寻址（经 roles.json 扩充）仅认
#     本会话首次注册（session-registry）之后发出的消息——角色重绑后旧私聊不自动移交；
#     不复述无关线程。v1 邮箱 pending 照旧领取（pending→read），与线程侧按 msg_id
#     去重合并为一条摘要：单条预览上限 / 总条数上限 / 全批次字节预算（环境变量
#     RELAY_UNREAD_MAX_MSGS / RELAY_UNREAD_MAX_BYTES 可覆盖，默认 10 / 3500），
#     只展开前缀，末尾报剩余条数并给『chat_read --thread 读全文』指引；
#     注入成功输出后才推进游标，且只推进已注入确认的前缀（崩溃语义=可重投通知，
#     至少一次，不宣称严格一次）。
#   依赖（16-A2 已验收，语义不改）：同仓 tools/chat_state.py、tools/chat_read.py。
#     本体部署于 tools/task-010/（同目录）；暂存包部署于 hooks/（../tools）。
#     缺失时 v4 特性优雅降级为 v3 行为（presence/线程未读跳过，邮箱车道照旧）。
# v3 变更（历史）：ROOT 解析 标记>环境变量>静默；SessionStart 注册；UPS
#     additionalContext；Stop 自动续接 decision:block；领导排除/链长上限/原子领取安全阀。
# 事件分工与输出合同见下文 main()。

MAX_CHAIN = 3
BODY_PREVIEW = 120
UNREAD_MAX_MSGS = int(os.environ.get("RELAY_UNREAD_MAX_MSGS") or "10")
UNREAD_MAX_BYTES = int(os.environ.get("RELAY_UNREAD_MAX_BYTES") or "3500")

# 路径依 ROOT 在 main() 内解析（v3 起无硬编码默认根）
RELAY = RUNTIME = REGISTRY = CHAIN_LOG = LEADER_DENY = ROLES = UPS_FLAG = CHAT = None

try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    for _cand in (_HERE, os.path.normpath(os.path.join(_HERE, os.pardir, "tools"))):
        if os.path.isfile(os.path.join(_cand, "chat_state.py")) and _cand not in sys.path:
            sys.path.insert(0, _cand)
    import chat_state
    import chat_read
    V4_READY = True
except Exception:
    V4_READY = False


def done():
    sys.exit(0)


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_leaders():
    try:
        with open(LEADER_DENY, encoding="utf-8") as f:
            return set(l.strip() for l in f if l.strip())
    except Exception:
        return set()


def load_roles():
    try:
        with open(ROLES, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def short8(sid):
    return sid.replace("sess_", "")[:8] if sid else ""


def my_chat_dirs(sid, is_leader, roles):
    """本会话可领取的消息目录名列表（完整id > 短标识 > 角色）。"""
    dirs = []
    if sid:
        full = sid.replace("sess_", "")
        if full:
            dirs.append("to-sess-" + full)
    s8 = short8(sid)
    if s8:
        dirs.append("to-sess-" + s8)
    if is_leader:
        dirs.append("to-leader")
    else:
        emps = roles.get("employees")
        if isinstance(emps, dict):
            for label, info in emps.items():
                if not isinstance(info, dict):
                    continue
                if (s8 and info.get("short") == s8) or (sid and info.get("session_id") == sid):
                    dirs.append("to-" + label)
    return dirs


def claim_chat(dirs):
    """领取发给本会话的全部消息：sha 校验、pending→read 原子改名；坏消息隔离为 read/*.bad。"""
    got, bad = [], []
    for d in dirs:
        pend = os.path.join(CHAT, d, "pending")
        if not os.path.isdir(pend):
            continue
        for name in sorted(os.listdir(pend)):
            if not name.endswith(".json"):
                continue
            src = os.path.join(pend, name)
            try:
                with open(src, encoding="utf-8") as f:
                    msg = json.load(f)
                if not isinstance(msg, dict) or "body" not in msg:
                    raise ValueError("消息缺 body")
                sha = hashlib.sha256(msg["body"].encode("utf-8")).hexdigest()
                if sha != msg.get("body_sha256"):
                    raise ValueError("body_sha256 不一致")
            except Exception as exc:
                dest_dir = os.path.join(CHAT, d, "read")
                os.makedirs(dest_dir, exist_ok=True)
                try:
                    os.rename(src, os.path.join(dest_dir, name + ".bad"))
                    bad.append((d, name, str(exc)))
                except OSError:
                    pass
                continue
            dest_dir = os.path.join(CHAT, d, "read")
            os.makedirs(dest_dir, exist_ok=True)
            try:
                os.rename(src, os.path.join(dest_dir, name))
            except OSError:
                continue  # 被其他事件抢先
            got.append(msg)
    return got, bad


# ---------------- v4：presence / 游标式未读 ----------------

def _parse_epoch(ts):
    """registry 本地 ISO（naive）或消息 UTC '…Z' → epoch 秒；失败返回 None。"""
    try:
        ts = str(ts).strip()
        if ts.endswith("Z"):
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def refresh_presence(root, sid, model, cwd_raw):
    """身份确认后立即刷新 presence；无 model 不覆盖已知 model；失败不阻断。"""
    if not sid or not V4_READY:
        return
    try:
        os.environ["RELAY_SESSION_ID"] = sid
        model = model or ""
        if not model:
            sessions = chat_state.read_state(
                root, os.path.join("relay", "runtime", "presence.json")).get("sessions", {})
            model = (sessions.get(sid) or {}).get("model") or ""
        chat_state.presence_set(root, short8(sid), model, cwd_raw)
    except Exception:
        pass


def _first_seen_epoch(root, sid):
    """本会话在 session-registry.jsonl 的首次注册时间（epoch）；未注册/不可解析返回 None。"""
    try:
        with open(os.path.join(root, "relay", "runtime", "session-registry.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("session_id") == sid:
                    return _parse_epoch(rec.get("ts", ""))
    except OSError:
        return None
    return None


def thread_inbound_unread(root, sid):
    """收集本会话的线程侧入站未读。

    返回 (entries, all_thread_ids)：
      entries   = [{"thread_id", "msg"}, ...] 按 (thread_id, thread_seq, msg_id) 有序；
      all_thread_ids = 线程日志中出现过的全部 msg_id（供邮箱侧合并去重判定）。
    身份规则：直接按 short8/完整 id 寻址 → 任何时候都可收；角色名寻址（roles.json 扩充）
    仅认本会话首次注册之后发出的消息（未注册 fail-closed）——角色重绑后旧私聊不自动移交。
    """
    if not sid or not V4_READY:
        return [], set()
    me = short8(sid)
    aliases = {chat_read.norm_addr(a) for a in chat_read.identity_aliases(root, me)}
    # 补充：roles.json 的 leader 是扁平结构（非 {label:{...}}），identity_aliases 不为其展开
    # 别名——hook 侧补齐（不改 chat_read 语义）；否则生产中 --to leader 的线程消息
    # 永远进不了 leader 会话的未读（员工通知全部走该地址）。
    lead = load_roles().get("leader")
    if isinstance(lead, dict):
        vals = {"leader", lead.get("session_id") or "", lead.get("short") or "", lead.get("label") or ""}
        vals = {v.replace("sess_", "", 1) if v.startswith("sess_") else v for v in vals if v}
        if me in vals or sid in vals or sid.replace("sess_", "", 1) in vals:
            aliases |= {chat_read.norm_addr(v) for v in vals}
    direct = {chat_read.norm_addr(x) for x in (me, sid.replace("sess_", "", 1)) if x}
    cursors = chat_state.cursor_get(root, me)
    tdir = os.path.join(root, "relay", "chat", "threads")
    if not os.path.isdir(tdir):
        return [], set()
    entries, all_ids, first_seen = [], set(), None
    for tid in sorted(os.listdir(tdir)):
        if not os.path.isdir(os.path.join(tdir, tid)):
            continue
        mark = cursors.get(tid, 0)
        seen = set()
        for ok, m in chat_read.iter_thread(root, tid):
            if not ok:
                continue
            all_ids.add(m["msg_id"])
            if m["msg_id"] in seen:
                continue
            seen.add(m["msg_id"])
            if m["thread_seq"] <= mark:
                continue
            to_n = chat_read.norm_addr(m["to"])
            from_n = chat_read.norm_addr(m["from"])
            if to_n not in aliases or from_n in aliases:
                continue  # 非本会话入站（他人收件或自发）
            if to_n not in direct:
                # 角色名寻址：仅认本会话首次注册之后发出的消息
                # （消息时间戳为秒级截断，比较统一降到秒粒度，避免亚秒时序误判）
                if first_seen is None:
                    first_seen = _first_seen_epoch(root, sid)
                if first_seen is None:
                    continue  # 未注册：fail-closed，不认领角色历史
                t = _parse_epoch(m.get("created_at", ""))
                if t is None or int(t) < int(first_seen):
                    continue
            entries.append({"thread_id": tid, "msg": m})
    entries.sort(key=lambda e: (e["thread_id"], e["msg"]["thread_seq"], e["msg"]["msg_id"]))
    return entries, all_ids


def build_unread_summary(tentries, msgs, all_thread_ids):
    """线程未读与已领取邮箱消息合并为一条确定性摘要（预算内只展开前缀，留尾报剩余）。

    返回 (text, shown_entries, remaining)：text 为空表示无未读；
    shown_entries 为已展开注入确认的线程侧条目（供游标只推进已注入前缀）。
    """
    v1only = [m for m in msgs if m.get("msg_id") not in all_thread_ids]
    total = len(tentries) + len(v1only)
    if total == 0:
        return "", [], 0
    head = "【relay 消息】本会话有 %d 条未读（线程日志与 v1 邮箱已按 msg_id 去重，邮箱文件已移入 read/）：" % total
    tail = ("请按 .kimi-code/skills/relay-next/SKILL.md 的【消息模式】处理（直推到达的消息为【relay-push】"
            "载荷时先按【relay-push 直推模式】解析校验；若同时收到任务卡，先做卡再处理消息）。")
    guide = "用 chat_read --thread <thread_id> 可读全文。"
    items = [("t", e) for e in tentries] + [("m", m) for m in
                                            sorted(v1only, key=lambda m: (str(m.get("from", "")), str(m.get("msg_id", ""))))]
    lines, shown = [], []
    used = len(head.encode("utf-8"))
    for kind, it in items:
        if kind == "t":
            m = it["msg"]
            line = "(%d) thread=%s seq=%d msg=%s from=%s kind=%s：%s" % (
                len(lines) + 1, it["thread_id"], m["thread_seq"], m["msg_id"],
                m.get("from", "?"), m.get("kind", "?"),
                m.get("body", "").replace("\n", " ")[:BODY_PREVIEW])
        else:
            line = "(%d) (v1邮箱) msg=%s from=%s kind=%s：%s" % (
                len(lines) + 1, it.get("msg_id", "?"), it.get("from", "?"),
                it.get("kind", "?"), it.get("body", "").replace("\n", " ")[:BODY_PREVIEW])
        b = len(line.encode("utf-8"))
        if len(lines) >= UNREAD_MAX_MSGS or used + b > UNREAD_MAX_BYTES:
            break
        lines.append(line)
        used += b
        shown.append((kind, it))
    remaining = total - len(lines)
    parts = [head] + lines
    if remaining > 0:
        parts.append("另有 %d 条未读未展开（本轮注入预算已满）。%s %s" % (remaining, guide, tail))
    else:
        parts.append(tail)
    return "\n".join(parts), [it for kind, it in shown if kind == "t"], remaining


def advance_shown_prefix(root, sid, shown_entries):
    """注入成功输出后调用：只推进已注入确认的前缀（每线程取已展开条目的最大 seq）。"""
    if not shown_entries or not V4_READY:
        return
    per = {}
    for e in shown_entries:
        per[e["thread_id"]] = max(per.get(e["thread_id"], 0), e["msg"]["thread_seq"])
    try:
        chat_state.cursor_advance(root, short8(sid), per)
    except Exception:
        pass  # 推进失败=下轮按游标重投（可重投通知语义，至少一次）


def main():
    global RELAY, RUNTIME, REGISTRY, CHAIN_LOG, LEADER_DENY, ROLES, UPS_FLAG, CHAT

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 与 tools/ 家族一致：注入 JSON 恒为 UTF-8
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        done()
    if not isinstance(data, dict):
        done()

    def fld(snake):
        v = data.get(snake)
        if not isinstance(v, str) or not v:
            v = data.get({"session_id": "sessionId", "turn_id": "turnId",
                          "model": "model", "cwd": "cwd"}.get(snake, snake))
        return v if isinstance(v, str) else None

    cwd_raw = fld("cwd") or os.getcwd()
    norm = os.path.normcase(os.path.abspath(cwd_raw))

    # ---- v3 ROOT 解析：标记 > 环境变量 > 静默退出 ----
    if os.path.isfile(os.path.join(norm, "relay", "relay.enabled")):
        root = norm
    elif os.environ.get("RELAY_HOOK_ROOT"):
        root = os.path.normcase(os.path.abspath(os.environ["RELAY_HOOK_ROOT"]))
    else:
        root = None
    if root is None:
        done()
    if norm != root:
        done()  # 会话 cwd 与解析出的根不一致（如在别的目录闲聊）：静默

    RELAY = os.path.join(root, "relay")
    RUNTIME = os.path.join(RELAY, "runtime")
    REGISTRY = os.path.join(RUNTIME, "session-registry.jsonl")
    CHAIN_LOG = os.path.join(RUNTIME, "chain-log.jsonl")
    LEADER_DENY = os.path.join(RUNTIME, "leader-sessions.txt")
    ROLES = os.path.join(RUNTIME, "roles.json")
    UPS_FLAG = os.path.join(RUNTIME, "ups-context-enabled")
    CHAT = os.path.join(RELAY, "chat")

    sid = fld("session_id") or os.environ.get("CLAUDE_SESSION_ID") or ""
    os.makedirs(RUNTIME, exist_ok=True)

    # ---- v4 presence 心跳：身份确认后、一切提前退出之前 ----
    refresh_presence(root, sid, fld("model"), cwd_raw)

    if event == "SessionStart":
        append_jsonl(REGISTRY, {"ts": datetime.now().isoformat(), "session_id": sid,
                                "cwd": cwd_raw, "model": fld("model"), "scope": "root"})
        done()

    if event == "UserPromptSubmit":
        if not os.path.isfile(UPS_FLAG):
            done()  # 门控关：不注入、不领件（presence 已照常刷新）
        leaders = load_leaders()
        msgs, bad = claim_chat(my_chat_dirs(sid, sid in leaders, load_roles()))
        ts = datetime.now().isoformat()
        for d, name, why in bad:
            append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid,
                                     "action": "chat-quarantine", "dir": d, "file": name, "why": why})
        if msgs:
            append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid,
                                     "action": "chat-claim+ups", "count": len(msgs)})
        tentries, all_ids = thread_inbound_unread(root, sid)
        text, shown, remaining = build_unread_summary(tentries, msgs, all_ids)
        if not text:
            done()
        if shown:
            append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid,
                                     "action": "unread-inject+ups", "inject": len(shown),
                                     "remaining": remaining, "mailbox": len(msgs)})
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                             "additionalContext": text}},
                                    ensure_ascii=False))
        advance_shown_prefix(root, sid, shown)
        sys.exit(0)

    if event != "Stop":
        done()

    # ---- Stop：自动续接（卡片 + 未读合并一次注入） ----
    leaders = load_leaders()
    is_leader = bool(sid) and sid in leaders

    stop_active = data.get("stopHookActive", data.get("stop_hook_active"))
    turn = fld("turn_id") or ""

    state_path = os.path.join(RUNTIME, "chain-state-%s.json" % ((sid or "unknown")[:13]))
    try:
        st = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        st = {"count": 0, "last_turn": None}
    if stop_active is not True:
        st["count"] = 0  # 新的自然轮次：链计数归零
    if turn and st.get("last_turn") == turn:
        done()
    if st["count"] >= MAX_CHAIN:
        done()

    card = None
    if not is_leader:
        inbox = os.path.join(RELAY, "inbox")
        if os.path.isdir(inbox):
            s8 = short8(sid)
            for name in sorted(os.listdir(inbox)):
                if not name.endswith(".json"):
                    continue
                try:
                    c = json.load(open(os.path.join(inbox, name), encoding="utf-8"))
                except Exception:
                    continue
                if not all(k in c for k in ("task_id", "prompt", "prompt_sha256")):
                    continue
                w = c.get("worker", "any")
                # v1/v3：any 卡，zcode 协议卡，或定向给本会话的卡（sess:<完整id> 或 sess:<8位短标识>）
                if w in ("any", "") or w == "zcode" or (sid and w == "sess:" + sid) or (s8 and w == "sess:" + s8):
                    card = (name, c)
                    break

    msgs, bad = claim_chat(my_chat_dirs(sid, is_leader, load_roles()))
    ts = datetime.now().isoformat()
    for d, name, why in bad:
        append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid, "action": "chat-quarantine",
                                 "dir": d, "file": name, "why": why})
    tentries, all_ids = thread_inbound_unread(root, sid)
    text, shown, remaining = build_unread_summary(tentries, msgs, all_ids)
    if not card and not text:
        done()

    dest_name = None
    if card:
        name, c = card
        s8 = short8(sid) or "hook"
        dest_name = "%s.by-sess-%s.json" % (os.path.splitext(name)[0], s8)
        try:
            os.rename(os.path.join(RELAY, "inbox", name), os.path.join(RELAY, "claimed", dest_name))
        except OSError:
            dest_name = None  # 被其他会话抢先；消息照常投递
    if not dest_name and not text:
        done()

    st["count"] += 1
    st["last_turn"] = turn
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f)

    parts = []
    if dest_name:
        append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid, "turn_id": turn,
                                 "action": "claim+continue", "task_id": card[1]["task_id"],
                                 "chain_count": st["count"], "card": dest_name})
        parts.append("【relay 自动续接】本会话已自动领取下一张任务卡 %s（relay/claimed/%s）。"
                     "请立即按 .kimi-code/skills/relay-next/SKILL.md 的【续接模式】执行该卡：校验哈希 → "
                     "排空式执行（连同下方消息一并处理）→ 写回报 → 队列空后停止。不要自己去 inbox 抢卡。"
                     "回报后的 NOTICE 会经服务端直推领导（push=failed 时文件层已落盘，不影响）。"
                     % (card[1]["task_id"], dest_name))
    if text:
        if msgs:
            append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid, "turn_id": turn,
                                     "action": "chat-claim+stop", "count": len(msgs),
                                     "chain_count": st["count"]})
        if shown:
            append_jsonl(CHAIN_LOG, {"ts": ts, "session_id": sid, "turn_id": turn,
                                     "action": "unread-inject", "inject": len(shown),
                                     "remaining": remaining, "mailbox": len(msgs)})
        parts.append(text)
    sys.stdout.write(json.dumps({"decision": "block", "reason": "\n".join(parts)}, ensure_ascii=False))
    advance_shown_prefix(root, sid, shown)
    sys.exit(0)


try:
    main()
except SystemExit:
    raise
except Exception:
    sys.exit(0)
