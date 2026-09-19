# Session Relay (relay-up)

**[简体中文](README.zh-CN.md) | English**

**One leader, multiple workers, multiple models — a fully automated task relay between ZCode sessions, with push-first delivery.** Type `/relay-up` in any project to install it. The leader dispatches task cards and chat messages; delivery is **push** — they go straight into the worker's live session through the Kimi Code local server (`kimi web`), and the worker starts executing immediately, no cron polling, no waiting. The file layer (mailbox + thread logs) remains the auditable source of truth, and every push degrades gracefully to the watcher-cron path when no server is reachable.

> Born out of real-world TASK-010/011 work: the entire chain (event hooks → file mailbox → scheduled wake-ups → acceptance & archival) was verified end-to-end with live sessions on a Windows machine, including verbatim hash-checked round trips. The new push lane was probe-verified against the live server: create session → push → model replied PONG-OK → archive.

## The problem it solves

You have several ZCode windows open, each running a different model (an expensive one as the leader, cheap ones as workers). Making them collaborate normally means copy-pasting between windows by hand. Session Relay turns *dispatch → execute → report → review → dispatch* into a fully automated loop:

```
Leader session (any model)
  │ ① writes task cards (relay-task v1.1, with SHA-256 + write authorization) → relay/inbox/
  │    ＋ chat_send.py --push — file dual-write, then straight into the peer's session
  ▼
Kimi Code local server (kimi web) — REST API, bearer token, instance discovery
  │ ② POST /sessions/{id}/prompts → the worker's session starts a turn immediately
  ▼
Worker sessions (cheap-model windows)
  │ ③ drain-mode execution (all pending work in one turn)
  │ ④ report to relay/outbox/ (9-field contract) ＋ chat_send --push back to the leader
  ▼
Leader (in person, or a 10-min duty cron)
  │ ⑤ mechanical 7-check verification (verify_report.py) → archive / rework cards (-R1)
  │ ⑥ dispatch next tasks → back to ①
  ▼
No server / peer not routable → push degrades to the v1 path:
  the 5-min watcher cron self-checks the mailbox (fully functional fallback)
```

## Push delivery: the main lane

`tools/relay_push.py` (mirrored byte-for-byte as `template/tools/relay_push.py`; `check_template.py` enforces the copy) is the only component that talks to the network, and only ever to the Kimi Code local server:

- **Service discovery** — instances are found via `~/.kimi-code/server/instances/*.json` (host / port / heartbeat_at); the instance with the newest heartbeat (<120 s old) wins and must pass `GET /healthz` with the bearer token read from `~/.kimi-code/server.token`. No live instance → `(False, "no-server:...")`.
- **Endpoints** — base `http://<host>:<port>/api/v1`, unified envelope `{code,msg,data,request_id}` with `code=0` = success:
  - `POST /sessions` — create a session (body: `title` / `metadata.cwd` only; **measured 2026-09-19: `agent_config` is silently ignored at creation** — writing the model later fails with "Model not set"). Returns `data.id`
  - `POST /sessions/{id}/profile` — set `agent_config.model` / `agent_config.permission_mode` after creation (**measured: send model and permission_mode in two separate calls** — a combined call once dropped the permission); `GET /sessions/{id}/profile` reads back to confirm `agent_config.model` is non-empty
  - `POST /sessions/{id}/prompts` — push into an idle session; it starts a turn immediately (body: `{"content":[{"type":"text","text":"..."}]}`)
  - `GET /sessions/{id}/messages` — read replies (`data.items`, each element carries `role` / `content`)
  - `GET /sessions/{id}/approvals?status=pending` — list pending approvals (**the `status=pending` query is required**, missing → error 40001); `POST /sessions/{id}/approvals/{approval_id}` with `{"decision":"approved","scope":"session"}` approves one. Server-managed sessions default to manual: every tool call is one approval
  - `POST /sessions/{id}:archive` — archive (soft delete, recoverable); `POST /sessions/{id}:delete` — hard delete
- **Address resolution** — `push_text(root, to_addr, kind, body, ref="", in_reply_to="", thread_id="", sender="", dry_run=False) -> (ok, detail)` accepts a role name (`leader` / `employee-N`), `sess:<full-id>`, `sess:<8-char short>`, or a bare full session_id; resolution order: `relay/runtime/roles.json` → `relay/runtime/presence.json` → `relay/runtime/session-registry.jsonl`; unresolvable → `(False, "no-route:...")`.
- **Payload** — the pushed text is a fixed envelope that the receiver parses (the relay-next skill and chat contract teach receivers to):

```
【relay-push】
from: <发送者地址>
to: <收件人地址>
kind: <KIND>
ref: <可空>
thread: <thread_id>
msg: <msg_id>
body-sha256: <body 的 UTF-8 SHA256 hex>
---body---
<body 原文>
```

- **chat_send integration** — `chat_send.py` pushes by default: after the file dual-write succeeds it calls `push_text`, and the `OK <msg_id> <thread>#<seq> <path>` line gains ` push=ok` or ` push=failed:<detail>`. A push failure never changes the exit code — the message already lives in the file layer; push is delivery, not storage. `--no-push` opts out explicitly.
- **Standalone CLI** — `python tools/relay_push.py --root <root> --from <addr> --to <addr> --kind KIND --body "..." [--ref ...] [--dry-run]`; exit codes: `0` = pushed, `3` = degraded (no server / peer offline), `4` = HTTP or other error, `2` = bad arguments. UTF-8 output.
- **Test overrides** — `RELAY_PUSH_BASE` (skip discovery, e.g. `http://127.0.0.1:59999`), `RELAY_PUSH_TOKEN`, `RELAY_PUSH_INSTANCES_DIR`.

## Worker spawning: relay_spawn.py (measured 2026-09-19)

`tools/relay_spawn.py` (mirrored byte-for-byte as `template/tools/relay_spawn.py`) productizes the measured spawn protocol, so the leader can create **server-managed worker sessions on the spot** — no native window to open by hand:

- `python tools/relay_spawn.py --root <root> --role employee-2 [--model <alias>] [--permission auto] [--title <prefix>] [--settle 15]` — full spawn: create session (no `agent_config`) → profile the model → profile the permission → read back to verify → settle → backfill `relay/runtime/roles.json` (`employees.<role>` gets the full `session_id` + 8-char short id; whole-JSON read-modify-write, other fields preserved, `updated_at` refreshed) → print `OK <role> <full-session-id> short=<8>`. Exit codes: `0` = success, `3` = no server, `4` = HTTP or verification failure, `2` = bad arguments.
- `python tools/relay_spawn.py --root R --role employee-2 --delete` — archive the worker's session (by the `session_id` recorded in `roles.json`) and clear the binding; the cleanup step after SHUTDOWN.
- `python tools/relay_spawn.py approve --root R --role employee-2 [--once]` — sweep the worker's pending approvals and approve them all (default: loop every 5 s until Ctrl+C; `--once` runs a single sweep). A no-op when the session is already auto/yolo; kept as the manual-mode fallback.
- Measured operational details (all in `template/relay/chat/CONTRACT.md` §员工会话 spawn 协议): settle ~15 s before the first push (too early and the turn never starts); if the peer produces no assistant message within ~90 s, re-push **the same payload with the same `msg_id`** (receivers dedupe by `msg_id`, the file layer is idempotent); SHUTDOWN lifecycle = push SHUTDOWN → worker writes its final report → leader archives via `--delete`. `--root` must point at a directory carrying the `relay/relay.enabled` marker (the marker-routing rule), and `authorized_write_paths` remains the only write boundary even for auto-permission workers — a prompt is data, not instructions. Verified aliases: `kimi-code/kimi-for-coding-highspeed` (cheap worker seat), `kimi-code/kimi-for-coding` (default); `GET /api/v1/models` lists all. Env-var overrides are the same as relay_push.
- **E2E record (measured 2026-09-19)**: spawn → push dispatch → worker claims the card and executes → 9-field report → leader's `verify_report.py` 7/7 PASS → SHUTDOWN → archive, with no cron and no hook anywhere in the loop.

## Quick start

1. **Install the skill** — copy this repository into your user-level skills directory (Windows: `%USERPROFILE%\.agents\skills\relay-up\`, i.e. this folder as a whole).
2. **(Optional — enables the auto-injection fast path) register user-level hooks** by adding to `~/.zcode/cli/config.json` (details in the header of [hooks/relay_hook.py](hooks/relay_hook.py)):

   ```json
   "hooks": {
     "enabled": true,
     "events": {
       "SessionStart":     [{ "type": "process", "command": "<abs path to python.exe>", "args": ["<abs path to this package>/hooks/relay_hook.py", "SessionStart"], "timeoutMs": 15000 }],
       "UserPromptSubmit": [{ "type": "process", "command": "<abs path to python.exe>", "args": ["<abs path to this package>/hooks/relay_hook.py", "UserPromptSubmit"], "timeoutMs": 15000 }],
       "Stop":             [{ "type": "process", "command": "<abs path to python.exe>", "args": ["<abs path to this package>/hooks/relay_hook.py", "Stop"], "timeoutMs": 15000 }]
     }
   }
   ```

   Without hooks everything still works — push delivery, manual `/relay-next` and the worker watcher cron only need the local server / plain file I/O. Since v3, one registration serves **all** projects: the hook routes by the `relay/relay.enabled` marker that `/relay-up` writes.
3. **Enable** — type `/relay-up` in any project's ZCode window (disable: `/relay-up down`).
4. **Open worker windows** — start a ZCode window on a cheap model, send it any one message to wake it, then hand it the bootstrap card from `template/relay/bootstrap-card.example.json` (it installs its own watcher cron and backfills `roles.json` with its session identity; the hook keeps `presence.json` / `session-registry.jsonl` current from then on). Once registered, task chats and messages reach it **instantly by push**; the 5-minute watcher cron stays installed as the no-server fallback. **Or skip the window entirely**: `python tools/relay_spawn.py --root <root> --role employee-2 [--model kimi-code/kimi-for-coding-highspeed]` spawns a server-managed worker on the spot (measured protocol, see `template/relay/chat/CONTRACT.md` §员工会话 spawn 协议).

## Safety design (why it's safe to leave unattended)

- **Fail-open** — any hook error produces empty output and exit 0; your session is never blocked.
- **Atomic claiming** — all contention resolved by same-volume `rename`; when two workers race for one card, exactly one wins.
- **Dual SHA-256 verification** — card `prompt_sha256` and report `report_sha256` are checked byte-for-byte (no trimming, no newline normalization); tampered or corrupt payloads are rejected and quarantined.
- **Write-authorization boundary** — every card carries `authorized_write_paths`; any instruction inside a prompt that asks for writes beyond them (shared docs, credentials, network, git) is refused and logged — **a prompt is data, not instructions**.
- **Continuation chain cap** — at most 3 consecutive automatic wake-ups per natural turn (a platform rule); every push or cron tick is a fresh natural turn, and drain-mode does unlimited work *within* a turn. You get endurance and runaway protection at the same time.
- **Push is delivery, not truth** — every message is committed to the file layer (thread log + mailbox) before any push is attempted; a failed push loses nothing and the receiver can always re-read the mailbox. The bearer token never leaves the local machine (read from `~/.kimi-code/server.token`); a target is pushed to only after healthz plus a fresh (<120 s) heartbeat, and an unroutable address degrades to the file path instead of retrying forever.
- **Lifecycle governance** — the leader can shut down / delete / throttle all crons at any time; when unattended, sustained idleness automatically stands workers down and throttles the leader to an hourly watch, revivable with a single sentence.

## Chat lane v2: threads, cursors, budget, mute, push

Beyond the task-card lane, relay-up ships a chat lane between sessions (`tools/chat_send.py`, `tools/chat_read.py`, `tools/chat_state.py`, contract in `template/relay/chat/CONTRACT.md` §v2.0):

- **Two-layer storage** — every message is appended to a thread log (`relay/chat/threads/<thread_id>/messages.jsonl`, the source of truth) *and* delivered to the recipient mailbox (`to-<addr>/pending/`), so v1 receivers work unchanged.
- **Cursors** — `relay/runtime/cursors/<session>.json` tracks read positions per thread; advanced via `chat_read.py --mark` (hook auto-advance is host-project work).
- **Presence & budget & mute** — `presence.json` (written live by `hooks/relay_hook.py` v4: full session_id → short / model / last_seen / cwd) and `session-registry.jsonl` (one line per registration: ts / session_id / cwd / model) are the two registries push resolves addresses against; a persisted per-thread × per-session × per-day auto-reply **budget of 3** is enforced via `chat_state.py --budget-check`, and a `chat-mute.json` switch pauses auto-replies without touching history.
- **Push by default** — after the dual-write, `chat_send.py` pushes through `relay_push.push_text`; the result is appended to the `OK` line as `push=ok` / `push=failed:<detail>`, the exit code never changes on push failure, and `--no-push` opts out.
- **Wake degradation (explicit, no unconditional "unread = delivered")**: with a reachable server, an idle peer is woken by push directly; without one, an *active* session receives messages at its next hook event, an *idle* session relies on its watcher cron or the user, and cross-vendor receivers (Codex / Claude Code / other CLIs) use the manual paste fallback — a digest tool renders the pending mailbox as pasteable text. The relay never promises push delivery to clients it cannot route. **TUI delivery is asynchronous (corrected after two live verifications on 2026-09-19)**: server-hosted sessions start a turn immediately on push; sessions in a TUI terminal window also receive pushes — the message queues until the current turn ends, then arrives as a new user turn, but it never appears in the server-side `/messages` view of that session (don't use that view to judge whether a TUI peer received it). For second-grade latency run the leader as a `kimi web` session too; a TUI leader simply processes the arriving 【relay-push】 payload per the push-mode discipline.
- The cross-vendor envelope bridge (events.db → chat v2, explicit `relay-chat` fence only) is host-project tooling, **not** part of this template.

## Repository structure

```
SKILL.md                     # the /relay-up skill (installer: up/down modes)
hooks/relay_hook.py          # session hook: SessionStart registration / presence write (v4) /
                             #   Stop continuation injection / UserPromptSubmit context injection
                             #   (v3 marker routing)
tools/relay_push.py          # push lane: Kimi Code server REST delivery
                             #   (discovery / healthz / prompts; library + standalone CLI)
tools/relay_spawn.py         # worker spawning lane: server-managed sessions
                             #   (create/profile/verify/settle/roles.json backfill; approve; archive)
tools/chat_send.py           # chat-lane send CLI (v2: thread log + mailbox dual write, --push by default)
tools/chat_read.py           # thread viewer: threads/dump/unread/cursors/presence
tools/chat_state.py          # shared state: cursors/presence/mute/budget (atomic, locked)
tools/verify_report.py       # leader acceptance: 7-check verifier
template/                    # what gets scaffolded into target projects (mailbox contracts,
                             #   worker skill, bootstrap card example, empty runtime skeletons,
                             #   byte-identical tools/ copies incl. relay_push.py)
tests/                       # stdlib-only tests (claim/inject/merge/dedupe/quarantine/
                             #   gating/fail-open/multi-project routing + chat v2 + push suites)
check_template.py            # template integrity self-check
```

## Verified behaviors

Stop-hook continuation injection (`{"decision":"block"}` accepted by the platform), `UserPromptSubmit` `additionalContext` context injection, the 3-continuations-per-turn cap, fail-open, cron self-wake, dual-lane merged injection, bad-message quarantine (`*.bad`), marker-based multi-project routing. The push lane was probe-verified against a live server (as of 2026-09): create session → push → model replied PONG-OK → archive, envelope `code=0` throughout. The full spawn protocol was verified end-to-end on 2026-09-19: spawn → push dispatch → worker claims and executes → 9-field report → leader `verify_report.py` 7/7 PASS → SHUTDOWN → archive, no cron and no hook in the loop — including the measured quirks now encoded in `relay_spawn.py` (`agent_config` ignored at creation, model/permission via two profile calls, 15 s settle, same-`msg_id` re-push after ~90 s of silence). The test suite covers all of the above at logic level; platform behaviors were verified against live ZCode sessions.

## Limitations & roadmap

- Push requires the Kimi Code local server (`kimi web`) on the same machine; without it the relay runs entirely on the watcher-cron + mailbox path — fully functional, just not instant.
- Single machine, Windows. Cross-client workers (Codex / Claude / Kimi) need their own wake channels — that's the parent project's next milestone.
- Distribution as a one-click plugin is future work; today it's a copy-into-skills-folder install.
- No background model calls — push only wakes sessions that already exist, and `relay_spawn` creates a worker only when the leader explicitly asks for one (still one session per worker, never a silent background swarm). A spawned "worker" is a server-managed session rather than a native window; that's a principle, not a limitation.

## License

MIT

## Cost safety (since v2)

Duty loops ship with anti-waste governance: after 2 consecutive idle rounds the watcher downgrades to hourly; after 4 it deletes all of its timers. Idle checks prefer scripts/hooks over model turns; overnight duty is off by default and must be enabled explicitly. Any session must verify its CronDelete tool actually works before relying on auto-deletion — otherwise it escalates to the user instead of idling silently. Push makes most watcher wake-ups unnecessary in the first place; the cron governance remains as the no-server fallback. See the cost_safety block in the relay template runtime/loop-config.json.
