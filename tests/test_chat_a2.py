#!/usr/bin/env python3
"""TASK-016A2 补丁单元测试（仅标准库；子进程 + 临时 --root）。

运行：
    python tests/test_chat_a2.py
覆盖任务书六项补丁：身份过滤、崩溃恢复（遗留锁/坏尾行/写字节校验/邮箱原子发布）、
--retry-of 双写漂移、chat_state 共享状态（原子合并/游标单调/越界报错）、
预算工具化（上限3/持久化/同 msg_id 不重复计数）、输入校验（非法 --thread/非法消息对象）。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_REPO = os.path.normpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(ROOT_REPO, "tools")
CHAT_SEND = os.path.join(TOOLS, "chat_send.py")
CHAT_READ = os.path.join(TOOLS, "chat_read.py")
CHAT_STATE = os.path.join(TOOLS, "chat_state.py")
PY = sys.executable


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-a2-test-")
        self.env = dict(os.environ, RELAY_LOCK_TIMEOUT="2", PYTHONIOENCODING="utf-8")

    def run_tool(self, tool, *extra):
        proc = subprocess.run([PY, tool, "--root", self.root] + list(extra),
                              capture_output=True, timeout=60, env=self.env)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def send(self, *extra):
        return self.run_tool(CHAT_SEND, *extra)

    def read(self, *extra):
        return self.run_tool(CHAT_READ, *extra)

    def thread_log(self, tid="t-employee-4-leader"):
        return os.path.join(self.root, "relay", "chat", "threads", tid, "messages.jsonl")

    def read_log(self, tid="t-employee-4-leader"):
        with open(self.thread_log(tid), "rb") as f:
            return [json.loads(l.decode("utf-8")) for l in f if l.strip()]


class P1IdentityFilter(Base):
    """补丁1：--unread 只统计发给该会话的入站消息；自发不计；mark 不碰无关线程。"""

    def test_inbound_only_and_mark_scoped(self):
        # 角色 a 是 employee-4（收 3 条），自发 1 条；另建一个与该会话无关的线程
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "in1")
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "in2")
        self.send("--from", "employee-4", "--to", "leader", "--kind", "CHAT", "--body", "self-out")
        self.send("--from", "leader", "--to", "employee-5", "--kind", "CHAT", "--body", "other")
        # roles.json 缺失 → 回退模式：统计全部但排除 from==该会话
        rc, out, err = self.read("--unread", "4abf3d15")
        self.assertEqual(rc, 0, err)
        self.assertIn("t-employee-4-leader  未读=3/3", out)
        self.assertIn("t-employee-5-leader  未读=1/1", out)   # 回退模式可见
        # 写 roles.json 进入精确身份过滤：4abf3d15=employee-4
        roles_dir = os.path.join(self.root, "relay", "runtime")
        os.makedirs(roles_dir, exist_ok=True)
        with open(os.path.join(roles_dir, "roles.json"), "w", encoding="utf-8") as f:
            json.dump({"leader": {}, "employees": {
                "employee-4": {"session_id": "sess_4abf3d15-1111", "short": "4abf3d15"},
                "employee-5": {"session_id": "sess_eeeeeeee-2222", "short": "eeeeeeee"}}}, f)
        rc, out, err = self.read("--unread", "4abf3d15")
        self.assertEqual(rc, 0, err)
        self.assertIn("t-employee-4-leader  未读=2/2", out)   # 3 入站，1 自发被排除
        self.assertNotIn("t-employee-5-leader", out)          # 无关线程不出现
        rc, _, err = self.read("--unread", "4abf3d15", "--mark")
        self.assertEqual(rc, 0, err)
        with open(os.path.join(roles_dir, "cursors", "4abf3d15.json"), encoding="utf-8") as f:
            curs = json.load(f)["threads"]
        self.assertEqual(curs, {"t-employee-4-leader": 2})    # 推进到入站末尾(seq2)，自发 seq3 不计入，不碰无关线程


class P2CrashRecovery(Base):
    """补丁2：遗留锁受控恢复；坏尾行阻止追加；邮箱唯一临时名原子发布。"""

    def test_stale_lock_recovered_and_live_lock_refused(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "seed")
        log = self.thread_log()
        lock = log + ".lock"
        # 留一个"死进程 + 足够旧"的遗留锁 → 必须受控恢复后成功
        with open(lock, "w") as f:
            json.dump({"pid": 0, "ts": "2000-01-01T00:00:00Z"}, f)
        rc, out, err = self.send("--from", "leader", "--to", "employee-4",
                                 "--kind", "CHAT", "--body", "after-stale")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(lock))
        # 活进程 + 新锁 → 拒绝（RELAY_LOCK_TIMEOUT=2 秒）
        with open(lock, "w") as f:
            json.dump({"pid": os.getpid(), "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        rc, _, err = self.send("--from", "leader", "--to", "employee-4",
                               "--kind", "CHAT", "--body", "blocked")
        self.assertEqual(rc, 4, err)
        self.assertIn("锁等待超时", err)
        os.remove(lock)

    def test_corrupt_tail_blocks_append(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "ok1")
        with open(self.thread_log(), "ab") as f:
            f.write(b'{"thread_seq": 2, "body": "half')
        before = len(self.read_log_safe())
        rc, out, err = self.send("--from", "leader", "--to", "employee-4",
                                 "--kind", "CHAT", "--body", "must-fail")
        self.assertEqual(rc, 4, err)
        self.assertIn("尾行损坏", err)
        self.assertEqual(len(self.read_log_safe()), before)   # 未续写

    def read_log_safe(self):
        try:
            return self.read_log()
        except (OSError, ValueError):
            # 含坏行时逐行尽力解析（跳过解析失败的行，如损坏的尾半行）
            out = []
            with open(self.thread_log(), "rb") as f:
                for l in f:
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        out.append(json.loads(l.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        pass
            return out

    def test_mailbox_atomic_publish_no_tmp_left(self):
        rc, out, _ = self.send("--from", "leader", "--to", "employee-4",
                               "--kind", "CHAT", "--body", "atomic")
        self.assertEqual(rc, 0)
        pend = os.path.join(self.root, "relay", "chat", "to-employee-4", "pending")
        leftovers = [n for n in os.listdir(pend) if ".tmp-" in n]
        self.assertEqual(leftovers, [])


class P3RetryOf(Base):
    """补丁3：--retry-of 同 msg_id 仅重建邮箱，不写新日志；同 msg_id 读取去重。"""

    def test_retry_of_rebuilds_mailbox_only(self):
        rc, out, _ = self.send("--from", "leader", "--to", "employee-4",
                               "--kind", "CHAT", "--body", "drift")
        self.assertEqual(rc, 0)
        msg_id = out.split()[1]
        log_lines_before = len(self.read_log())
        # 模拟漂移：邮箱通知丢失
        pend = os.path.join(self.root, "relay", "chat", "to-employee-4", "pending")
        for n in os.listdir(pend):
            os.remove(os.path.join(pend, n))
        rc, out2, err = self.send("--from", "x", "--to", "y", "--kind", "CHAT",
                                  "--body", "ignored", "--thread", "t-employee-4-leader",
                                  "--retry-of", msg_id)
        self.assertEqual(rc, 0, err)
        self.assertIn(msg_id, out2)
        self.assertEqual(len(self.read_log()), log_lines_before)  # 日志未新增
        pend2 = os.path.join(self.root, "relay", "chat", "to-employee-4", "pending")
        names = os.listdir(pend2)
        self.assertEqual(len(names), 1)
        with open(os.path.join(pend2, names[0]), encoding="utf-8") as f:
            rebuilt = json.load(f)
        self.assertEqual(rebuilt["msg_id"], msg_id)
        self.assertEqual(rebuilt["body"], "drift")
        self.assertEqual(rebuilt["thread_id"], "t-employee-4-leader")

    def test_retry_of_missing_in_log_fails(self):
        rc, _, err = self.send("--from", "x", "--to", "y", "--kind", "CHAT", "--body", "z",
                               "--thread", "t-a-b", "--retry-of", "20260101T000000Z-000000")
        self.assertEqual(rc, 2, err)
        self.assertIn("线程日志中无该消息", err)


class P4ChatState(Base):
    """补丁4：共享状态原子合并；游标单调；越界报状态异常。"""

    def test_cursor_merge_and_monotonic(self):
        for i in range(3):
            self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "m%d" % i)
        rc, out, err = self.run_tool(CHAT_STATE, "--cursor-advance", "4abf3d15",
                                     "--thread", "t-employee-4-leader", "--seq", "2")
        self.assertEqual(rc, 0, err)
        # 推进另一个线程不应丢失第一个线程的游标（合并语义）
        self.send("--from", "leader", "--to", "employee-5", "--kind", "CHAT", "--body", "o")
        rc, out, err = self.run_tool(CHAT_STATE, "--cursor-advance", "4abf3d15",
                                     "--thread", "t-employee-5-leader", "--seq", "1")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_tool(CHAT_STATE, "--cursor-get", "4abf3d15")
        curs = json.loads(out)
        self.assertEqual(curs, {"t-employee-4-leader": 2, "t-employee-5-leader": 1})

    def test_cursor_beyond_end_rejected(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "one")
        rc, _, err = self.run_tool(CHAT_STATE, "--cursor-advance", "4abf3d15",
                                   "--thread", "t-employee-4-leader", "--seq", "99")
        self.assertEqual(rc, 3, err)
        self.assertIn("越过日志末尾", err)
        rc, out, _ = self.run_tool(CHAT_STATE, "--cursor-get", "4abf3d15")
        self.assertEqual(json.loads(out), {})                 # 未静默归零或写入


class P5Budget(Base):
    """补丁5：每线程×会话×周期上限3；持久化；同 msg_id 不重复计数。"""

    def budget(self, thread, session, msg_id):
        return self.run_tool(CHAT_STATE, "--budget-check",
                             "--thread", thread, "--session", session, "--msg-id", msg_id)

    def test_limit3_persist_no_double_count(self):
        tid = "t-employee-4-leader"
        alloweds = []
        for i in range(4):
            rc, out, err = self.budget(tid, "4abf3d15", "in-msg-%d" % i)
            self.assertEqual(rc, 0, err)
            alloweds.append(out.startswith("ALLOWED=True"))
        self.assertEqual(alloweds, [True, True, True, False])
        # 同一 msg_id 重投：不重复计数，且不改变判定
        rc, out, err = self.budget(tid, "4abf3d15", "in-msg-2")
        self.assertEqual(rc, 0, err)
        self.assertIn("ALLOWED=False USED=4", out)  # 重投不重复计数：已用仍为 4
        with open(os.path.join(self.root, "relay", "runtime", "budget.json"), encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        ent = entries[next(iter(entries))]
        self.assertEqual(len(ent["msg_ids"]), len(set(ent["msg_ids"])))  # msg_ids 无重复
        # 持久化：新进程读取（subprocess 天然新进程；再验证一次第4条仍拒）
        rc, out, _ = self.budget(tid, "4abf3d15", "in-msg-9")
        self.assertTrue(out.startswith("ALLOWED=False"))
        # 独立线程/会话互不影响
        rc, out, _ = self.budget("t-x-y", "eeeeeeee", "other-1")
        self.assertTrue(out.startswith("ALLOWED=True"))


class P6Validation(Base):
    """补丁6：参数与消息对象校验。"""

    def test_thread_path_escape_rejected(self):
        rc, _, err = self.send("--from", "a", "--to", "b", "--kind", "CHAT",
                               "--body", "x", "--thread", "../evil")
        self.assertEqual(rc, 2, err)
        self.assertIn("thread_id 非法", err)
        rc, _, err = self.read("--thread", "a/b")
        self.assertEqual(rc, 2, err)
        self.assertIn("非法字符", err)

    def test_invalid_message_object_counted_as_bad_line(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "good")
        # 合法 JSON 但缺字段、thread_seq 为负 → 都按坏行处理，不当合法消息
        with open(self.thread_log(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"msg_id": "x", "thread_seq": -1}) + "\n")
            f.write(json.dumps({"msg_id": "y", "body": "no-sha", "thread_seq": 2}) + "\n")
        rc, out, err = self.read("--thread", "t-employee-4-leader")
        self.assertEqual(rc, 0, err)
        self.assertIn("good", out)
        self.assertNotIn("no-sha", out)
        self.assertIn("坏行 2 条", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
