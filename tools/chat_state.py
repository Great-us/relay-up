#!/usr/bin/env python3
"""TASK-016A2 共享状态工具（游标/presence/chat-mute/预算，仅标准库）。

所有 JSON 状态文件统一经【锁内 读取-合并-写回 + 唯一临时名 + os.replace 原子替换】，
修复固定 .tmp 名的并发丢更新。

锁文件协议（TASK-016A2 崩溃恢复）：
  - <目标>.lock 内容为一行 JSON：{"pid": <int>, "ts": "<ISO UTC>"}
  - 遗留锁恢复条件：pid 不存活 **且** 锁龄超过 LOCK_TIMEOUT 秒，二者同时满足才删除；
    不得仅凭"够旧"删除。
  - LOCK_TIMEOUT 可用环境变量 RELAY_LOCK_TIMEOUT 覆盖（默认 30 秒，供测试缩短）。
  - pid 存活探测：os.kill(pid, 0)；Windows 上对不存在进程抛 OSError 视为不存活。

游标：只可推进（单调），新值 ≤ 旧值视为无操作；新值超过线程日志末尾 thread_seq
视为状态异常，报错退出非零，不静默归零。

预算：每线程 × 每会话 × 每对话周期（UTC 日期）自动回复上限 BUDGET_LIMIT=3，
持久化于 relay/runtime/budget.json；同一入站 msg_id 重复投递不重复计数。

CLI（供人工/脚本调用，16-B/C 优先 import 使用）：
  python chat_state.py --root <根> [--cursor-get <短8>] [--cursor-advance <短8> --thread <id> --seq <n>]
                       [--presence-get] [--presence-set --short s --model m --cwd c]
                       [--budget-check --thread <id> --session <短8> --msg-id <id>]
退出码：0 正常 / 2 参数或校验错误 / 3 状态异常（如游标越过日志末尾）/ 4 锁等待超时。
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone

BUDGET_LIMIT = 3
LOCK_TIMEOUT = float(os.environ.get("RELAY_LOCK_TIMEOUT", "30"))
LOCK_POLL = 0.05


# ---------- 进程存活 ----------

def pid_alive(pid):
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lock_age_seconds(info):
    try:
        t = datetime.strptime(info["ts"], "%Y-%m-%dT%H:%M:%SZ")
        return time.time() - t.replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        return float("inf")  # 无法解析视为足够旧（配合 pid 校验）


def acquire_lock(lock_path, timeout=None):
    """获取锁；遗留锁满足恢复协议时受控清除后重试。返回出错信息或 None。"""
    deadline = time.time() + (LOCK_TIMEOUT if timeout is None else timeout)
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            os.write(fd, json.dumps({"pid": os.getpid(), "ts": now}).encode("utf-8"))
            os.close(fd)
            return None
        except FileExistsError:
            info = _read_lock_info(lock_path)
            if info is not None and not pid_alive(info.get("pid")) \
                    and lock_age_seconds(info) > LOCK_TIMEOUT:
                try:
                    os.remove(lock_path)  # 受控恢复：pid 已死且超时
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                return "锁等待超时：%s（持有者=%r）" % (lock_path, info)
            time.sleep(LOCK_POLL)


def _read_lock_info(lock_path):
    """最小窗口读取锁内容：os.open+一次 read+立即 close，降低与释放方的竞争面。"""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def release_lock(lock_path):
    # Windows：他方恰好持有读句柄时 os.remove 会抛 PermissionError，短暂重试
    for _ in range(10):
        try:
            os.remove(lock_path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.01)


# ---------- 原子 JSON 状态读写 ----------

def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def update_state(root, relpath, merge_fn):
    """锁内 读取-合并-写回。merge_fn(data)->(new_data, extra) ，extra 由调用方消费。"""
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = path + ".lock"
    err = acquire_lock(lock)
    if err:
        return None, err
    try:
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except ValueError:
                data = {}  # 状态文件损坏时从空开始合并；原始文件不删除
        new_data, extra = merge_fn(data)
        atomic_write_json(path, new_data)
        return extra, None
    finally:
        release_lock(lock)


def read_state(root, relpath):
    path = os.path.join(root, relpath)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- 线程日志辅助 ----------

def thread_end_seq(thread_log):
    """线程日志末尾 thread_seq；无日志/无完整行返回 0。尾行不完整返回 None（异常）。"""
    if not os.path.exists(thread_log):
        return 0
    with open(thread_log, "rb") as f:
        data = f.read()
    if data and not data.endswith(b"\n"):
        return None
    lines = [l for l in data.split(b"\n") if l.strip()]
    if not lines:
        return 0
    try:
        return int(json.loads(lines[-1].decode("utf-8"))["thread_seq"])
    except (ValueError, KeyError, UnicodeDecodeError):
        return None


def thread_log_path(root, thread_id):
    return os.path.join(root, "relay", "chat", "threads", thread_id, "messages.jsonl")


# ---------- 游标 ----------

def cursor_get(root, short):
    return read_state(root, os.path.join("relay", "runtime", "cursors", short + ".json")).get("threads", {})


def cursor_advance(root, short, seqs):
    """单调推进游标；请求值超过线程日志末尾 → 状态异常（返回 err）。"""
    def merge(data):
        threads = data.get("threads", {})
        for tid, seq in seqs.items():
            threads[tid] = max(int(threads.get(tid, 0)), int(seq))
        data["threads"] = threads
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return data, None
    for tid, seq in seqs.items():
        end = thread_end_seq(thread_log_path(root, tid))
        if end is None:
            return None, "线程 %s 日志尾行损坏，拒绝推进游标" % tid
        if int(seq) > int(end):
            return None, "游标越过日志末尾：%s 请求 %d > 末尾 %d" % (tid, seq, end)
    _, err = update_state(root, os.path.join("relay", "runtime", "cursors", short + ".json"), merge)
    return None, err


# ---------- presence / chat-mute ----------

def presence_set(root, short, model, cwd):
    sid_full = os.environ.get("RELAY_SESSION_ID", short)
    def merge(data):
        sessions = data.get("sessions", {})
        sessions[sid_full] = {"short": short, "model": model, "cwd": cwd,
                              "last_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        data["sessions"] = sessions
        data["updated_at"] = sessions[sid_full]["last_seen"]
        return data, None
    _, err = update_state(root, os.path.join("relay", "runtime", "presence.json"), merge)
    return err


def mute_set(root, threads=None, sessions=None):
    def merge(data):
        if threads is not None:
            data["threads"] = threads
        if sessions is not None:
            data["sessions"] = sessions
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return data, None
    _, err = update_state(root, os.path.join("relay", "runtime", "chat-mute.json"), merge)
    return err


# ---------- 预算 ----------

def budget_key(thread, session, period):
    return "%s|%s|%s" % (thread, session, period)


def budget_check_and_count(root, thread, session, inbound_msg_id, limit=BUDGET_LIMIT):
    """检查并登记一次自动回复预算。返回 (allowed, used, err)。

    同一 inbound_msg_id 重复调用只返回已用值，不递增、不拦截首次判定结果。
    """
    period = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def merge(data):
        entries = data.get("entries", {})
        key = budget_key(thread, session, period)
        ent = entries.get(key, {"count": 0, "msg_ids": []})
        dup = inbound_msg_id in ent["msg_ids"]
        if not dup:
            ent["count"] += 1
            ent["msg_ids"].append(inbound_msg_id)
        entries[key] = ent
        data["entries"] = entries
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return data, (ent, dup)

    out, err = update_state(root, os.path.join("relay", "runtime", "budget.json"), merge)
    if err:
        return None, None, err
    ent, dup = out
    used = ent["count"] if dup else ent["count"] - 1  # 重投不计数：回报当前已用；新登记回报登记前已用
    return used < limit, used, None


# ---------- CLI ----------

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--cursor-get", default="", metavar="短8")
    ap.add_argument("--cursor-advance", default="", metavar="短8")
    ap.add_argument("--thread", default="")
    ap.add_argument("--seq", type=int, default=-1)
    ap.add_argument("--presence-get", action="store_true")
    ap.add_argument("--budget-check", action="store_true")
    ap.add_argument("--session", default="")
    ap.add_argument("--msg-id", default="")
    args = ap.parse_args()
    root = os.path.normpath(os.path.abspath(args.root))

    if args.cursor_get:
        print(json.dumps(cursor_get(root, args.cursor_get), ensure_ascii=False, sort_keys=True))
        return 0
    if args.cursor_advance:
        if not (args.thread and args.seq >= 0):
            print("FAIL --cursor-advance 需配合 --thread 与 --seq>=0", file=sys.stderr)
            return 2
        _, err = cursor_advance(root, args.cursor_advance, {args.thread: args.seq})
        if err:
            print("FAIL %s" % err, file=sys.stderr)
            return 3
        print("OK")
        return 0
    if args.presence_get:
        print(json.dumps(read_state(root, os.path.join("relay", "runtime", "presence.json")),
                         ensure_ascii=False, indent=2))
        return 0
    if args.budget_check:
        if not (args.thread and args.session and args.msg_id):
            print("FAIL --budget-check 需 --thread/--session/--msg-id", file=sys.stderr)
            return 2
        allowed, used, err = budget_check_and_count(root, args.thread, args.session, args.msg_id)
        if err:
            print("FAIL %s" % err, file=sys.stderr)
            return 3
        print("ALLOWED=%s USED=%d LIMIT=%d" % (allowed, used, BUDGET_LIMIT))
        return 0
    print("FAIL 未指定动作", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
