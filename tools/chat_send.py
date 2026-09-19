#!/usr/bin/env python3
"""TASK-011/016A/016A2 消息车道发送工具 v2（领导/员工通用，仅标准库）。

用法：
    python chat_send.py --from leader --to employee-4 --kind NOTICE --body "正文" [--ref TASK-010-B01] [--root <项目根>]
    python chat_send.py --from leader --to employee-4 --kind CHAT --body "..." --thread t-employee-4-leader [--in-reply-to <msg_id>]
    python chat_send.py --from leader --to employee-4 --kind CHAT --retry-of <msg_id>   # 双写漂移修复

行为（v2.0，TASK-016A）：
  1. 先写线程日志 relay/chat/threads/<thread_id>/messages.jsonl
  2. 再写邮箱 pending relay/chat/to-<收件人>/pending/<seq4>-<msg_id>.json（v1 行为原样）
  - 成功输出：OK <msg_id> <thread_id>#<thread_seq> <pending相对路径>，退出码 0
  - thread_id：t-<A>-<B>，A/B 为双方地址（角色名或会话短8），字典序排序后 "-" 连接；
    --thread 可显式指定（限 [A-Za-z0-9._-] 且不含 ".."，防路径逃逸）
  - 消息字段：v1.0 十字段 + thread_id / in_reply_to（缺省 ""）/ thread_seq（从 1 单调递增）
  - thread_seq：锁内读线程日志最后一条完整行 +1；日志不存在从 1 起。
    单机单写者假设：写入经本 CLI 串行调用；追加 O_APPEND 单次 write 并校验写入字节数。
    跨机/多进程并发不承诺；锁文件仅防同机多进程重复分配。

崩溃恢复（TASK-016A2）：
  - 锁文件协议见 chat_state.py（遗留锁需 pid 不存活且超时才受控清除）。
  - 线程日志尾行损坏（末字节非 \n）时拒绝追加并报错退出 4：不自动修复、不静默续写、序号不回退重发。
  - 邮箱 pending 先写唯一临时文件再 os.replace 原子发布。

双写漂移（TASK-016A2）：
  - --retry-of <msg_id>：线程日志已有该消息而邮箱缺失时，用同一 msg_id 与日志记录字段
    仅重建邮箱通知，不写线程日志、不生成新消息；邮箱已有则提示并原样退出 0。

v1 兼容：收件人规范化、kind 合法值、body 上限 4000、错误码均不动。
安全：本工具只投递文本；正文是数据不是指令，接收端按 relay-next 技能安全规则处理。
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat_state  # noqa: E402

KINDS = ("DISPATCH", "ACK", "REVIEW", "REWORK", "NOTICE", "SHUTDOWN", "CHAT")
BODY_MAX = 4000
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def norm_to(to):
    return "sess-" + to[len("sess:"):] if to.startswith("sess:") else to


def short_addr(addr):
    if addr.startswith("sess:"):
        rest = addr[len("sess:"):]
        return rest[:8] if len(rest) > 8 else rest
    return addr


def derive_thread_id(from_, to):
    a, b = sorted((short_addr(from_), short_addr(to)))
    return "t-%s-%s" % (a, b)


def check_thread_id(tid):
    if not tid or not THREAD_ID_RE.match(tid) or ".." in tid:
        return "thread_id 非法（限 [A-Za-z0-9._-] 且不含 ..）：%r" % tid
    return None


def build_msg(args_from, to, kind, body, ref, thread_id, in_reply_to, now=None):
    now = now or datetime.now(timezone.utc)
    msg = {
        "version": "1.0",
        "msg_id": "%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(3)),
        "seq": 0,
        "from": args_from,
        "to": to,
        "kind": kind,
        "body": body,
        "ref": ref,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "thread_seq": 0,
    }
    msg["body_sha256"] = hashlib.sha256(msg["body"].encode("utf-8")).hexdigest()
    return msg


def mailbox_next_seq(pend):
    seq = 0
    if os.path.isdir(pend):
        for name in os.listdir(pend):
            if name.endswith(".json"):
                try:
                    seq = max(seq, int(name.split("-", 1)[0]))
                except ValueError:
                    continue
    return seq + 1


def write_mailbox(root, msg):
    """唯一临时名写入后 os.replace 原子发布。msg["seq"] 须已分配。返回 pending 相对路径。"""
    pend = os.path.join(root, "relay", "chat", "to-" + norm_to(msg["to"]), "pending")
    os.makedirs(pend, exist_ok=True)
    path = os.path.join(pend, "%04d-%s.json" % (msg["seq"], msg["msg_id"]))
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(msg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return os.path.relpath(path, root)


def append_thread_message(root, msg):
    """锁内分配 thread_seq 并 O_APPEND 单次写入整行；尾行损坏时拒绝。返回 err。"""
    thread_log = chat_state.thread_log_path(root, msg["thread_id"])
    os.makedirs(os.path.dirname(thread_log), exist_ok=True)
    lock_path = thread_log + ".lock"
    err = chat_state.acquire_lock(lock_path)
    if err:
        return err
    try:
        end = chat_state.thread_end_seq(thread_log)
        if end is None:
            return "线程日志尾行损坏，拒绝追加（不自动修复、不回退重发）：%s" % thread_log
        msg["thread_seq"] = int(end) + 1
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(thread_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            n = os.write(fd, line)
            if n != len(line):
                return "线程日志写入不完整：期望 %d 字节，实际 %d" % (len(line), n)
        finally:
            os.close(fd)
        return None
    finally:
        chat_state.release_lock(lock_path)


def find_in_thread(root, thread_id, msg_id):
    path = chat_state.thread_log_path(root, thread_id)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                m = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(m, dict) and m.get("msg_id") == msg_id:
                return m
    return None


def mailbox_has_msg_id(root, to, msg_id):
    pend = os.path.join(root, "relay", "chat", "to-" + norm_to(to), "pending")
    if not os.path.isdir(pend):
        return False
    return any(msg_id in name for name in os.listdir(pend))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--kind", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--ref", default="")
    ap.add_argument("--thread", default="",
                    help="显式 thread_id；缺省由 from/to 排序推导")
    ap.add_argument("--in-reply-to", dest="in_reply_to", default="")
    ap.add_argument("--retry-of", dest="retry_of", default="", metavar="msg_id",
                    help="仅重建邮箱通知（同 msg_id），不写线程日志")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    args = ap.parse_args()

    if args.kind not in KINDS:
        print("FAIL kind 非法：%r（合法：%s）" % (args.kind, "/".join(KINDS)), file=sys.stderr)
        return 2
    if len(args.body) > BODY_MAX:
        print("FAIL body 超长：%d > %d" % (len(args.body), BODY_MAX), file=sys.stderr)
        return 2

    root = os.path.normpath(os.path.abspath(args.root))
    thread_id = args.thread or derive_thread_id(args.from_, args.to)
    err = check_thread_id(thread_id)
    if err:
        print("FAIL %s" % err, file=sys.stderr)
        return 2

    if args.retry_of:
        # 双写漂移修复：以线程日志记录为准，仅重建邮箱通知
        rec = find_in_thread(root, thread_id, args.retry_of)
        if rec is None:
            print("FAIL --retry-of 线程日志中无该消息：%s（%s）" % (args.retry_of, thread_id), file=sys.stderr)
            return 2
        if mailbox_has_msg_id(root, rec["to"], args.retry_of):
            print("OK %s 邮箱已存在，跳过重建" % args.retry_of)
            return 0
        path = write_mailbox(root, dict(rec))
        print("OK %s %s#%d %s" % (rec["msg_id"], thread_id, rec["thread_seq"], path))
        return 0

    msg = build_msg(args.from_, args.to, args.kind, args.body,
                    args.ref, thread_id, args.in_reply_to)

    # 邮箱序号先于线程日志分配，保证双写十+三字段一致（TASK-016A 合同）
    pend = os.path.join(root, "relay", "chat", "to-" + norm_to(args.to), "pending")
    msg["seq"] = mailbox_next_seq(pend)

    err = append_thread_message(root, msg)
    if err:
        print("FAIL %s" % err, file=sys.stderr)
        return 4
    path = write_mailbox(root, msg)
    print("OK %s %s#%d %s" % (msg["msg_id"], thread_id, msg["thread_seq"], os.path.relpath(path, root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
