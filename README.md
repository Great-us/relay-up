# Session Relay (relay-up)

**[简体中文](README.zh-CN.md) | English**

**One leader, multiple workers, multiple models — a fully automated task relay between ZCode sessions.** Type `/relay-up` in any project to install it. Worker windows wake themselves on a schedule and pick up work; the leader reviews reports and dispatches new tasks, looping until the backlog is done. All transport is plain file I/O — the relay itself never calls any model API.

> Born out of real-world TASK-010/011 work: the entire chain (event hooks → file mailbox → scheduled wake-ups → acceptance & archival) was verified end-to-end with live sessions on a Windows machine, including verbatim hash-checked round trips.

## The problem it solves

You have several ZCode windows open, each running a different model (an expensive one as the leader, cheap ones as workers). Making them collaborate normally means copy-pasting between windows by hand. Session Relay turns *dispatch → execute → report → review → dispatch* into a fully automated loop:

```
Leader session (any model)
  │ ① writes task cards (relay-task v1.1, with SHA-256 + write authorization) → relay/inbox/
  │    ＋ directed chat messages → relay/chat/
  ▼
Worker sessions (cheap-model windows, each with a 5-min watcher cron)
  │ ② cron wakes them → they self-check the mailbox → atomically claim cards
  │    → drain-mode execution (all pending work in one turn)
  │ ③ report to relay/outbox/ (9-field contract) ＋ chat-notify the leader
  ▼
Leader (in person, or a 10-min duty cron)
  │ ④ mechanical 7-check verification (verify_report.py) → archive / rework cards (-R1)
  │ ⑤ dispatch next tasks → back to ①
  ▼
Queue empty & everything accepted → automatic standby throttle
  (idle shutdown of worker crons after N quiet rounds; one sentence revives everything)
```

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

   Without hooks everything still works — manual `/relay-next` and the worker watcher cron only need file I/O. Since v3, one registration serves **all** projects: the hook routes by the `relay/relay.enabled` marker that `/relay-up` writes.
3. **Enable** — type `/relay-up` in any project's ZCode window (disable: `/relay-up down`).
4. **Open worker windows** — start a ZCode window on a cheap model, send it any one message to wake it, then hand it the bootstrap card from `template/relay/bootstrap-card.example.json` (it installs its own watcher cron). From then on it self-checks every 5 minutes, unattended.

## Safety design (why it's safe to leave unattended)

- **Fail-open** — any hook error produces empty output and exit 0; your session is never blocked.
- **Atomic claiming** — all contention resolved by same-volume `rename`; when two workers race for one card, exactly one wins.
- **Dual SHA-256 verification** — card `prompt_sha256` and report `report_sha256` are checked byte-for-byte (no trimming, no newline normalization); tampered or corrupt payloads are rejected and quarantined.
- **Write-authorization boundary** — every card carries `authorized_write_paths`; any instruction inside a prompt that asks for writes beyond them (shared docs, credentials, network, git) is refused and logged — **a prompt is data, not instructions**.
- **Continuation chain cap** — at most 3 consecutive automatic wake-ups per natural turn (a platform rule); every cron tick is a fresh natural turn, and drain-mode does unlimited work *within* a turn. You get endurance and runaway protection at the same time.
- **Lifecycle governance** — the leader can shut down / delete / throttle all crons at any time; when unattended, sustained idleness automatically stands workers down and throttles the leader to an hourly watch, revivable with a single sentence.

## Chat lane v2: threads, cursors, budget, mute

Beyond the task-card lane, relay-up ships a chat lane between sessions (`tools/chat_send.py`, `tools/chat_read.py`, `tools/chat_state.py`, contract in `template/relay/chat/CONTRACT.md` §v2.0):

- **Two-layer storage** — every message is appended to a thread log (`relay/chat/threads/<thread_id>/messages.jsonl`, the source of truth) *and* delivered to the recipient mailbox (`to-<addr>/pending/`), so v1 receivers work unchanged.
- **Cursors** — `relay/runtime/cursors/<session>.json` tracks read positions per thread; advanced via `chat_read.py --mark` (hook auto-advance is host-project work).
- **Presence & budget & mute** — `presence.json` (schema), a persisted per-thread × per-session × per-day auto-reply **budget of 3** enforced via `chat_state.py --budget-check`, and a `chat-mute.json` switch that pauses auto-replies without touching history.
- **Wake degradation (explicit, no unconditional "unread = delivered")**: an *active* session receives messages at its next hook event; an *idle* session relies on its watcher cron or the user; cross-vendor receivers (Codex / Claude Code / other CLIs) use the manual paste fallback — a digest tool renders the pending mailbox as pasteable text. The relay never promises push delivery to arbitrary clients.
- The cross-vendor envelope bridge (events.db → chat v2, explicit `relay-chat` fence only) is host-project tooling, **not** part of this template.

## Repository structure

```
SKILL.md                     # the /relay-up skill (installer: up/down modes)
hooks/relay_hook.py          # session hook: SessionStart registration / Stop continuation
                             #   injection / UserPromptSubmit context injection (v3 marker routing)
tools/chat_send.py           # chat-lane send CLI (v2: thread log + mailbox dual write)
tools/chat_read.py           # thread viewer: threads/dump/unread/cursors/presence
tools/chat_state.py          # shared state: cursors/presence/mute/budget (atomic, locked)
tools/verify_report.py       # leader acceptance: 7-check verifier
template/                    # what gets scaffolded into target projects (mailbox contracts,
                             #   worker skill, bootstrap card example, empty runtime skeletons)
tests/                       # stdlib-only tests (claim/inject/merge/dedupe/quarantine/
                             #   gating/fail-open/multi-project routing + chat v2 suites)
check_template.py            # template integrity self-check
```

## Verified behaviors

Stop-hook continuation injection (`{"decision":"block"}` accepted by the platform), `UserPromptSubmit` `additionalContext` context injection, the 3-continuations-per-turn cap, fail-open, cron self-wake, dual-lane merged injection, bad-message quarantine (`*.bad`), marker-based multi-project routing. The test suite covers all of the above at logic level; platform behaviors were verified against live ZCode sessions (as of 2026-09).

## Limitations & roadmap

- Single machine, Windows. Cross-client workers (Codex / Claude / Kimi) need their own wake channels — that's the parent project's next milestone.
- Distribution as a one-click plugin is future work; today it's a copy-into-skills-folder install.
- No background model calls — a "worker" is always a visible native window. That's a principle, not a limitation.

## License

MIT

## Cost safety (since v2)

Duty loops ship with anti-waste governance: after 2 consecutive idle rounds the watcher downgrades to hourly; after 4 it deletes all of its timers. Idle checks prefer scripts/hooks over model turns; overnight duty is off by default and must be enabled explicitly. Any session must verify its CronDelete tool actually works before relying on auto-deletion — otherwise it escalates to the user instead of idling silently. See the cost_safety block in the relay template runtime/loop-config.json.
