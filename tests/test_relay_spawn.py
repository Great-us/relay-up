#!/usr/bin/env python3
"""TASK-B1 relay_spawn.py 单元测试（仅标准库；临时 --root + 线程内假 Kimi 服务器）。

运行：
    python -m unittest tests.test_relay_spawn -v
覆盖：实测 spawn 协议调用序（建会话不带 agent_config → 两次 profile 分开补 →
读回校验 model → settle → roles.json 读-改-写回填）、roles.json 其他字段保留、
归档清绑定、approve 循环（status=pending 必填 query、decision/scope 载荷）、
CLI 退出码（0/2/3/4）、template 逐字节副本一致。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_REPO = os.path.normpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(ROOT_REPO, "tools")
RELAY_SPAWN = os.path.join(TOOLS, "relay_spawn.py")
sys.path.insert(0, TOOLS)
import relay_push  # noqa: E402,F401
import relay_spawn  # noqa: E402

PY = sys.executable
SID_A = "sess_a1b2c3d4-0000-4000-8000-0123456789ab"
MODEL_HI = "kimi-code/kimi-for-coding-highspeed"


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

    def _record(self, method, raw=b""):
        self.server.seen.append({"method": method, "path": self.path,
                                 "auth": self.headers.get("Authorization"), "raw": raw})

    def do_GET(self):
        srv = self.server
        self._record("GET")
        if self.path == "/healthz":
            self._reply(200, {"code": 0, "msg": "ok", "data": {}})
            return
        if self.path.startswith("/api/v1/sessions/") and self.path.endswith("/profile"):
            self._reply(200, {"code": 0, "msg": "ok", "data": srv.profile_resp})
            return
        if self.path.startswith("/api/v1/sessions/") and "/approvals" in self.path:
            if "status=pending" not in self.path:   # 实测缺该 query 报 40001
                self._reply(400, {"code": 40001, "msg": "status required"})
                return
            self._reply(200, {"code": 0, "msg": "ok", "data": srv.pending})
            return
        self._reply(404, {"code": 1, "msg": "not found"})

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self._record("POST", raw)
        if self.path == "/api/v1/sessions":
            if srv.fail_create:
                self._reply(200, {"code": 7, "msg": "create boom"})
                return
            self._reply(200, {"code": 0, "msg": "ok", "data": {"id": SID_A}})
            return
        if self.path.startswith("/api/v1/sessions/") and self.path.endswith("/profile"):
            self._reply(200, {"code": 0, "msg": "ok", "data": {}})
            return
        if self.path.startswith("/api/v1/sessions/") and ":archive" in self.path:
            self._reply(200, {"code": 0, "msg": "ok", "data": {}})
            return
        if self.path.startswith("/api/v1/sessions/") and "/approvals/" in self.path:
            self._reply(200, {"code": 0, "msg": "ok", "data": {}})
            return
        self._reply(404, {"code": 1, "msg": "not found"})


def make_server():
    srv = HTTPServer(("127.0.0.1", 0), FakeHandler)
    srv.seen = []
    srv.fail_create = False
    srv.profile_resp = {"agent_config": {"model": MODEL_HI, "permission_mode": "auto"}}
    srv.pending = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def base_of(srv):
    return "http://127.0.0.1:%d" % srv.server_address[1]


def dead_base():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


# ---------- 夹具写入 ----------

def enable_relay(root):
    d = os.path.join(root, "relay")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "relay.enabled"), "w", encoding="utf-8", newline="\n") as f:
        f.write("enabled\n")


def write_roles(root, employees=None, extra=None):
    path = os.path.join(root, "relay", "runtime", "roles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"version": 1,
            "leader": {"label": "leader", "session_id": None, "short": None, "model": None},
            "employees": dict(employees or {}),
            "updated_at": "2026-09-19T00:00:00Z"}
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_roles(root):
    with open(os.path.join(root, "relay", "runtime", "roles.json"), encoding="utf-8") as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relay-spawn-test-")
        enable_relay(self.root)
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

    def posts(self):
        return [r for r in self.srv.seen if r["method"] == "POST"]

    def post_bodies(self):
        return [json.loads(r["raw"].decode("utf-8")) for r in self.posts()]

    def run_cli(self, *extra, env=None):
        e = dict(os.environ)
        e.update(env or {})
        e["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([PY, RELAY_SPAWN] + list(extra),
                              capture_output=True, timeout=30, env=e)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))


class SpawnProtocol(Base):
    def test_spawn_happy_path_call_order(self):
        ok, detail, sid = relay_spawn.spawn_worker(
            self.root, "employee-2", model=MODEL_HI, permission="auto", settle=0)
        self.assertTrue(ok, detail)
        self.assertEqual(sid, SID_A)
        # 调用序：healthz → 建会话 → profile(model) → profile(permission) → 读回 profile
        seq = [(r["method"], r["path"]) for r in self.srv.seen]
        self.assertEqual(seq[0], ("GET", "/healthz"))
        self.assertEqual(seq[1], ("POST", "/api/v1/sessions"))
        self.assertEqual(seq[2], ("POST", "/api/v1/sessions/%s/profile" % SID_A))
        self.assertEqual(seq[3], ("POST", "/api/v1/sessions/%s/profile" % SID_A))
        self.assertEqual(seq[4], ("GET", "/api/v1/sessions/%s/profile" % SID_A))
        # 实测：创建请求不带 agent_config，只带 title + metadata.cwd
        create = self.post_bodies()[0]
        self.assertEqual(create, {"title": "relay-employee-2", "metadata": {"cwd": self.root}})
        # 实测：model 与 permission 分两次 profile 调用
        p1, p2 = self.post_bodies()[1], self.post_bodies()[2]
        self.assertEqual(p1, {"agent_config": {"model": MODEL_HI}})
        self.assertEqual(p2, {"agent_config": {"permission_mode": "auto"}})

    def test_spawn_roles_json_backfill_preserves_fields(self):
        write_roles(self.root,
                    employees={"employee-1": {"session_id": "sess_1111aaaa-0000",
                                              "short": "1111aaaa", "note": "老员工"}},
                    extra={"leader_note": "别动我"})
        ok, detail, sid = relay_spawn.spawn_worker(
            self.root, "employee-2", model=MODEL_HI, settle=0)
        self.assertTrue(ok, detail)
        data = read_roles(self.root)
        self.assertEqual(data["leader_note"], "别动我")            # 其他顶层字段保留
        self.assertEqual(data["employees"]["employee-1"]["note"], "老员工")  # 老员工字段保留
        ent = data["employees"]["employee-2"]                      # 新角色自动建
        self.assertEqual(ent["session_id"], SID_A)
        self.assertEqual(ent["short"], "a1b2c3d4")                 # uuid 前 8 位
        self.assertEqual(ent["model"], MODEL_HI)
        self.assertNotEqual(data["updated_at"], "2026-09-19T00:00:00Z")  # 已刷新

    def test_spawn_roles_json_created_from_scratch(self):
        ok, detail, sid = relay_spawn.spawn_worker(self.root, "employee-2", settle=0)
        self.assertTrue(ok, detail)
        data = read_roles(self.root)
        self.assertEqual(data["employees"]["employee-2"]["session_id"], SID_A)
        self.assertIn("updated_at", data)

    def test_spawn_title_prefix(self):
        ok, detail, sid = relay_spawn.spawn_worker(
            self.root, "employee-2", title="ops", settle=0)
        self.assertTrue(ok, detail)
        self.assertEqual(self.post_bodies()[0]["title"], "ops-employee-2")

    def test_spawn_no_model_skips_model_write_and_readback(self):
        ok, detail, sid = relay_spawn.spawn_worker(
            self.root, "employee-2", model=None, permission="manual", settle=0)
        self.assertTrue(ok, detail)
        # 只有 permission 一次 profile；无模型补写则无读回校验
        self.assertEqual(len(self.posts()), 2)  # create + 1 profile
        self.assertEqual(self.post_bodies()[1],
                         {"agent_config": {"permission_mode": "manual"}})

    def test_spawn_settle_sleep(self):
        from unittest import mock
        with mock.patch("time.sleep") as slp:
            ok, detail, sid = relay_spawn.spawn_worker(
                self.root, "employee-2", settle=5.0)
        self.assertTrue(ok, detail)
        slp.assert_called_once_with(5.0)

    def test_spawn_default_settle_is_15(self):
        from unittest import mock
        with mock.patch("time.sleep") as slp:
            ok, detail, sid = relay_spawn.spawn_worker(self.root, "employee-2")
        self.assertTrue(ok, detail)
        slp.assert_called_once_with(15.0)   # 实测缺省 settle 15 秒

    def test_spawn_create_envelope_fail(self):
        self.srv.fail_create = True
        ok, detail, sid = relay_spawn.spawn_worker(self.root, "employee-2", settle=0)
        self.assertFalse(ok)
        self.assertIsNone(sid)
        self.assertIn("api:code=7", detail)

    def test_spawn_readback_model_empty_is_verify_fail(self):
        self.srv.profile_resp = {"agent_config": {"model": ""}}   # 实测症状：存下空串
        ok, detail, sid = relay_spawn.spawn_worker(
            self.root, "employee-2", model=MODEL_HI, settle=0)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("verify:"), detail)
        # 校验失败不回填 roles.json
        self.assertFalse(os.path.isfile(os.path.join(self.root, "relay", "runtime", "roles.json")))

    def test_spawn_bad_root_no_marker(self):
        bare = tempfile.mkdtemp(prefix="relay-spawn-bare-")
        ok, detail, sid = relay_spawn.spawn_worker(bare, "employee-2", settle=0)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-root:"), detail)
        self.assertIsNone(sid)
        self.assertEqual(self.srv.seen, [])

    def test_spawn_bad_role(self):
        ok, detail, sid = relay_spawn.spawn_worker(self.root, "../evil", settle=0)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-role:"), detail)

    def test_spawn_bad_permission(self):
        ok, detail, sid = relay_spawn.spawn_worker(self.root, "employee-2",
                                                   permission="god", settle=0)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-permission:"), detail)

    def test_spawn_no_server(self):
        os.environ["RELAY_PUSH_BASE"] = dead_base()
        ok, detail, sid = relay_spawn.spawn_worker(self.root, "employee-2", settle=0)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-server:"), detail)


class Archive(Base):
    def test_delete_archives_and_clears_binding(self):
        write_roles(self.root, employees={"employee-2": {"session_id": SID_A,
                                                         "short": "a1b2c3d4"},
                                          "employee-1": {"session_id": "sess_1111aaaa-0000",
                                                         "short": "1111aaaa"}})
        ok, detail = relay_spawn.archive_worker(self.root, "employee-2")
        self.assertTrue(ok, detail)
        self.assertIn("archived=%s" % SID_A, detail)
        # 归档命中 :archive 端点（软删）
        self.assertIn(("POST", "/api/v1/sessions/%s:archive" % SID_A),
                      [(r["method"], r["path"]) for r in self.srv.seen])
        data = read_roles(self.root)
        self.assertNotIn("employee-2", data["employees"])        # 绑定已清
        self.assertIn("employee-1", data["employees"])           # 其他员工保留

    def test_delete_no_binding(self):
        write_roles(self.root)
        ok, detail = relay_spawn.archive_worker(self.root, "employee-2")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("no-binding:"), detail)

    def test_delete_bad_root(self):
        bare = tempfile.mkdtemp(prefix="relay-spawn-bare-")
        ok, detail = relay_spawn.archive_worker(bare, "employee-2")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("bad-root:"), detail)


class Approve(Base):
    def test_approve_once_approves_all_pending(self):
        self.srv.pending = [{"approval_id": "ap-1", "tool": "Bash"},
                            {"approval_id": "ap-2", "tool": "Write"}]
        ok, detail = relay_spawn.approve_pending(SID_A, once=True)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "approved=2")
        # GET 必须带 status=pending（实测缺了报 40001）
        gets = [r for r in self.srv.seen if r["method"] == "GET"
                and "/approvals" in r["path"]]
        self.assertEqual(len(gets), 1)
        self.assertIn("status=pending", gets[0]["path"])
        # 每条一条 POST，body 实测协议
        posts = [r for r in self.posts() if "/approvals/" in r["path"]]
        self.assertEqual([r["path"] for r in posts],
                         ["/api/v1/sessions/%s/approvals/ap-1" % SID_A,
                          "/api/v1/sessions/%s/approvals/ap-2" % SID_A])
        self.assertEqual(json.loads(posts[0]["raw"].decode("utf-8")),
                         {"decision": "approved", "scope": "session"})

    def test_approve_once_empty_pending(self):
        ok, detail = relay_spawn.approve_pending(SID_A, once=True)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "approved=0")
        self.assertEqual([r for r in self.posts() if "/approvals/" in r["path"]], [])

    def test_approve_emit_callback(self):
        self.srv.pending = [{"approval_id": "ap-9"}]
        lines = []
        ok, detail = relay_spawn.approve_pending(SID_A, once=True, emit=lines.append)
        self.assertTrue(ok, detail)
        self.assertEqual(lines, ["round=1 approved=1 total=1"])

    def test_approve_missing_query_param_rejected(self):
        # 假服务器对缺 status=pending 的 GET 回 40001；此处验证 _approve_round 透传错误
        ok, detail = relay_spawn.approve_pending(SID_A, once=True)
        self.assertTrue(ok, detail)  # 正常路径带 query，不受影响
        gets = [r for r in self.srv.seen if r["method"] == "GET" and "/approvals" in r["path"]]
        self.assertIn("status=pending", gets[0]["path"])


class Cli(Base):
    def test_cli_spawn_ok_output(self):
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--model", MODEL_HI, "--settle", "0")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "OK employee-2 %s short=a1b2c3d4" % SID_A)

    def test_cli_spawn_bad_root_exit2(self):
        bare = tempfile.mkdtemp(prefix="relay-spawn-bare-")
        rc, out, err = self.run_cli("--root", bare, "--role", "employee-2", "--settle", "0")
        self.assertEqual(rc, 2)
        self.assertIn("bad-root:", err)

    def test_cli_spawn_bad_permission_exit2(self):
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--permission", "god", "--settle", "0")
        self.assertEqual(rc, 2)
        self.assertIn("bad-permission:", err)

    def test_cli_spawn_no_server_exit3(self):
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--settle", "0",
                                    env={"RELAY_PUSH_BASE": dead_base()})
        self.assertEqual(rc, 3)
        self.assertIn("no-server:", err)

    def test_cli_spawn_create_fail_exit4(self):
        self.srv.fail_create = True
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--settle", "0")
        self.assertEqual(rc, 4)
        self.assertIn("api:code=7", err)

    def test_cli_delete_ok(self):
        write_roles(self.root, employees={"employee-2": {"session_id": SID_A,
                                                         "short": "a1b2c3d4"}})
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--delete")
        self.assertEqual(rc, 0, err)
        self.assertIn("archived=%s" % SID_A, out)
        self.assertNotIn("employee-2", read_roles(self.root)["employees"])

    def test_cli_delete_no_binding_exit3(self):
        write_roles(self.root)
        rc, out, err = self.run_cli("--root", self.root, "--role", "employee-2",
                                    "--delete")
        self.assertEqual(rc, 3)
        self.assertIn("no-binding:", err)

    def test_cli_approve_once_ok(self):
        write_roles(self.root, employees={"employee-2": {"session_id": SID_A,
                                                         "short": "a1b2c3d4"}})
        self.srv.pending = [{"approval_id": "ap-1"}]
        rc, out, err = self.run_cli("approve", "--root", self.root,
                                    "--role", "employee-2", "--once")
        self.assertEqual(rc, 0, err)
        self.assertIn("round=1 approved=1 total=1", out)
        self.assertIn("OK approve employee-2 approved=1", out)

    def test_cli_approve_no_binding_exit3(self):
        write_roles(self.root)
        rc, out, err = self.run_cli("approve", "--root", self.root,
                                    "--role", "employee-2", "--once")
        self.assertEqual(rc, 3)
        self.assertIn("no-binding:", err)

    def test_cli_approve_bad_root_exit2(self):
        bare = tempfile.mkdtemp(prefix="relay-spawn-bare-")
        rc, out, err = self.run_cli("approve", "--root", bare,
                                    "--role", "employee-2", "--once")
        self.assertEqual(rc, 2)
        self.assertIn("bad-root:", err)


class TemplateSync(unittest.TestCase):
    def test_template_copy_byte_identical(self):
        with open(os.path.join(ROOT_REPO, "tools", "relay_spawn.py"), "rb") as f:
            a = f.read()
        with open(os.path.join(ROOT_REPO, "template", "tools", "relay_spawn.py"), "rb") as f:
            b = f.read()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
