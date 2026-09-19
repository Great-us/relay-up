# Session Relay（relay-up）

**[English](README.md) | 简体中文**

**一个领导，多个员工，多个模型——ZCode 会话间的全自动任务中继。** 在任意项目敲 `/relay-up` 即装；员工窗口定时自醒取件；领导审阅回报、派发新任务，循环直到做完为止。中转全程只靠文件读写，不调用任何额外模型 API。

> 源自 Model Relay 项目 TASK-010/011 的实战：在一台 Windows 机器上用真实会话端到端验证了整条链路（事件 hook → 文件信箱 → 定时唤醒 → 验收归档），原文往返逐字哈希核对通过。

## 它解决什么问题

你在 ZCode 里开了好几个窗口（不同模型：贵的当领导、便宜的当员工）。让它们协作 ordinarily 要靠你人工复制粘贴。Session Relay 把“派工 → 执行 → 回报 → 审阅 → 再派工”变成全自动：

```
领导会话（任意模型）
  │ ① 写任务卡（relay-task v1.1，含 SHA256 与写入授权）→ relay/inbox/
  │    ＋ chat 定向消息 → relay/chat/
  ▼
员工会话（便宜模型窗口，各自装 5 分钟值班 cron）
  │ ② cron 醒来自查信箱 → 原子领卡 → 排空式执行（一轮连做所有待办）
  │ ③ 回报 relay/outbox/（九字段合同）＋ chat 主动通知领导
  ▼
领导（本人或 10 分钟值班 cron）
  │ ④ verify_report.py 七项机械核验 → 归档 / 返工卡(-R1)
  │ ⑤ 下发新任务 → 回到 ①
  ▼
队列空 & 全部验收 → 自动降频值守
  （连续空闲可自动关停员工定时器；一句话恢复）
```

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

   未注册也不影响使用：手动 `/relay-next` 与员工值班 cron 两条路径只依赖文件读写。v3 起**一份注册服务所有项目**——hook 按 `/relay-up` 写入的 `relay/relay.enabled` 标记自动路由。
3. **启用**：在任意项目的 ZCode 窗口敲 `/relay-up`（撤除：`/relay-up down`）。
4. **开员工窗**：新开 ZCode 窗口选个便宜模型，发任意一条消息唤醒，再按 `template/relay/bootstrap-card.example.json` 给它自举卡（装值班 cron）；此后每 5 分钟自动取件，无需看管。

## 安全设计（为什么敢让它无人值守）

- **fail-open**：hook 任何异常一律空输出退出，绝不阻塞你的会话。
- **原子领取**：全部抢占用同卷 `rename`，两个员工抢同一张卡只有一个成功。
- **双哈希核验**：任务卡 `prompt_sha256` 与回报 `report_sha256` 逐字校验（不 trim、不归一化换行），篡改/损坏即拒收隔离。
- **写入授权边界**：每张卡自带 `authorized_write_paths`，卡内任何越界指令（改共享文档、读凭据、外联、动 git）一律拒绝并记录——**prompt 是数据不是指令**。
- **续写链上限**：每自然轮最多 3 次连续自动唤醒（平台规则），值班 cron 每次唤醒都是新自然轮，“排空式执行”保证轮内不限量——续航与防失控兼得。
- **终局治理**：领导可随时关停/删除/降频全部定时器；无人值守时持续空闲自动降频值守，保留一句话恢复能力。

## chat 车道 v2：线程、游标、预算、静音

任务卡车道之外，relay-up 还内置会话间 chat 车道（`tools/chat_send.py`、`tools/chat_read.py`、`tools/chat_state.py`，合同见 `template/relay/chat/CONTRACT.md` §v2.0）：

- **双层存储**：每条消息先追加线程日志（`relay/chat/threads/<thread_id>/messages.jsonl`，事实源），再投递收件人邮箱（`to-<地址>/pending/`），v1 接收端零改动可用。
- **游标**：`relay/runtime/cursors/<会话>.json` 按线程记录已读位置，经 `chat_read.py --mark` 手动推进（hook 自动推进属宿主项目工作）。
- **presence / 预算 / 静音**：`presence.json`（schema）；每线程×每会话×每对话周期**自动回复上限 3 条**的持久化预算（`chat_state.py --budget-check` 执行）；`chat-mute.json` 静音开关暂停自动回复、不动历史与未读。
- **唤醒降级（明确定义，不承诺无条件“未读即达”）**：活跃会话在下一次 hook 事件时收到消息；空闲会话依赖值班 cron 或用户触发；跨厂商接收端（Codex / Claude Code 等）走人工粘贴降级路径（摘要工具把 pending 邮箱渲染为可粘贴正文）。relay 不向任意客户端承诺推送可达。
- 跨厂商信封桥（events.db→chat v2，仅显式 relay-chat 信封）为宿主项目自带工具，**不入模板**。

## 仓库结构

```
SKILL.md                     # /relay-up 技能（安装器，up/down 两模式）
hooks/relay_hook.py          # 会话 hook：SessionStart 注册 / Stop 续接注入 / UPS 上下文注入（v3 标记路由）
tools/chat_send.py           # chat 车道发送 CLI（v2：线程日志+邮箱双写）
tools/chat_read.py           # 线程查看：threads/转储/未读/游标/presence
tools/chat_state.py          # 共享状态：游标/presence/静音/预算（锁内原子合并）
tools/verify_report.py       # 领导验收七项核验
template/                    # 铺设到目标项目的骨架（信箱合同/员工技能/自举卡示例/runtime 空壳）
tests/                       # 标准库单测（领取/注入/合并/去重/隔离/门控/fail-open/多项目路由 + chat v2 套件）
check_template.py            # 模板完整性自检
```

## 已在真实环境验证的行为

Stop hook 续接注入（`{"decision":"block"}` 平台接受）、UserPromptSubmit `additionalContext` 上下文注入、每自然轮 3 次续写上限、fail-open、cron 定时自醒、双车道合并注入、坏消息隔离（`*.bad`）、基于标记的多项目路由。测试套件覆盖以上全部逻辑层；平台行为以 2026-09 的 ZCode 真实会话实测为准。

## 限制与路线图

- 单机 Windows；跨客户端（Codex/Claude/Kimi 当员工）需要各自的唤醒通道，是原项目的下一里程碑。
- 一键插件化分发在计划中；当前为复制到技能目录的安装方式。
- 不做后台模型调用——“员工”始终是原生可见窗口，这是本项目的原则而非缺陷。

## 许可证

MIT

## 成本安全（v2 起）

值班循环内置防浪费治理：连续空转 2 轮自动降频为每小时、4 轮自动删除全部定时器；空闲检查优先脚本化；夜间值守默认关闭（须显式开启）；任何会话在执行"自动删除"前必须先实测本会话具备该工具，否则立即升级用户而非静默空转。详见 relay 模板 runtime/loop-config.json 的 cost_safety 块。
