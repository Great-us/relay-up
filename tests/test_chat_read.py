#!/usr/bin/env python3
"""TASK-016A chat_read.py 单元测试（仅标准库；子进程调用 + 临时 --root）。

运行：
    python tests/test_chat_read.py
覆盖任务书要求：坏行容错（跳过+计数+退出码0）、--threads/--thread 输出、
--unread 游标前后未读数变化、--mark 游标推进落盘、--presence 缺文件提示。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_REPO = os.path.normpath(os.path.join(HERE, ".."))
CHAT_SEND = os.path.join(ROOT_REPO, "tools", "chat_send.py")
CHAT_READ = os.path.join(ROOT_REPO, "tools", "chat_read.py")
PY = sys.executable


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-chatread-test-")

    def send(self, *extra):
        proc = subprocess.run([PY, CHAT_SEND, "--root", self.root] + list(extra),
                              capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def read(self, *extra):
        proc = subprocess.run([PY, CHAT_READ, "--root", self.root] + list(extra),
                              capture_output=True, timeout=30)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))


class BadLines(Base):
    def test_half_json_line_skipped_counted_exit0(self):
        for i in range(2):
            self.send("--from", "leader", "--to", "employee-4",
                      "--kind", "CHAT", "--body", "正常%d" % (i + 1))
        log = os.path.join(self.root, "relay", "chat", "threads",
                           "t-employee-4-leader", "messages.jsonl")
        with open(log, "ab") as f:
            f.write(b'{"thread_seq": 3, "body": "half')  # 手工半行坏 JSON
        rc, out, err = self.read("--thread", "t-employee-4-leader")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("正常"), 2)             # 好行不中断
        self.assertIn("坏行 1 条", err)
        self.assertIn("JSON 解析失败", err)

    def test_sha_mismatch_skipped(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "x")
        log = os.path.join(self.root, "relay", "chat", "threads",
                           "t-employee-4-leader", "messages.jsonl")
        with open(log, encoding="utf-8") as f:
            msgs = [json.loads(l) for l in f if l.strip()]
        msgs[0]["body"] = "被篡改"                          # body_sha256 不再匹配
        with open(log, "w", encoding="utf-8", newline="\n") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        rc, out, err = self.read("--thread", "t-employee-4-leader")
        self.assertEqual(rc, 0)
        self.assertNotIn("被篡改", out)
        self.assertIn("body_sha256 不符", err)


class ThreadsListing(Base):
    def test_threads_and_thread_dump(self):
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "甲")
        self.send("--from", "employee-4", "--to", "leader", "--kind", "CHAT", "--body", "乙")
        rc, out, err = self.read("--threads")
        self.assertEqual(rc, 0, err)
        self.assertIn("t-employee-4-leader", out)
        self.assertIn("消息=2", out)
        rc, out, err = self.read("--thread", "t-employee-4-leader")
        self.assertEqual(rc, 0, err)
        self.assertIn("#1", out)
        self.assertIn("#2", out)
        self.assertIn("甲", out)
        self.assertIn("乙", out)


class Cursor(Base):
    def test_unread_before_and_after_mark(self):
        for i in range(3):
            self.send("--from", "leader", "--to", "employee-4",
                      "--kind", "CHAT", "--body", "m%d" % (i + 1))
        rc, out, err = self.read("--unread", "4abf3d15")          # 无游标=全未读
        self.assertEqual(rc, 0, err)
        self.assertIn("未读=3/3  (游标=0", out)
        rc, _, err = self.read("--unread", "4abf3d15", "--mark")
        self.assertEqual(rc, 0, err)
        curs = os.path.join(self.root, "relay", "runtime", "cursors", "4abf3d15.json")
        self.assertTrue(os.path.exists(curs))
        with open(curs, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["threads"]["t-employee-4-leader"], 3)
        self.assertIn("updated_at", data)
        self.send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "m4")
        rc, out, err = self.read("--unread", "4abf3d15")
        self.assertEqual(rc, 0, err)
        self.assertIn("未读=1/4  (游标=3", out)                   # 推进后新到 1 条未读


class Presence(Base):
    def test_missing_presence_hint(self):
        rc, out, err = self.read("--presence")
        self.assertEqual(rc, 0, err)
        self.assertIn("暂无心跳数据", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
