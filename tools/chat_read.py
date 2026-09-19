#!/usr/bin/env python3
"""TASK-016A/016A2 线程只读查看工具（仅标准库）。

用法：
    python chat_read.py --root <项目根> [--thread <id>] [--threads] [--unread <会话>] [--presence] [--mark]

子命令：
  --threads        列出所有线程（id、消息数、首末时间、最后一句 from）
  --thread <id>    转储该线程全部消息（时间 from→to [kind] body 前200字符 + thread_seq）
  --unread <会话>  按游标列出各线程未读条数与摘要；只统计**发给该会话**的入站消息
  --mark           仅与 --unread 连用：把游标推进到各线程已确认入站消息的最大 thread_seq
  --presence       读 relay/runtime/presence.json（不存在则提示"暂无心跳数据"）

身份过滤（TASK-016A2）：
  - 会话身份集合 = 会话参数本身及其变体（sess: 前缀/短8），并经 relay/runtime/roles.json
    把角色名、短8、完整 session_id 互相扩充；不得拆 thread_id 猜参与者。
  - 入站 = to ∈ 身份集合 且 from ∉ 身份集合（自发消息不计入未读）。
  - --mark 只推进含有已确认入站消息的线程，推进到该线程最后一条入站消息的 thread_seq
    （不是全线程末尾，不碰无关线程）。
  - 回退：当身份无法与任何消息匹配且 roles.json 不存在时，沿用旧行为统计全部消息
    （仍排除 from==该会话 的自发消息），保证 v1 临时根可用。

坏行处理：JSON 解析失败、body_sha256 不符、或"合法 JSON 但不是合法消息对象"
（字段缺失/类型错/thread_seq 非负整数）的行：跳过、计数并在 stderr 列出；不修复写回。

参数校验：--thread/--unread 限 [A-Za-z0-9._-] 且不含 ".."（防路径逃逸）。
游标经 tools/chat_state.py 原子读写，只可推进；越过线程日志末尾报状态异常退出 3。
全程只读（除 --mark）。退出码：0 正常 / 2 参数或校验错误 / 3 状态异常。
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat_state  # noqa: E402

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REQUIRED_STR = ("version", "msg_id", "from", "to", "kind", "body",
                "ref", "created_at", "thread_id", "in_reply_to")


def check_id_arg(value):
    if not value or not SAFE_ID_RE.match(value) or ".." in value:
        return "参数含非法字符（限 [A-Za-z0-9._-] 且不含 ..）：%r" % value
    return None


def valid_message(m):
    """结构化校验：合法 JSON 且为合法消息对象。"""
    if not isinstance(m, dict):
        return False
    for k in REQUIRED_STR:
        if not isinstance(m.get(k), str):
            return False
    if not isinstance(m.get("thread_seq"), int) or isinstance(m.get("thread_seq"), bool) \
            or m["thread_seq"] < 0:
        return False
    if hashlib.sha256(m["body"].encode("utf-8")).hexdigest() != m.get("body_sha256"):
        return False
    return True


def iter_thread(root, thread_id):
    """逐行解析线程日志，产出 (ok, msg_or_reason)。坏行按合同跳过不中断。"""
    path = chat_state.thread_log_path(root, thread_id)
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        for ln, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                yield False, "line %d JSON 解析失败: %s" % (ln, e)
                continue
            if isinstance(msg, dict) and isinstance(msg.get("body"), str) \
                    and hashlib.sha256(msg["body"].encode("utf-8")).hexdigest() != msg.get("body_sha256"):
                yield False, "line %d body_sha256 不符 (msg_id=%s)" % (ln, msg.get("msg_id", "?"))
                continue
            if not valid_message(msg):
                yield False, "line %d 非法消息对象（字段/类型/thread_seq/body_sha256 校验未过, msg_id=%r）" % (
                    ln, msg.get("msg_id") if isinstance(msg, dict) else "?")
                continue
            yield True, msg


def identity_aliases(root, session):
    """把会话参数扩充为身份别名集合（角色名/短8/完整ID 互认）。"""
    aliases = {session}
    stripped = session[len("sess:"):] if session.startswith("sess:") else session
    aliases.add(stripped)
    roles_path = os.path.join(root, "relay", "runtime", "roles.json")
    if os.path.exists(roles_path):
        try:
            with open(roles_path, encoding="utf-8") as f:
                roles = json.load(f)
        except (OSError, ValueError):
            roles = {}
        for group in ("leader", "employees"):
            for role, info in (roles.get(group) or {}).items():
                ids = {role}
                if isinstance(info, dict):
                    ids |= {info.get("session_id") or "", info.get("short") or ""}
                ids = {i for i in ids if i}
                for i in list(ids):
                    ids.add(i.replace("sess_", "", 1) if i.startswith("sess_") else i)
                if stripped in ids:
                    aliases |= ids
    return aliases


def norm_addr(addr):
    return addr.replace("sess:", "", 1).replace("sess_", "", 1)


def is_inbound(msg, aliases):
    return norm_addr(msg["to"]) in aliases and norm_addr(msg["from"]) not in aliases


def list_threads(root):
    tdir = os.path.join(root, "relay", "chat", "threads")
    if not os.path.isdir(tdir):
        return []
    out = []
    for tid in sorted(os.listdir(tdir)):
        msgs = [m for ok, m in iter_thread(root, tid) if ok]
        out.append({"id": tid, "count": len(msgs),
                    "first": msgs[0]["created_at"] if msgs else "",
                    "last": msgs[-1]["created_at"] if msgs else "",
                    "last_from": msgs[-1]["from"] if msgs else ""})
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    ap.add_argument("--thread", default="")
    ap.add_argument("--threads", action="store_true")
    ap.add_argument("--unread", default="", metavar="会话")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--presence", action="store_true")
    args = ap.parse_args()
    root = os.path.normpath(os.path.abspath(args.root))

    if not (args.thread or args.threads or args.unread or args.presence):
        print("FAIL 未指定动作：--threads / --thread <id> / --unread <会话> / --presence 之一", file=sys.stderr)
        return 2
    if args.mark and not args.unread:
        print("FAIL --mark 仅可与 --unread 连用", file=sys.stderr)
        return 2
    if args.thread:
        err = check_id_arg(args.thread)
        if err:
            print("FAIL %s" % err, file=sys.stderr)
            return 2

    if args.presence:
        path = os.path.join(root, "relay", "runtime", "presence.json")
        if not os.path.exists(path):
            print("暂无心跳数据")
        else:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for sid, p in sorted(data.get("sessions", {}).items()):
                print("%s  short=%s model=%s last_seen=%s cwd=%s" % (
                    sid, p.get("short", ""), p.get("model", ""), p.get("last_seen", ""), p.get("cwd", "")))

    if args.threads:
        rows = list_threads(root)
        if not rows:
            print("（无线程）")
        for r in rows:
            print("%s  消息=%d  首=%s  末=%s  最后from=%s" % (
                r["id"], r["count"], r["first"], r["last"], r["last_from"]))

    if args.thread:
        bad = 0
        for ok, m in iter_thread(root, args.thread):
            if not ok:
                bad += 1
                print("跳过坏行：%s" % m, file=sys.stderr)
                continue
            print("#%s %s %s→%s [%s] %s" % (
                m["thread_seq"], m["created_at"], m["from"], m["to"], m["kind"],
                m["body"][:200]))
        if bad:
            print("（坏行 %d 条已跳过）" % bad, file=sys.stderr)

    if args.unread:
        err = check_id_arg(args.unread)
        if err:
            print("FAIL %s" % err, file=sys.stderr)
            return 2
        aliases = identity_aliases(root, args.unread)
        me = norm_addr(args.unread)
        cursor = chat_state.cursor_get(root, me)

        # 第一遍：是否存在任何身份匹配（决定过滤模式，避免逐线程判定污染无关线程）
        resolved = False
        for r in list_threads(root):
            for ok, m in iter_thread(root, r["id"]):
                if ok and is_inbound(m, aliases):
                    resolved = True
                    break
            if resolved:
                break

        new_marks = {}
        for r in list_threads(root):
            seqs, seen_ids = [], set()
            for ok, m in iter_thread(root, r["id"]):
                if not ok:
                    continue
                if m["msg_id"] in seen_ids:      # 同 msg_id 去重
                    continue
                seen_ids.add(m["msg_id"])
                if resolved:
                    if is_inbound(m, aliases):
                        seqs.append(m["thread_seq"])
                elif norm_addr(m["from"]) != me:  # 回退：统计全部，仅排除自发
                    seqs.append(m["thread_seq"])
            if not seqs:
                continue                          # 无（过滤后）消息的线程不动游标
            mark = cursor.get(r["id"], 0)
            unread = [s for s in seqs if s > mark]
            print("%s  未读=%d/%d  (游标=%d, 末=%d)" % (
                r["id"], len(unread), len(seqs), mark, max(seqs)))
            new_marks[r["id"]] = max(seqs)
        if args.mark:
            _, aerr = chat_state.cursor_advance(root, me, new_marks)
            if aerr:
                print("FAIL %s" % aerr, file=sys.stderr)
                return 3
            print("游标已推进并写入 relay/runtime/cursors/%s.json" % me)

    return 0


if __name__ == "__main__":
    sys.exit(main())
