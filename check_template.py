#!/usr/bin/env python3
"""relay-up 技能模板自检（仅标准库）：文件齐全、JSON 骨架可解析、无真实 session_id 泄漏。"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED = [
    "SKILL.md",
    "template/relay/README.md",
    "template/relay/chat/CONTRACT.md",
    "template/.zcode/skills/relay-next/SKILL.md",
    "template/relay/runtime/roles.json",
    "template/relay/runtime/leader-queue.json",
    "template/relay/runtime/loop-config.json",
    "template/relay/bootstrap-card.example.json",
    # TASK-016G：chat v2 家族必备
    "hooks/relay_hook.py",
    "tools/chat_send.py",
    "tools/chat_read.py",
    "tools/chat_state.py",
    "tools/verify_report.py",
    "template/tools/chat_send.py",
    "template/tools/chat_read.py",
    "template/tools/chat_state.py",
    "tests/test_chat_send.py",
    "tests/test_chat_read.py",
    "tests/test_chat_a2.py",
    "tests/test_relay_hook_chat.py",
    "tests/test_verify_report.py",
]
fails = []
for rel in REQUIRED:
    if not os.path.isfile(os.path.join(HERE, rel)):
        fails.append("缺文件: " + rel)
for rel in ("template/relay/runtime/roles.json", "template/relay/runtime/leader-queue.json",
            "template/relay/runtime/loop-config.json", "template/relay/bootstrap-card.example.json"):
    try:
        json.load(open(os.path.join(HERE, rel), encoding="utf-8"))
    except Exception as exc:
        fails.append("JSON 不可解析: %s (%s)" % (rel, exc))
# chat v2 一致性：template/tools 副本与 tools/ 正本逐字节一致
for name in ("chat_send.py", "chat_read.py", "chat_state.py"):
    a = os.path.join(HERE, "tools", name)
    b = os.path.join(HERE, "template", "tools", name)
    if os.path.isfile(a) != os.path.isfile(b) or (
            os.path.isfile(a) and open(a, "rb").read() != open(b, "rb").read()):
        fails.append("模板副本与正本不一致: tools/%s" % name)
contract = os.path.join(HERE, "template/relay/chat/CONTRACT.md")
if os.path.isfile(contract) and "v2.0" not in open(contract, encoding="utf-8").read():
    fails.append("CONTRACT.md 缺 v2.0 章节")

# 泄漏扫描限安装面；tests/ 为合成夹具（含刻意构造的 sess_xxxxxxxx- 形态）不扫描
for rel in REQUIRED:
    if rel.startswith("tests/"):
        continue
    p = os.path.join(HERE, rel)
    if not os.path.isfile(p):
        continue
    body = open(p, encoding="utf-8").read()
    hits = re.findall(r"sess_[0-9a-f]{8}-", body)
    if hits:
        fails.append("疑似真实 session_id 泄漏: %s (%d 处)" % (rel, len(hits)))
print("\n".join(fails) if fails else "OK: 模板 %d 项齐全，JSON 可解析，无真实会话ID泄漏" % len(REQUIRED))
sys.exit(1 if fails else 0)
