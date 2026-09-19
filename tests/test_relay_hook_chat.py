#!/usr/bin/env python3
"""relay_hook 单元测试（v2/v3 回归 + TASK-016B v4 游标注入/presence；仅标准库；合成 stdin + 临时 RELAY_HOOK_ROOT，不触生产 runtime）。

运行：
    python tests/test_relay_hook_chat.py          （暂存包：hooks/relay_hook.py + tools/chat_send.py）
    python tests/task-010/test_relay_hook_chat.py （本体：tools/task-010/ 内同套文件，路径自动探测）
stdin 合同依据 reports/task-010-zcode-relay/EVENT-CHAIN.md（VERIFIED_LOCAL）：
公共字段双命名（snake_case 与 camelCase 同值），hook 以 snake_case 为规范、camelCase 兜底。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _repo_root():
    """向上探测仓库根（暂存包 tests/ 一层深；本体 tests/task-010/ 两层深）。"""
    d = HERE
    for _ in range(4):
        for rel in ("hooks", os.path.join("tools", "task-010")):
            if os.path.isfile(os.path.join(d, rel, "relay_hook.py")):
                return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return os.path.normpath(os.path.join(HERE, ".."))


def _tool(name, rels=(os.path.join("tools", "task-010"), "tools")):
    d = _repo_root()
    for rel in rels:
        cand = os.path.join(d, rel, name)
        if os.path.isfile(cand):
            return cand
    return os.path.join(d, rels[-1], name)


ROOT_REPO = _repo_root()
HOOK = _tool("relay_hook.py", ("hooks", os.path.join("tools", "task-010")))
CHAT_SEND = _tool("chat_send.py")
PY = sys.executable

EMP = "sess_1111aaaa-0000-0000-0000-000000000000"   # 短标识 1111aaaa
EMP2 = "sess_2222bbbb-0000-0000-0000-000000000000"  # 短标识 2222bbbb
NEWB = "sess_9999ffff-0000-0000-0000-000000000000"  # 短标识 9999ffff（重绑测试的新窗口）
LEADER = "sess_9999bbbb-0000-0000-0000-000000000000"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-hook-test-")
        for sub in ("inbox", "claimed", "outbox", "runtime", "chat"):
            os.makedirs(os.path.join(self.root, "relay", sub), exist_ok=True)

    def run_hook(self, event, sid=EMP, turn="t1", extra=None, cwd=None, raw=None, use_env_root=True, hook_env=None):
        payload = {
            "session_id": sid, "sessionId": sid,
            "turn_id": turn, "turnId": turn,
            "cwd": cwd or self.root,
        }
        payload.update(extra or {})
        env = dict(os.environ)
        if use_env_root:
            env["RELAY_HOOK_ROOT"] = self.root
        else:
            env.pop("RELAY_HOOK_ROOT", None)
        if hook_env:
            env.update(hook_env)
        proc = subprocess.run(
            [PY, HOOK, event],
            input=raw if raw is not None else json.dumps(payload).encode("utf-8"),
            capture_output=True, env=env, timeout=30)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def write_card(self, task_id="TASK-X01", worker="any", prompt="do the thing"):
        card = {"version": "1.1", "task_id": task_id, "worker": worker,
                "prompt": prompt, "prompt_sha256": sha256(prompt)}
        path = os.path.join(self.root, "relay", "inbox", task_id + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False)
        return path

    def write_chat(self, dirname, body="hi", msg_id="m1", good_sha=True, kind="NOTICE", frm="leader"):
        pend = os.path.join(self.root, "relay", "chat", dirname, "pending")
        os.makedirs(pend, exist_ok=True)
        msg = {"version": "1.0", "msg_id": msg_id, "seq": 1, "from": frm,
               "to": dirname, "kind": kind, "body": body, "ref": "", "created_at": "2026-09-18T00:00:00Z",
               "body_sha256": sha256(body) if good_sha else "0" * 64}
        path = os.path.join(pend, "0001-%s.json" % msg_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False)
        return path

    def claimed_files(self):
        return os.listdir(os.path.join(self.root, "relay", "claimed"))

    def chat_read(self, dirname):
        d = os.path.join(self.root, "relay", "chat", dirname, "read")
        return os.listdir(d) if os.path.isdir(d) else []


class StopCardAndChat(Base):
    def test_card_any_claim_and_inject(self):
        self.write_card("TASK-C1")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.claimed_files()), 1)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("TASK-C1", data["reason"])

    def test_worker_zcode_claimable(self):  # 保留 v3 行为
        self.write_card("TASK-Z9", worker="zcode")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.claimed_files()), 1)
        self.assertIn("TASK-Z9", out)

    def test_chat_short_dir_claim_and_inject(self):
        self.write_chat("to-sess-1111aaaa", body="员工你好")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        self.assertIn("【relay 消息】", out)
        self.assertIn("员工你好", out)
        self.assertEqual(len(self.chat_read("to-sess-1111aaaa")), 1)

    def test_card_and_chat_merged_single_output(self):
        self.write_card("TASK-M1")
        self.write_chat("to-sess-1111aaaa", body="顺带消息")
        rc, out = self.run_hook("Stop")
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("【relay 自动续接】", data["reason"])
        self.assertIn("【relay 消息】", data["reason"])
        self.assertEqual(len(self.claimed_files()), 1)
        self.assertEqual(len(self.chat_read("to-sess-1111aaaa")), 1)

    def test_leader_gets_chat_not_card(self):
        with open(os.path.join(self.root, "relay", "runtime", "leader-sessions.txt"), "w", encoding="utf-8") as f:
            f.write(LEADER + "\n")
        self.write_card("TASK-L1")
        self.write_chat("to-leader", body="领导请审阅")
        rc, out = self.run_hook("Stop", sid=LEADER)
        self.assertEqual(rc, 0)
        self.assertEqual(self.claimed_files(), [])                       # 未领卡
        self.assertIn("TASK-L1.json", os.listdir(os.path.join(self.root, "relay", "inbox")))
        self.assertIn("【relay 消息】", out)
        self.assertEqual(len(self.chat_read("to-leader")), 1)

    def test_role_dir_via_roles_json(self):
        with open(os.path.join(self.root, "relay", "runtime", "roles.json"), "w", encoding="utf-8") as f:
            json.dump({"employees": {"employee-4": {"short": "1111aaaa"}}, "leader": {}}, f)
        self.write_chat("to-employee-4", body="按角色收")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        self.assertIn("按角色收", out)

    def test_other_session_chat_not_taken(self):
        self.write_chat("to-sess-7777cccc", body="别人的")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")                                 # 无本会话内容 → 不注入
        pend = os.path.join(self.root, "relay", "chat", "to-sess-7777cccc", "pending")
        self.assertEqual(len(os.listdir(pend)), 1)                       # 原地不动


class Guards(Base):
    def test_same_turn_dedup(self):
        self.write_card("TASK-D1")
        self.run_hook("Stop", turn="t1")
        self.write_card("TASK-D2")                                       # 同 turn 再来一张
        rc, out = self.run_hook("Stop", turn="t1")
        self.assertEqual(out.strip(), "")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "relay", "inbox"))), ["TASK-D2.json"])

    def test_max_chain_blocks(self):
        st_dir = os.path.join(self.root, "relay", "runtime")
        state = "chain-state-%s.json" % EMP[:13]
        with open(os.path.join(st_dir, state), "w", encoding="utf-8") as f:
            json.dump({"count": 3, "last_turn": "t0"}, f)
        self.write_card("TASK-K1")
        rc, out = self.run_hook("Stop", turn="t9", extra={"stopHookActive": True, "stop_hook_active": True})
        self.assertEqual(out.strip(), "")
        self.assertEqual(self.claimed_files(), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "relay", "inbox"))), ["TASK-K1.json"])

    def test_chat_bad_sha_quarantined(self):
        self.write_chat("to-sess-1111aaaa", body="损坏消息", good_sha=False)
        rc, out = self.run_hook("Stop")
        self.assertEqual(out.strip(), "")
        read = self.chat_read("to-sess-1111aaaa")
        self.assertEqual(read, ["0001-m1.json.bad"])                     # 隔离为 .bad
        with open(os.path.join(self.root, "relay", "runtime", "chain-log.jsonl"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("chat-quarantine", log)

    def test_garbage_stdin_failopen(self):
        rc, out = self.run_hook("Stop", raw=b"not-json{")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_out_of_scope_cwd_ignored(self):
        self.write_card("TASK-S1")
        rc, out = self.run_hook("Stop", cwd=os.path.join(tempfile.gettempdir(), "elsewhere"))
        self.assertEqual(out.strip(), "")
        self.assertEqual(self.claimed_files(), [])

    def test_camel_only_naming(self):
        payload = {"sessionId": EMP, "turnId": "t1", "cwd": self.root}
        env = dict(os.environ)
        env["RELAY_HOOK_ROOT"] = self.root
        self.write_card("TASK-N1")
        proc = subprocess.run([PY, HOOK, "Stop"], input=json.dumps(payload).encode(),
                              capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("TASK-N1", proc.stdout.decode("utf-8", "replace"))
        self.assertEqual(len(self.claimed_files()), 1)


class Ups(Base):
    def test_flag_off_silent(self):
        self.write_chat("to-sess-1111aaaa", body="门控关")
        rc, out = self.run_hook("UserPromptSubmit", extra={"prompt": "用户消息"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_flag_on_injects_additional_context(self):
        open(os.path.join(self.root, "relay", "runtime", "ups-context-enabled"), "w").close()
        self.write_chat("to-sess-1111aaaa", body="门控开", kind="DISPATCH")
        rc, out = self.run_hook("UserPromptSubmit", extra={"prompt": "用户消息"})
        self.assertEqual(rc, 0)
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("【relay 消息】", ctx)
        self.assertIn("门控开", ctx)
        self.assertEqual(len(self.chat_read("to-sess-1111aaaa")), 1)


class V3MarkerRouting(Base):
    """v3：relay/relay.enabled 标记路由（多项目零配置激活）。"""

    def write_marker(self):
        with open(os.path.join(self.root, "relay", "relay.enabled"), "w", encoding="utf-8") as f:
            f.write("testproj 2026-09-18\n")

    def test_marker_dir_activates_without_env(self):
        self.write_marker()
        self.write_card("TASK-V3A")
        rc, out = self.run_hook("Stop", use_env_root=False)   # cwd=self.root 且有标记
        self.assertEqual(rc, 0)
        self.assertIn("TASK-V3A", out)
        self.assertEqual(len(self.claimed_files()), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "relay", "runtime", "chain-log.jsonl")))

    def test_no_marker_no_env_silent(self):
        self.write_card("TASK-V3B")                            # 无标记、无环境变量
        rc, out = self.run_hook("Stop", use_env_root=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
        self.assertEqual(self.claimed_files(), [])
        self.assertEqual(os.listdir(os.path.join(self.root, "relay", "runtime")), [])

    def test_marker_wins_over_env(self):
        self.write_marker()
        other = tempfile.mkdtemp(prefix="relay-hook-other-")   # 环境变量指向别处，标记应胜出
        self.write_card("TASK-V3C")
        env = dict(os.environ)
        env["RELAY_HOOK_ROOT"] = other
        payload = {"session_id": EMP, "sessionId": EMP, "turn_id": "t1", "turnId": "t1", "cwd": self.root}
        proc = subprocess.run([PY, HOOK, "Stop"], input=json.dumps(payload).encode(),
                              capture_output=True, env=env, timeout=30)
        self.assertIn("TASK-V3C", proc.stdout.decode("utf-8", "replace"))
        self.assertEqual(len(self.claimed_files()), 1)


class ChatSendRoundtrip(Base):
    def test_send_then_hook_claims(self):
        env = dict(os.environ)
        proc = subprocess.run(
            [PY, CHAT_SEND, "--from", "leader", "--to", "sess:1111aaaa",
             "--kind", "NOTICE", "--body", "回环消息", "--root", self.root],
            capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        pend = os.path.join(self.root, "relay", "chat", "to-sess-1111aaaa", "pending")
        self.assertEqual(len(os.listdir(pend)), 1)
        rc, out = self.run_hook("Stop")
        self.assertIn("回环消息", out)
        self.assertEqual(len(self.chat_read("to-sess-1111aaaa")), 1)

    def test_send_rejects_bad_kind(self):
        proc = subprocess.run(
            [PY, CHAT_SEND, "--from", "leader", "--to", "employee-4",
             "--kind", "SHOUT", "--body", "x", "--root", self.root],
            capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


class V4UnreadInjection(Base):
    """TASK-016B relay_hook v4：游标式未读注入 + presence 心跳。"""

    def register(self, sid, model="GLM-5.3-test"):
        rc, out = self.run_hook("SessionStart", sid=sid, extra={"model": model})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def send(self, frm, to, body, kind="NOTICE"):
        proc = subprocess.run(
            [PY, CHAT_SEND, "--from", frm, "--to", to, "--kind", kind,
             "--body", body, "--root", self.root],
            capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8", "replace").split()[1]   # OK <msg_id> ...

    def cursor(self, short):
        path = os.path.join(self.root, "relay", "runtime", "cursors", short + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("threads", {})
        except (OSError, ValueError):
            return {}

    def presence(self):
        with open(os.path.join(self.root, "relay", "runtime", "presence.json"), encoding="utf-8") as f:
            return json.load(f).get("sessions", {})

    def test_thread_unread_injected_and_cursor_advanced(self):
        self.register(EMP)
        msg_id = self.send("leader", "sess:1111aaaa", "v2 即达消息")
        rc, out = self.run_hook("Stop")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("【relay 消息】", out)
        self.assertIn("v2 即达消息", out)
        self.assertIn("t-1111aaaa-leader", out)
        self.assertIn("seq=1", out)
        self.assertIn("kind=NOTICE", out)
        self.assertEqual(out.count(msg_id), 1)                     # v1/v2 双现合并为一条
        self.assertEqual(self.cursor("1111aaaa").get("t-1111aaaa-leader"), 1)
        self.assertEqual(len(self.chat_read("to-sess-1111aaaa")), 1)

    def test_three_session_isolation(self):
        self.register(EMP)
        self.register(EMP2)
        self.register(LEADER)
        time.sleep(1.3)   # 消息时间戳为秒级截断：保证发送秒 ≥ 注册秒
        with open(os.path.join(self.root, "relay", "runtime", "roles.json"), "w", encoding="utf-8") as f:
            json.dump({"leader": {"session_id": LEADER, "short": "9999bbbb"},
                       "employees": {"employee-8": {"session_id": EMP, "short": "1111aaaa"}}}, f)
        self.send("leader", "sess:1111aaaa", "给一号窗")
        self.send("leader", "sess:2222bbbb", "给二号窗")
        self.send("1111aaaa", "leader", "呈报领导")
        rc, o1 = self.run_hook("Stop", sid=EMP)
        self.assertIn("给一号窗", o1)
        self.assertNotIn("给二号窗", o1)
        self.assertNotIn("呈报领导", o1)
        rc, o2 = self.run_hook("Stop", sid=EMP2)
        self.assertIn("给二号窗", o2)
        self.assertNotIn("给一号窗", o2)
        rc, o3 = self.run_hook("Stop", sid=LEADER)
        self.assertIn("呈报领导", o3)
        self.assertNotIn("给一号窗", o3)
        self.assertNotIn("给二号窗", o3)

    def test_own_sent_not_inbound(self):
        self.register(EMP)
        self.send("1111aaaa", "leader", "自发不算未读")
        rc, out = self.run_hook("Stop")
        self.assertEqual(out.strip(), "")                          # 自发消息不算入站
        pend = os.path.join(self.root, "relay", "chat", "to-leader", "pending")
        self.assertEqual(len(os.listdir(pend)), 1)                 # 他人邮箱原地不动

    def test_role_rebind_old_private_not_transferred(self):
        roles = os.path.join(self.root, "relay", "runtime", "roles.json")
        with open(roles, "w", encoding="utf-8") as f:
            json.dump({"leader": {}, "employees": {"employee-4": {"session_id": EMP, "short": "1111aaaa"}}}, f)
        self.register(EMP)
        self.send("leader", "employee-4", "旧私聊不移交")
        time.sleep(1.3)   # 消息时间戳为秒级截断：保证旧消息秒 < 新窗注册秒
        # 重绑：新窗口在消息之后注册（现实顺序=换窗→回填 roles）
        with open(roles, "w", encoding="utf-8") as f:
            json.dump({"leader": {}, "employees": {"employee-4": {"session_id": NEWB, "short": "9999ffff"}}}, f)
        self.register(NEWB)
        rc, out_new = self.run_hook("Stop", sid=NEWB)
        self.assertEqual(out_new.strip(), "")                      # 旧私聊未自动移交
        rc, out_old = self.run_hook("Stop", sid=EMP)
        self.assertEqual(out_old.strip(), "")                      # 旧窗口已不持有角色
        # 新窗口的直接寻址不受重绑影响
        self.send("leader", "sess:9999ffff", "新窗直发")
        rc, out_direct = self.run_hook("Stop", sid=NEWB)
        self.assertIn("新窗直发", out_direct)

    def test_budget_cut_leaves_tail_and_cursor_prefix(self):
        self.register(EMP)
        for i in range(1, 9):
            self.send("leader", "sess:1111aaaa", "预算消息%02d" % i)
        rc, out = self.run_hook("Stop", hook_env={"RELAY_UNREAD_MAX_MSGS": "5"})
        for n in range(1, 6):
            self.assertIn("(%d) thread=" % n, out)
        self.assertNotIn("(6) thread=", out)
        self.assertIn("另有 3 条未读未展开", out)                   # 剩余数正确
        self.assertEqual(self.cursor("1111aaaa").get("t-1111aaaa-leader"), 5)  # 只推进已注入前缀
        rc, out2 = self.run_hook("Stop", turn="t2")
        self.assertIn("预算消息08", out2)
        self.assertNotIn("另有", out2)
        self.assertEqual(self.cursor("1111aaaa").get("t-1111aaaa-leader"), 8)

    def test_crash_after_claim_before_output_redelivers(self):
        self.register(EMP)
        self.send("leader", "sess:1111aaaa", "崩溃重投消息")
        # 模拟"领取后、注入输出前崩溃"：pending 已被移入 read，游标未推进
        pend = os.path.join(self.root, "relay", "chat", "to-sess-1111aaaa", "pending")
        read = os.path.join(self.root, "relay", "chat", "to-sess-1111aaaa", "read")
        os.makedirs(read, exist_ok=True)
        for name in os.listdir(pend):
            os.rename(os.path.join(pend, name), os.path.join(read, name))
        rc, out = self.run_hook("Stop")
        self.assertIn("崩溃重投消息", out)                          # 从线程日志按游标重投
        self.assertEqual(self.cursor("1111aaaa").get("t-1111aaaa-leader"), 1)

    def test_v1_only_mailbox_message_still_injected(self):
        self.write_chat("to-sess-1111aaaa", body="纯v1消息", msg_id="m-v1only")
        rc, out = self.run_hook("Stop")
        self.assertIn("纯v1消息", out)
        self.assertIn("m-v1only", out)

    def test_ups_flag_off_presence_still_refreshed(self):
        self.register(EMP, model="GLM-5.3-主力")
        rc, out = self.run_hook("UserPromptSubmit", extra={"prompt": "用户消息"})  # 无门控标志
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")                          # 门控关：不注入
        ent = self.presence()[EMP]
        self.assertEqual(ent["short"], "1111aaaa")
        self.assertEqual(ent["model"], "GLM-5.3-主力")             # 无 model 事件不覆盖已知 model
        self.assertTrue(ent["last_seen"])

    def test_stop_ups_race_idempotent(self):
        open(os.path.join(self.root, "relay", "runtime", "ups-context-enabled"), "w").close()
        self.register(EMP)
        self.send("leader", "sess:1111aaaa", "竞争幂等")
        rc, up1 = self.run_hook("UserPromptSubmit", extra={"prompt": "x"})
        self.assertIn("竞争幂等", up1)
        rc, up2 = self.run_hook("UserPromptSubmit", extra={"prompt": "x"})        # 同轮再触发
        self.assertEqual(up2.strip(), "")                          # 游标已推进 → 幂等静默
        rc, st1 = self.run_hook("Stop")
        self.assertEqual(st1.strip(), "")                          # Stop 无新内容

    def test_unregistered_session_direct_message(self):
        # 无 registry/roles 的临时根：直接寻址消息仍可收（v1 兼容）
        self.send("leader", "sess:1111aaaa", "临时根直发")
        rc, out = self.run_hook("Stop")
        self.assertIn("临时根直发", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
