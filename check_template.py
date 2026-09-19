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
for rel in REQUIRED:
    p = os.path.join(HERE, rel)
    if not os.path.isfile(p):
        continue
    body = open(p, encoding="utf-8").read()
    hits = re.findall(r"sess_[0-9a-f]{8}-", body)
    if hits:
        fails.append("疑似真实 session_id 泄漏: %s (%d 处)" % (rel, len(hits)))
print("\n".join(fails) if fails else "OK: 模板 %d 项齐全，JSON 可解析，无真实会话ID泄漏" % len(REQUIRED))
sys.exit(1 if fails else 0)
