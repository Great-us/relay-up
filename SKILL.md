---
name: relay-up
description: 在任意项目启用或撤除 Session Relay（会话中继——Kimi Code 会话间任务卡+chat 双车道信箱：服务端直推即时唤醒 + cron/hook 文件层兜底 + relay_spawn 现场 spawn server 托管员工，含值班 cron 治理）。用户输入 /relay-up、或说 启用中继/装 relay/在这个项目装会话中继 时执行启用；输入 /relay-up down、或说 撤除中继 时执行撤除。Use when the user types /relay-up (enable) or /relay-up down (disable) in any project directory.
---

# relay-up — 会话中继一键启用/撤除

你是安装器。**当前工作目录 = 目标项目根**。模板在本技能目录 `template/` 下。铁律：只写 `./relay/`、`./.kimi-code/skills/relay-next/`、`./tools/chat_send.py`、`./tools/relay_push.py`、`./tools/relay_spawn.py`、`./tools/verify_report.py`、`./relay/relay.enabled`；已存在的文件一律**不覆盖**（恢复语义）；不碰用户级配置、不碰凭据、不动项目其他文件；拿不准就列出计划先问。

## UP（默认：启用/恢复）

1. 确认 cwd 是预期的项目根（`pwd`；若是用户主目录或盘根，停下向用户确认）。
2. 建目录：`relay/{inbox,claimed,outbox,archive/pending-review,chat,runtime}` 与 `.kimi-code/skills/`、`tools/`。
3. 逐项复制（源→目标，**目标已存在则跳过并计数**）：
   - `template/relay/README.md` → `relay/README.md`
   - `template/relay/chat/CONTRACT.md` → `relay/chat/CONTRACT.md`
   - `template/.kimi-code/skills/relay-next/SKILL.md` → `.kimi-code/skills/relay-next/SKILL.md`
   - `template/relay/runtime/{roles.json,leader-queue.json,loop-config.json}` → `relay/runtime/` 同名
   - `template/tools/chat_send.py` → `tools/chat_send.py`（chat 发送工具随项目铺设，默认 --push 服务端直推）
   - `template/tools/relay_push.py` → `tools/relay_push.py`（服务端直推工具随项目铺设）
   - `template/tools/relay_spawn.py` → `tools/relay_spawn.py`（员工 spawn 工具随项目铺设，server 托管会话，协议见 relay/chat/CONTRACT.md §员工会话 spawn 协议）
   - `tools/verify_report.py`（本包）→ `tools/verify_report.py`（领导验收工具）
   - `template/relay/bootstrap-card.example.json` → 仅展示给用户/领导参考，不直接投箱
4. 写启用标记 `relay/relay.enabled`（不存在时）：一行内容 `<项目文件夹名> <UTC日期>`。
5. 验证并汇报：目录树、新建/跳过计数、标记文件内容。
6. 打印下一步（三选一）：
   - **本会话当领导**：读 `relay/README.md` 与 `runtime/roles.json`，把任务写成 v1.1 卡投 `relay/inbox/`，参考 `bootstrap-card.example.json` 给员工窗发自举卡；派工/通知用 `tools/chat_send.py`（默认 `--push`，员工在线即达）；员工窗口也可不开，直接 `python tools/relay_spawn.py --root <根> --role employee-2 [--model 别名]` 现场 spawn server 托管员工（详见 relay/chat/CONTRACT.md §员工会话 spawn 协议）；
   - **本会话当员工**：领导投卡后，敲 `/relay-next` 取件（在线时领导消息经服务端直推即时到达，值班 cron 兜底）；
   - **员工值班 cron**：自举卡安装后每 5 分钟自动取件（离线兜底）。

## DOWN（参数 down：撤除）

1. `relay/` 整体重命名为 `relay.bak-<UTC时间戳>`（保留可恢复）。
2. 删除 `.kimi-code/skills/relay-next/`（若存在）；旧版 `.zcode/skills/relay-next/` 一并删除（若存在）。
3. 提醒用户：各会话用 CronList 检查并 CronDelete 标题含『relay 取件值班』或『relay 领导值班』的自动化；报告备份路径。

## 依赖与降级说明（如实告知用户）

- **服务端直推**（chat_send `--push` 默认开 / `tools/relay_push.py`）需要 Kimi Code 本机服务器（`kimi web`）在线：经 `~/.kimi-code/server/instances/*.json` 发现 + `~/.kimi-code/server.token` 鉴权；无服务器/对端不在线时推送自动降级——消息已在文件层落盘不受影响（发送行尾 `push=failed:<detail>` 仅提示送达未达），hook 注入与值班 cron 完整兜底。
- **自动快路径**（Stop 续接注入 / UPS 上下文注入）需要用户级 hooks 指向本包 `hooks/relay_hook.py`（注册方法见仓库 README；hook 按 `relay/relay.enabled` 标记路由，一份注册服务所有项目）。
- 未注册 hooks 且服务器不在线时**不影响可用性**：手动 `/relay-next` 与员工值班 cron 两条文件层路径完整可用（纯文件读写）。
- chat 双向、任务卡、验收（`tools/verify_report.py`）均为纯文件合同，天然跨项目；文件层是事实源，直推只是送达层。
