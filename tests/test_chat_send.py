#!/usr/bin/env python3
"""TASK-011 chat_send.py 单元测试（仅标准库；子进程调用 + 临时 --root，不触生产 relay/）。

运行：
    python tests/task-010/test_chat_send.py
覆盖 TASK-011-Q01 卡要求：seq 递增（同收件人连发两条 seq=1,2）、非法 kind exit 2、
body 超 4000 字 exit 2、--to sess:xxxx 目录规范化为 to-sess-xxxx、UTF-8 中文正文写读一致。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_REPO = os.path.normpath(os.path.join(HERE, ".."))
CHAT_SEND = os.path.join(ROOT_REPO, "tools", "chat_send.py")
PY = sys.executable


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-chatsend-test-")

    def run_send(self, *extra):
        proc = subprocess.run(
            [PY, CHAT_SEND, "--root", self.root] + list(extra),
            capture_output=True, timeout=30)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def pend_dir(self, to_dir):
        return os.path.join(self.root, "relay", "chat", to_dir, "pending")

    def read_msg(self, to_dir):
        pend = self.pend_dir(to_dir)
        names = sorted(os.listdir(pend))
        self.assertEqual(len(names), 1)
        with open(os.path.join(pend, names[0]), encoding="utf-8") as f:
            raw = f.read()
        return names[0], raw, json.loads(raw)


class Seq(Base):
    def test_seq_increments_same_recipient(self):
        rc1, _, err1 = self.run_send("--from", "1111aaaa", "--to", "employee-4",
                                     "--kind", "NOTICE", "--body", "第一条")
        self.assertEqual(rc1, 0, err1)
        rc2, _, err2 = self.run_send("--from", "1111aaaa", "--to", "employee-4",
                                     "--kind", "NOTICE", "--body", "第二条")
        self.assertEqual(rc2, 0, err2)
        pend = self.pend_dir("to-employee-4")
        names = sorted(os.listdir(pend))
        self.assertEqual([n[:4] for n in names], ["0001", "0002"])
        seqs = []
        for n in names:
            with open(os.path.join(pend, n), encoding="utf-8") as f:
                seqs.append(json.load(f)["seq"])
        self.assertEqual(seqs, [1, 2])


class Guards(Base):
    def test_bad_kind_exit2_no_file(self):
        rc, _, err = self.run_send("--from", "1111aaaa", "--to", "employee-4",
                                   "--kind", "SHOUT", "--body", "x")
        self.assertEqual(rc, 2)
        self.assertIn("kind", err)
        self.assertFalse(os.path.isdir(self.pend_dir("to-employee-4")))

    def test_body_over_4000_exit2_no_file(self):
        rc, _, err = self.run_send("--from", "1111aaaa", "--to", "employee-4",
                                   "--kind", "CHAT", "--body", "x" * 4001)
        self.assertEqual(rc, 2)
        self.assertIn("4000", err)
        self.assertFalse(os.path.isdir(self.pend_dir("to-employee-4")))


class Addressing(Base):
    def test_sess_to_normalized_to_dir(self):
        rc, _, err = self.run_send("--from", "leader", "--to", "sess:abcd1234",
                                   "--kind", "DISPATCH", "--body", "定向消息")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(self.pend_dir("to-sess-abcd1234")))
        self.assertFalse(os.path.isdir(self.pend_dir("sess:abcd1234")))
        _, _, msg = self.read_msg("to-sess-abcd1234")
        self.assertEqual(msg["to"], "sess:abcd1234")   # 目录规范化，消息字段保留原值


class Encoding(Base):
    def test_utf8_chinese_roundtrip(self):
        body = "中文正文：员工自举完成，写读一致（含全角标点）。"
        rc, out, err = self.run_send("--from", "1111aaaa", "--to", "employee-4",
                                     "--kind", "NOTICE", "--body", body)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("OK "))
        name, raw, msg = self.read_msg("to-employee-4")
        self.assertIn("员工自举完成", raw)              # ensure_ascii=False，中文原样落盘
        self.assertEqual(msg["body"], body)
        self.assertEqual(msg["body_sha256"], sha256(body))
        self.assertEqual(msg["kind"], "NOTICE")


# ---- TASK-016A v2.0 线程层 ----

V1_FIELDS = ["version", "msg_id", "seq", "from", "to", "kind", "body",
             "ref", "created_at", "body_sha256"]
NEW_FIELDS = ["thread_id", "in_reply_to", "thread_seq"]


def read_thread(root, thread_id):
    import json as _json
    path = os.path.join(root, "relay", "chat", "threads", thread_id, "messages.jsonl")
    with open(path, "rb") as f:
        return [_json.loads(l.decode("utf-8")) for l in f if l.strip()]


class ThreadDualWrite(Base):
    def test_dual_write_fields_consistent(self):
        rc, out, err = self.run_send("--from", "leader", "--to", "employee-4",
                                     "--kind", "CHAT", "--body", "双写一致",
                                     "--ref", "TASK-016A")
        self.assertEqual(rc, 0, err)
        _, _, msg = self.read_msg("to-employee-4")
        msgs = read_thread(self.root, "t-employee-4-leader")
        self.assertEqual(len(msgs), 1)
        for k in V1_FIELDS + NEW_FIELDS:
            self.assertEqual(msg[k], msgs[0][k], k)
        # 推导 thread_id：两地址字典序
        self.assertEqual(msg["thread_id"], "t-employee-4-leader")
        self.assertEqual(msgs[0]["thread_seq"], 1)
        self.assertEqual(msgs[0]["in_reply_to"], "")
        # 输出行格式 OK <msg_id> <thread>#<seq> <pending路径>
        self.assertIn("%s t-employee-4-leader#1 " % msg["msg_id"], out)


class ThreadSeq(Base):
    def test_seq_monotonic_123(self):
        for i in range(3):
            rc, out, err = self.run_send("--from", "leader", "--to", "employee-4",
                                         "--kind", "CHAT", "--body", "第%d条" % (i + 1))
            self.assertEqual(rc, 0, err)
            self.assertIn("#%d " % (i + 1), out)
        seqs = [m["thread_seq"] for m in read_thread(self.root, "t-employee-4-leader")]
        self.assertEqual(seqs, [1, 2, 3])

    def test_explicit_thread_matches_derivation(self):
        self.run_send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "a")
        self.run_send("--from", "leader", "--to", "employee-4", "--kind", "CHAT", "--body", "b",
                      "--thread", "t-employee-4-leader")
        msgs = read_thread(self.root, "t-employee-4-leader")
        self.assertEqual([m["thread_seq"] for m in msgs], [1, 2])   # 显式与推导同线程
        self.assertEqual([m["body"] for m in msgs], ["a", "b"])

    def test_in_reply_to_passthrough(self):
        rc, out, _ = self.run_send("--from", "leader", "--to", "employee-4",
                                   "--kind", "CHAT", "--body", "回复",
                                   "--in-reply-to", "20260919T000000Z-dead00")
        self.assertEqual(rc, 0)
        msgs = read_thread(self.root, "t-employee-4-leader")
        self.assertEqual(msgs[0]["in_reply_to"], "20260919T000000Z-dead00")


class V1Compat(Base):
    def test_existing_v1_pending_does_not_break_send(self):
        # 预置一条不含新字段的旧 v1 pending 消息；v2 发送照常投递且邮箱 seq 续排
        pend = self.pend_dir("to-employee-4")
        os.makedirs(pend)
        old = {"version": "1.0", "msg_id": "20260918T000000Z-000000", "seq": 1,
               "from": "leader", "to": "employee-4", "kind": "ACK", "body": "旧消息",
               "ref": "", "created_at": "2026-09-18T00:00:00Z", "body_sha256": sha256("旧消息")}
        with open(os.path.join(pend, "0001-20260918T000000Z-000000.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
            f.write("\n")
        rc, _, err = self.run_send("--from", "leader", "--to", "employee-4",
                                   "--kind", "CHAT", "--body", "新消息")
        self.assertEqual(rc, 0, err)
        names = sorted(os.listdir(pend))
        self.assertEqual([n[:4] for n in names], ["0001", "0002"])
        with open(os.path.join(pend, names[1]), encoding="utf-8") as f:
            new = json.load(f)
        self.assertEqual(new["seq"], 2)                    # 邮箱 seq 续排
        self.assertNotIn("thread_id", old)                 # 旧消息无新字段仍完好


class ConcurrentAppend(Base):
    def test_two_senders_10_lines_no_interleave_no_dup_seq(self):
        procs = []
        for i in range(5):
            procs.append(subprocess.Popen(
                [PY, CHAT_SEND, "--root", self.root, "--from", "leader", "--to", "employee-4",
                 "--kind", "CHAT", "--body", "leader-%d" % i],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            procs.append(subprocess.Popen(
                [PY, CHAT_SEND, "--root", self.root, "--from", "employee-4", "--to", "leader",
                 "--kind", "CHAT", "--body", "employee-%d" % i],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        # 两方推导出同一个 thread_id（地址排序一致）
        raw = open(os.path.join(self.root, "relay", "chat", "threads",
                                "t-employee-4-leader", "messages.jsonl"), "rb").read()
        self.assertTrue(raw.endswith(b"\n"))               # 无交错半行
        msgs = [json.loads(l.decode("utf-8")) for l in raw.split(b"\n") if l.strip()]
        self.assertEqual(len(msgs), 10)
        seqs = sorted(m["thread_seq"] for m in msgs)
        self.assertEqual(seqs, list(range(1, 11)))         # 无重复


if __name__ == "__main__":
    unittest.main(verbosity=2)
