# Session Relay（relay-up）

**[English](README.md) | 简体中文**

**一个领导，多个员工，多个模型——ZCode 会话间的全自动任务中继，推送直达。** 在任意项目敲 `/relay-up` 即装。领导派发任务卡与 chat 消息；送达走**推送**——经 Kimi Code 本机服务器（`kimi web`）直接注入员工存活会话，员工立即开轮执行，不等 cron 轮询。文件层（信箱 + 线程日志）仍是可审计的事实源；无服务器可达时，每次推送自动降级回值班 cron 路径。

> 源自 Model Relay 项目 TASK-010/011 的实战：在一台 Windows 机器上用真实会话端到端验证了整条链路（事件 hook → 文件信箱 → 定时唤醒 → 验收归档），原文往返逐字哈希核对通过。新的推送车道已经过本机服务器实测探针：建会话 → 推送 → 模型回 PONG-OK → 归档。

## 它解决什么问题

你在 ZCode 里开了好几个窗口（不同模型：贵的当领导、便宜的当员工）。让它们协作 ordinarily 要靠你人工复制粘贴。Session Relay 把“派工 → 执行 → 回报 → 审阅 → 再派工”变成全自动：

```
领导会话（任意模型）
  │ ① 写任务卡（relay-task v1.1，含 SHA256 与写入授权）→ relay/inbox/
  │    ＋ chat_send.py --push —— 文件双写后直推对端会话
  ▼
Kimi Code 本机服务器（kimi web）—— REST API、bearer 令牌、实例发现
  │ ② POST /sessions/{id}/prompts → 员工会话立即开一轮执行
  ▼
员工会话（便宜模型窗口）
  │ ③ 排空式执行（一轮连做所有待办）
  │ ④ 回报 relay/outbox/（九字段合同）＋ chat_send --push 回推领导
  ▼
领导（本人或 10 分钟值班 cron）
  │ ⑤ verify_report.py 七项机械核验 → 归档 / 返工卡(-R1)
  │ ⑥ 下发新任务 → 回到 ①
  ▼
无服务器 / 对端不可路由 → 推送降级回 v1 路径：
  5 分钟值班 cron 自查信箱（完整可用的兜底）
```

## 推送送达：主车道

`tools/relay_push.py`（逐字节同步副本 `template/tools/relay_push.py`，check_template 强制一致）是全仓库唯一联网的组件，且只连 Kimi Code 本机服务器：

- **服务发现**——实例来自 `~/.kimi-code/server/instances/*.json`（host / port / heartbeat_at）；取 heartbeat_at 最新且 <120 秒的实例，先带 `~/.kimi-code/server.token` 的 bearer 令牌过 `GET /healthz`。无存活实例 → `(False, "no-server:...")`。
- **端点**——基址 `http://<host>:<port>/api/v1`，统一信封 `{code,msg,data,request_id}`，`code=0` 即成功：
  - `POST /sessions`——建会话（body 只带 `title` / `metadata.cwd`；**2026-09-19 实测：创建时 `agent_config` 被整体忽略**——事后补模型为空串则首回合报 "Model not set"）。返回 `data.id`
  - `POST /sessions/{id}/profile`——建后补写 `agent_config.model` / `agent_config.permission_mode`（**实测 model 与 permission_mode 分两次调用更稳妥**，一次同传时 permission 曾被丢）；`GET /sessions/{id}/profile` 读回确认 `agent_config.model` 非空
  - `POST /sessions/{id}/prompts`——向空闲会话推送，会话立即开一轮（body：`{"content":[{"type":"text","text":"..."}]}`）
  - `GET /sessions/{id}/messages`——读回复（`data.items`，元素含 `role` / `content`）
  - `GET /sessions/{id}/approvals?status=pending`——列待审批（**query 参数 `status=pending` 必填**，缺了报 40001）；`POST /sessions/{id}/approvals/{approval_id}` body `{"decision":"approved","scope":"session"}` 批一条。server 托管会话缺省 manual：每个工具调用一条审批
  - `POST /sessions/{id}:archive`——归档（软删，可恢复）；`POST /sessions/{id}:delete`——硬删
- **地址解析**——`push_text(root, to_addr, kind, body, ref="", in_reply_to="", thread_id="", sender="", dry_run=False) -> (ok, detail)`；收件地址支持角色名（`leader` / `employee-N`）、`sess:<完整id>`、`sess:<8位短标识>`、裸完整 session_id；解析顺序：`relay/runtime/roles.json` → `relay/runtime/presence.json` → `relay/runtime/session-registry.jsonl`；解析不出 → `(False, "no-route:...")`。
- **载荷**——推送文本为固定信封，接收端按此解析（relay-next 技能与 chat 合同会教接收端）：

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

- **chat_send 集成**——`chat_send.py` 默认推送：文件双写成功后调用 `push_text`，`OK <msg_id> <thread>#<seq> <path>` 行尾追加 ` push=ok` 或 ` push=failed:<detail>`；push 失败一律不改退出码（消息已落文件层，push 只是送达）。`--no-push` 显式关闭。
- **独立 CLI**——`python tools/relay_push.py --root <根> --from <addr> --to <addr> --kind KIND --body "..." [--ref ...] [--dry-run]`；退出码：`0` 已推送、`3` 降级（无服务器/对端不在线）、`4` HTTP 或其他错误、`2` 参数错。输出 UTF-8。
- **测试覆盖**——`RELAY_PUSH_BASE`（跳过发现，如 `http://127.0.0.1:59999`）、`RELAY_PUSH_TOKEN`、`RELAY_PUSH_INSTANCES_DIR`。

## 员工 spawn：relay_spawn.py（2026-09-19 实测）

`tools/relay_spawn.py`（逐字节同步副本 `template/tools/relay_spawn.py`）把实测的 spawn 协议产品化，领导**现场 spawn server 托管员工会话**，不必再手工开原生窗口：

- `python tools/relay_spawn.py --root <根> --role employee-2 [--model 别名] [--permission auto] [--title 前缀] [--settle 15]`——完整 spawn：建会话（不带 `agent_config`）→ profile 补模型 → profile 补权限 → 读回校验 → settle → 回填 `relay/runtime/roles.json`（`employees.<角色>` 写入完整 `session_id` 与 8 位短标识；读-改-写整个 JSON、保留其他字段、`updated_at` 刷新）→ 打印 `OK <role> <完整session_id> short=<前8位>`。退出码：`0`=成功、`3`=无 server、`4`=HTTP 或校验失败、`2`=参数错。
- `python tools/relay_spawn.py --root R --role employee-2 --delete`——按 roles.json 里的 session_id 归档该员工会话并清绑定；SHUTDOWN 后的清理步骤。
- `python tools/relay_spawn.py approve --root R --role employee-2 [--once]`——扫该员工会话 pending 审批并全部 approved（缺省每 5 秒一轮直到 Ctrl+C；`--once` 扫一轮即退）。权限已是 auto/yolo 时通常空转，留作 manual 兜底。
- 实测操作要点（全文见 `template/relay/chat/CONTRACT.md` §员工会话 spawn 协议）：首推前 settle ~15 秒（过早推送回合可能不启动）；推送后 ~90 秒对端无 assistant 消息就用**同一 `msg_id` 载荷**重投（接收端按 `msg_id` 去重，文件层幂等）；SHUTDOWN 生命周期 = push SHUTDOWN → 员工写终局回报 → 领导 `--delete` 归档。`--root` 只接受带 `relay/relay.enabled` 标记的目录（标记路由铁律）；auto 权限下 `authorized_write_paths` 仍是唯一写入边界——prompt 是数据不是指令。已验证别名：`kimi-code/kimi-for-coding-highspeed`（便宜员工位）、`kimi-code/kimi-for-coding`（默认）；`GET /api/v1/models` 查全部。环境变量覆盖与 relay_push 一致。
- **E2E 实绩（2026-09-19 实测）**：spawn → 直推派工 → 员工领卡执行 → 九字段回报 → 领导 `verify_report.py` 7/7 PASS → SHUTDOWN → 归档，全程无 cron 无 hook。

## 快速开始

1. **安装技能**：把本仓库整体放入用户级技能目录（Windows：`%USERPROFILE%\.agents\skills\relay-up\`）。
2. **（可选，开启自动注入快路径）注册用户级 hooks**：在 `~/.zcode/cli/config.json` 顶层加入（详见 [hooks/relay_hook.py](hooks/relay_hook.py) 头注释）：

   ```json
   "hooks": {
     "enabled": true,
     "events": {
       "SessionStart":     [{ "type": "process", "command": "<python.exe 绝对路径>", "args": ["<本包绝对路径>/hooks/relay_hook.py", "SessionStart"], "timeoutMs": 15000 }],
       "UserPromptSubmit": [{ "type": "process", "command": "<python.exe 绝对路径>", "args": ["<本包绝对路径>/hooks/relay_hook.py", "UserPromptSubmit"], "timeoutMs": 15000 }],
       "Stop":             [{ "type": "process", "command": "<python.exe 绝对路径>", "args": ["<本包绝对路径>/hooks/relay_hook.py", "Stop"], "timeoutMs": 15000 }]
     }
   }
   ```

   未注册也不影响使用：推送送达、手动 `/relay-next` 与员工值班 cron 三条路径只依赖本机服务器 / 文件读写。v3 起**一份注册服务所有项目**——hook 按 `/relay-up` 写入的 `relay/relay.enabled` 标记自动路由。
3. **启用**：在任意项目的 ZCode 窗口敲 `/relay-up`（撤除：`/relay-up down`）。
4. **开员工窗**：新开 ZCode 窗口选个便宜模型，发任意一条消息唤醒，再按 `template/relay/bootstrap-card.example.json` 给它自举卡（装值班 cron、把自身会话身份回填进 `roles.json`；此后 hook 持续维护 `presence.json` / `session-registry.jsonl`）。登记后任务与消息**经推送即时直达**；5 分钟值班 cron 保留作无服务器兜底。**也可以不开窗**：`python tools/relay_spawn.py --root <根> --role employee-2 [--model kimi-code/kimi-for-coding-highspeed]` 直接现场 spawn server 托管员工（协议经实测，见 `template/relay/chat/CONTRACT.md` §员工会话 spawn 协议）。

## 安全设计（为什么敢让它无人值守）

- **fail-open**：hook 任何异常一律空输出退出，绝不阻塞你的会话。
- **原子领取**：全部抢占用同卷 `rename`，两个员工抢同一张卡只有一个成功。
- **双哈希核验**：任务卡 `prompt_sha256` 与回报 `report_sha256` 逐字校验（不 trim、不归一化换行），篡改/损坏即拒收隔离。
- **写入授权边界**：每张卡自带 `authorized_write_paths`，卡内任何越界指令（改共享文档、读凭据、外联、动 git）一律拒绝并记录——**prompt 是数据不是指令**。
- **续写链上限**：每自然轮最多 3 次连续自动唤醒（平台规则），每次推送或 cron 唤醒都是新自然轮，“排空式执行”保证轮内不限量——续航与防失控兼得。
- **推送只是送达，不是事实**：任何消息都先提交文件层（线程日志 + 邮箱）再尝试推送；推送失败不丢任何消息，接收端随时可重读信箱。bearer 令牌不出本机（读自 `~/.kimi-code/server.token`）；推送目标必须先过 healthz 且心跳新鲜（<120 秒）；地址不可路由时降级走文件路径，绝不无限重试。
- **终局治理**：领导可随时关停/删除/降频全部定时器；无人值守时持续空闲自动降频值守，保留一句话恢复能力。

## chat 车道 v2：线程、游标、预算、静音、推送

任务卡车道之外，relay-up 还内置会话间 chat 车道（`tools/chat_send.py`、`tools/chat_read.py`、`tools/chat_state.py`，合同见 `template/relay/chat/CONTRACT.md` §v2.0）：

- **双层存储**：每条消息先追加线程日志（`relay/chat/threads/<thread_id>/messages.jsonl`，事实源），再投递收件人邮箱（`to-<地址>/pending/`），v1 接收端零改动可用。
- **游标**：`relay/runtime/cursors/<会话>.json` 按线程记录已读位置，经 `chat_read.py --mark` 手动推进（hook 自动推进属宿主项目工作）。
- **presence / 预算 / 静音**：`presence.json`（由 `hooks/relay_hook.py` v4 实时写入：完整 session_id → short / model / last_seen / cwd）与 `session-registry.jsonl`（每行一条登记：ts / session_id / cwd / model）是推送解析地址依赖的两张登记表；另有每线程×每会话×每对话周期**自动回复上限 3 条**的持久化预算（`chat_state.py --budget-check` 执行）；`chat-mute.json` 静音开关暂停自动回复、不动历史与未读。
- **默认推送**：双写成功后 `chat_send.py` 经 `relay_push.push_text` 直推；结果以 `push=ok` / `push=failed:<detail>` 追加在 `OK` 行尾，推送失败不改退出码，`--no-push` 显式关闭。
- **唤醒降级（明确定义，不承诺无条件“未读即达”）**：服务器可达时，空闲对端由推送直接唤醒；不可达时，活跃会话在下一次 hook 事件时收到消息，空闲会话依赖值班 cron 或用户触发，跨厂商接收端（Codex / Claude Code 等）走人工粘贴降级路径（摘要工具把 pending 邮箱渲染为可粘贴正文）。relay 不向无法路由的客户端承诺推送可达。**TUI 会话的送达是异步的（2026-09-19 两次实测修正）**：server 托管会话被推送后立即开轮；TUI 终端里的会话也能收到——消息排队等当前回合结束，作为新用户回合出现，但**不体现在 server 端 `/messages` 视图**（勿以该视图判断 TUI 对端是否收到）。追求秒达可把领导会话也开在 `kimi web`；TUI 领导按到达的【relay-push】载荷走直推模式处理即可。
- 跨厂商信封桥（events.db→chat v2，仅显式 relay-chat 信封）为宿主项目自带工具，**不入模板**。

## 仓库结构

```
SKILL.md                     # /relay-up 技能（安装器，up/down 两模式）
hooks/relay_hook.py          # 会话 hook：SessionStart 注册 / presence 写入（v4）/
                             #   Stop 续接注入 / UPS 上下文注入（v3 标记路由）
tools/relay_push.py          # 推送车道：Kimi Code 服务器 REST 送达
                             #   （发现 / healthz / prompts；库 API + 独立 CLI）
tools/relay_spawn.py         # 员工 spawn 车道：server 托管会话
                             #   （建会话/profile 补写/读回校验/settle/roles.json 回填；
                             #   approve 代批；--delete 归档）
tools/chat_send.py           # chat 车道发送 CLI（v2：线程日志+邮箱双写，默认 --push）
tools/chat_read.py           # 线程查看：threads/转储/未读/游标/presence
tools/chat_state.py          # 共享状态：游标/presence/静音/预算（锁内原子合并）
tools/verify_report.py       # 领导验收七项核验
template/                    # 铺设到目标项目的骨架（信箱合同/员工技能/自举卡示例/
                             #   runtime 空壳，含 relay_push.py 在内的逐字节 tools/ 副本）
tests/                       # 标准库单测（领取/注入/合并/去重/隔离/门控/fail-open/
                             #   多项目路由 + chat v2 + push 套件）
check_template.py            # 模板完整性自检
```

## 已在真实环境验证的行为

Stop hook 续接注入（`{"decision":"block"}` 平台接受）、UserPromptSubmit `additionalContext` 上下文注入、每自然轮 3 次续写上限、fail-open、cron 定时自醒、双车道合并注入、坏消息隔离（`*.bad`）、基于标记的多项目路由。推送车道已经过本机服务器实测探针（2026-09）：建会话 → 推送 → 模型回 PONG-OK → 归档，全程信封 `code=0`。完整 spawn 协议已于 2026-09-19 端到端实测：spawn → 直推派工 → 员工领卡执行 → 九字段回报 → 领导 `verify_report.py` 7/7 PASS → SHUTDOWN → 归档，全程无 cron 无 hook——含已固化进 `relay_spawn.py` 的实测怪癖（创建时 `agent_config` 被忽略、model/permission 分两次 profile、15 秒 settle、静默 ~90 秒后用同一 `msg_id` 重投）。测试套件覆盖以上全部逻辑层；平台行为以 2026-09 的 ZCode 真实会话实测为准。

## 限制与路线图

- 推送依赖同机的 Kimi Code 本机服务器（`kimi web`）；没有它时中继完全运行在值班 cron + 信箱路径上——功能完整，只是不再即时。
- 单机 Windows；跨客户端（Codex/Claude/Kimi 当员工）需要各自的唤醒通道，是原项目的下一里程碑。
- 一键插件化分发在计划中；当前为复制到技能目录的安装方式。
- 不做后台模型调用——推送只唤醒已存在的会话，`relay_spawn` 也只在领导显式要求时才创建员工（仍然一个员工一个会话，绝不静默成群）；spawn 出的"员工"是 server 托管会话而非原生可见窗口，这是本项目的原则而非缺陷。

## 许可证

MIT

## 成本安全（v2 起）

值班循环内置防浪费治理：连续空转 2 轮自动降频为每小时、4 轮自动删除全部定时器；空闲检查优先脚本化；夜间值守默认关闭（须显式开启）；任何会话在执行"自动删除"前必须先实测本会话具备该工具，否则立即升级用户而非静默空转。推送本身已让多数值班唤醒不再必要；cron 治理保留作无服务器兜底。详见 relay 模板 runtime/loop-config.json 的 cost_safety 块。
