#!/usr/bin/env python3
"""TASK-017 relay_push.py 单元测试（仅标准库；临时 --root + 线程内假 Kimi 服务器）。

运行：
    python -m unittest tests.test_relay_push -v
覆盖：载荷文本逐字格式、路由解析三级顺序（roles → presence → registry）、
服务器发现（RELAY_PUSH_BASE 覆盖 / instances 心跳新鲜度）、推送信封判定、
CLI 退出码（0/2/3/4）、dry-run 零接触、template 逐字节副本一致。
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_REPO = os.path.normpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(ROOT_REPO, "tools")
RELAY_PUSH = os.path.join(TOOLS, "relay_push.py")
sys.path.insert(0, TOOLS)
import relay_push  # noqa: E402

PY = sys.executable
SID_A = "sess_a1b2c3d4-0000-4000-8000-0123456789ab"
SID_B = "sess_a1b2c3d4-ffff-4000-8000-0123456789ab"   # 与 SID_A 同短8，不同完整 id
SID_C = "sess_deadbeef-2222-4000-8000-0123456789ab"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 假 Kimi 服务器 ----------

class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        srv = self.server
        srv.seen.append({"method": "GET", "path": self.path,
                         "auth": self.headers.get("Authorization")})
        if self.path == "/healthz":
            if srv.fail_healthz:
                self._reply(500, {"code": 1, "msg": "healthz boom"})
            else:
                self._reply(200, {"code": 0, "msg": "ok", "data": {}})
            return
        self._reply(404, {"code": 1, "msg": "not found"})

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        srv.seen.append({"method": "POST", "path": self.path,
                         "auth": self.headers.get("Authorization"), "raw": raw})
        if self.path.startswith("/api/v1/sessions/") and self.path.endswith("/prompts"):
            if srv.fail_prompts == "http500":
                self._reply(500, {"code": 1, "msg": "prompts boom"})
                return
            if srv.fail_prompts == "code":
                self._reply(200, {"code": 7, "msg": "session busy"})
                return
            self._reply(200, {"code": 0, "msg": "ok", "data": {"request_id": "r-test"}})
            return
        self._reply(404, {"code": 1, "msg": "not found"})


def make_server():
    srv = HTTPServer(("127.0.0.1", 0), FakeHandler)
    srv.seen = []
    srv.fail_healthz = False
    srv.fail_prompts = ""
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def base_of(srv):
    return "http://127.0.0.1:%d" % srv.server_address[1]


def dead_base():
    """拿到一个刚释放、大概率无监听的本机端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


# ---------- 夹具写入 ----------

def write_roles(root, leader_sid=None, employees=None):
    path = os.path.join(root, "relay", "runtime", "roles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"version": 1,
            "leader": {"label": "leader", "session_id": leader_sid, "short": None, "model": None},
            "employees": {}}
    for label, sid in (employees or {}).items():
        data["employees"][label] = {"session_id": sid, "short": (sid or "")[5:13] or None}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_presence(root, sessions):
    path = os.path.join(root, "relay", "runtime", "presence.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"sessions": {}, "updated_at": "2026-09-19T00:00:00Z"}
    for sid, short in sessions.items():
        data["sessions"][sid] = {"short": short, "model": "k-test",
                                 "last_seen": "2026-09-19T00:00:00Z", "cwd": root}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_registry(root, sids):
    path = os.path.join(root, "relay", "runtime", "session-registry.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for sid in sids:
            f.write(json.dumps({"ts": "2026-09-19T00:00:00", "session_id": sid,
                                "cwd": root, "model": "k-test"}, ensure_ascii=False) + "\n")


def write_instance(dirpath, port, age_seconds=0):
    os.makedirs(dirpath, exist_ok=True)
    hb = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(dirpath, "inst.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"host": "127.0.0.1", "port": port, "heartbeat_at": hb}, f)
        f.write("\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-push-test-")
        self.srv = make_server()
        self._env = {}
        for k in ("RELAY_PUSH_BASE", "RELAY_PUSH_TOKEN", "RELAY_PUSH_INSTANCES_DIR"):
            self._env[k] = os.environ.get(k)
        os.environ["RELAY_PUSH_BASE"] = base_of(self.srv)
        os.environ["RELAY_PUSH_TOKEN"] = "test-token"
        os.environ.pop("RELAY_PUSH_INSTANCES_DIR", None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def push(self, to, kind="NOTICE", body="正文", **kw):
        return relay_push.push_text(self.root, to, kind, body, sender="leader", **kw)

    def posts(self):
        return [r for r in self.srv.seen if r["method"] == "POST"]

    def run_cli(self, *extra, env=None):
        e = dict(os.environ)
        e.update(env or {})
        e["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([PY, RELAY_PUSH, "--root", self.root] + list(extra),
                              capture_output=True, timeout=30, env=e)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))


class PayloadFormat(Base):
    def test_format_exact_lines(self):
        body = "第一行\n第二行"
        got = relay_push.format_payload("leader", "employee-4", "NOTICE", body,
                                        "TASK-017", "t-employee-4-leader",
                                        "20260919T120000Z-a1b2c3")
        expected = ("\n".join([
            "【relay-push】",
            "from: leader",
            "to: employee-4",
            "kind: NOTICE",
            "ref: TASK-017",
            "thread: t-employee-4-leader",
            "msg: 20260919T120000Z-a1b2c3",
            "body-sha256: " + sha256(body),
            "---body---",
        ]) + "\n" + body)
        self.assertEqual(got, expected)

    def test_empty_ref_and_multiline_body_verbatim(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        body = "收尾不带换行\n中间空行\n\n含中文标点。"
        ok, detail = self.push("employee-4", body=body, ref="", dry_run=True)
        self.assertTrue(ok, detail)
        self.assertIn("ref: \n", detail)
        self.assertTrue(detail.endswith(body))          # body 原文逐字在末尾
        self.assertNotIn("thread: \n", detail)          # thread 缺省已推导

    def test_dry_run_returns_payload_and_skips_http(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4", dry_run=True)
        self.assertTrue(ok, detail)
        self.assertTrue(detail.startswith("【relay-push】\nfrom: leader\n"))
        self.assertIn("body-sha256: " + sha256("正文"), detail)
        self.assertEqual(self.srv.seen, [])             # 零接触服务器


class Resolve(Base):
    def test_roles_resolution(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        sid, src = relay_push.resolve_route(self.root, "employee-4")
        self.assertEqual((sid, src), (SID_A, "roles.json"))

    def test_roles_win_over_presence_same_short(self):
        # 同短8：角色名寻址必须解析到 roles 绑定的 SID_A，而非 presence 的 SID_B
        write_roles(self.root, employees={"employee-4": SID_A})
        write_presence(self.root, {SID_B: "a1b2c3d4"})
        ok, detail = self.push("employee-4")
        self.assertTrue(ok, detail)
        self.assertEqual(self.posts()[0]["path"],
                         "/api/v1/sessions/%s/prompts" % SID_A)

    def test_role_unbound_falls_through_to_presence(self):
        write_roles(self.root, employees={"employee-4": None})
        write_presence(self.root, {SID_B: "a1b2c3d4"})
        sid, src = relay_push.resolve_route(self.root, "sess:a1b2c3d4")
        self.assertEqual((sid, src), (SID_B, "presence.json"))

    def test_registry_bare_full_session_id(self):
        write_registry(self.root, [SID_C])
        sid, src = relay_push.resolve_route(self.root, SID_C)
        self.assertEqual((sid, src), (SID_C, "session-registry.jsonl"))

    def test_registry_short8_prefix(self):
        write_registry(self.root, [SID_C])
        sid, src = relay_push.resolve_route(self.root, "sess:deadbeef")
        self.assertEqual((sid, src), (SID_C, "session-registry.jsonl"))

    def test_no_route(self):
        write_roles(self.root)  # 全未绑定
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-route:"), detail)


class PushHttp(Base):
    def test_push_ok_captured_envelope(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4", kind="DISPATCH", body="中文正文", ref="TASK-017")
        self.assertTrue(ok, detail)
        self.assertTrue(detail.startswith("msg="))
        posts = self.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], "/api/v1/sessions/%s/prompts" % SID_A)
        self.assertEqual(posts[0]["auth"], "Bearer test-token")
        body = json.loads(posts[0]["raw"].decode("utf-8"))
        self.assertEqual(body["content"][0]["type"], "text")
        msg_id = detail[len("msg="):]
        expected = relay_push.format_payload(
            "leader", "employee-4", "DISPATCH", "中文正文", "TASK-017",
            relay_push.derive_thread_id("leader", "employee-4"), msg_id)
        self.assertEqual(body["content"][0]["text"], expected)   # 载荷逐字一致

    def test_healthz_and_prompts_both_called(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertTrue(ok, detail)
        methods = [r["method"] for r in self.srv.seen]
        self.assertEqual(methods, ["GET", "POST"])   # 先 healthz 验证再推送

    def test_token_env_override(self):
        os.environ["RELAY_PUSH_TOKEN"] = "tok-2"
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertTrue(ok, detail)
        self.assertEqual(self.posts()[0]["auth"], "Bearer tok-2")

    def test_push_envelope_code_nonzero(self):
        self.srv.fail_prompts = "code"
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertIn("push:code=7", detail)

    def test_push_http_500(self):
        self.srv.fail_prompts = "http500"
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("http:"), detail)

    def test_no_server_connection_refused(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        os.environ["RELAY_PUSH_BASE"] = dead_base()
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-server:"), detail)

    def test_healthz_fail_is_no_server(self):
        self.srv.fail_healthz = True
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-server:"), detail)

    def test_guards_before_route(self):
        ok, detail = relay_push.push_text(self.root, "nobody", "SHOUT", "x", sender="leader")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-kind:"), detail)
        ok, detail = relay_push.push_text(self.root, "nobody", "CHAT", "x" * 4001, sender="leader")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-body:"), detail)
        ok, detail = self.push("employee-4", thread_id="bad..thread", dry_run=True)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-thread:"), detail)


class Discovery(Base):
    def test_instances_discovery_fresh(self):
        os.environ.pop("RELAY_PUSH_BASE", None)
        inst_dir = os.path.join(self.root, "instances")
        write_instance(inst_dir, self.srv.server_address[1], age_seconds=0)
        os.environ["RELAY_PUSH_INSTANCES_DIR"] = inst_dir
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertTrue(ok, detail)
        self.assertEqual(self.posts()[0]["path"],
                         "/api/v1/sessions/%s/prompts" % SID_A)

    def test_instances_stale_heartbeat_no_server(self):
        os.environ.pop("RELAY_PUSH_BASE", None)
        inst_dir = os.path.join(self.root, "instances")
        write_instance(inst_dir, self.srv.server_address[1], age_seconds=1000)
        os.environ["RELAY_PUSH_INSTANCES_DIR"] = inst_dir
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-server:"), detail)
        self.assertEqual(self.srv.seen, [])          # 未联系任何服务器

    def test_instances_dir_missing_no_server(self):
        os.environ.pop("RELAY_PUSH_BASE", None)
        os.environ["RELAY_PUSH_INSTANCES_DIR"] = os.path.join(self.root, "no-such-dir")
        write_roles(self.root, employees={"employee-4": SID_A})
        ok, detail = self.push("employee-4")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-server:"), detail)

    def test_parse_epoch_variants(self):
        self.assertEqual(relay_push._parse_epoch("1700000000"), 1700000000.0)
        self.assertEqual(relay_push._parse_epoch(1700000000), 1700000000.0)
        self.assertEqual(relay_push._parse_epoch("2026-09-19T12:00:00Z"),
                         datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNotNone(relay_push._parse_epoch("2026-09-19T12:00:00"))  # naive 本地 ISO
        self.assertIsNone(relay_push._parse_epoch("garbage"))


class Cli(Base):
    def test_cli_ok(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "NOTICE", "--body", "CLI 推送",
                                    "--ref", "TASK-017", "--in-reply-to", "m-1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("OK msg="), out)
        self.assertIn("push=ok", out)

    def test_cli_dry_run_prints_payload_no_contact(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "CHAT", "--body", "排练", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("【relay-push】\nfrom: leader\n"), out)
        self.assertIn("body-sha256: " + sha256("排练"), out)
        self.assertEqual(self.srv.seen, [])

    def test_cli_no_route_exit3(self):
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "NOTICE", "--body", "x")
        self.assertEqual(rc, 3)
        self.assertIn("no-route:", err)

    def test_cli_no_server_exit3(self):
        write_roles(self.root, employees={"employee-4": SID_A})
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "NOTICE", "--body", "x",
                                    env={"RELAY_PUSH_BASE": dead_base()})
        self.assertEqual(rc, 3)
        self.assertIn("no-server:", err)

    def test_cli_bad_kind_exit2(self):
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "SHOUT", "--body", "x")
        self.assertEqual(rc, 2)
        self.assertIn("bad-kind:", err)

    def test_cli_bad_body_exit2(self):
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "CHAT", "--body", "x" * 4001)
        self.assertEqual(rc, 2)
        self.assertIn("bad-body:", err)

    def test_cli_bad_thread_exit2(self):
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "CHAT", "--body", "x", "--thread", "a..b")
        self.assertEqual(rc, 2)
        self.assertIn("bad-thread:", err)

    def test_cli_push_code_fail_exit4(self):
        self.srv.fail_prompts = "code"
        write_roles(self.root, employees={"employee-4": SID_A})
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "NOTICE", "--body", "x")
        self.assertEqual(rc, 4)
        self.assertIn("push:code=7", err)

    def test_cli_push_http500_exit4(self):
        self.srv.fail_prompts = "http500"
        write_roles(self.root, employees={"employee-4": SID_A})
        rc, out, err = self.run_cli("--from", "leader", "--to", "employee-4",
                                    "--kind", "NOTICE", "--body", "x")
        self.assertEqual(rc, 4)
        self.assertIn("http:status=500", err)


class TemplateSync(unittest.TestCase):
    def test_template_copy_byte_identical(self):
        with open(os.path.join(ROOT_REPO, "tools", "relay_push.py"), "rb") as f:
            a = f.read()
        with open(os.path.join(ROOT_REPO, "template", "tools", "relay_push.py"), "rb") as f:
            b = f.read()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
