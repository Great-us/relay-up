# Session Relay（relay-up）

**一个领导，多个员工，多个模型——ZCode 会话间的全自动任务中继。** 在任意项目敲 `/relay-up` 即装；员工窗口定时自醒取件；领导审阅回报、派发新任务，循环直到做完为止。中转全程只靠文件读写，不调用任何额外模型 API。

> 本项目源自 [Model Relay](https://github.com/) 的 TASK-010/011 实战：在一台 Windows 机器上用真实会话逐字验证了整条链路（事件 hook → 文件信箱 → 定时唤醒 → 验收归档）。

## 它解决什么问题

你在 ZCode 里开了好几个窗口（不同模型：贵的当领导、便宜的当员工）。让它们协作 ordinarily 要靠你人工复制粘贴。Session Relay 把"派工 → 执行 → 回报 → 审阅 → 再派工"变成全自动：

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
队列空 & 全部验收 → 自动降频值守（连续空闲可自动关停员工定时器；一句话恢复）
```

## 快速开始

1. **安装技能**：把本仓库放入用户级技能目录（Windows：`%USERPROFILE%\.agents\skills\relay-up\`，即本 README 所在目录整体拷入）。
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

   未注册也不影响使用：手动 `/relay-next` 与员工值班 cron 两条路径只依赖文件读写。
3. **启用**：在任意项目的 ZCode 窗口敲 `/relay-up`（撤除：`/relay-up down`）。
4. **开员工窗**：新开 ZCode 窗口选个便宜模型，发任意一条消息唤醒，再按 `template/relay/bootstrap-card.example.json` 给它自举卡（装值班 cron）；此后每 5 分钟自动取件，无需看管。

## 安全设计（为什么敢让它无人值守）

- **fail-open**：hook 任何异常一律空输出退出，绝不阻塞你的会话。
- **原子领取**：全部抢占用同卷 `rename`，两个员工抢同一张卡只有一个成功。
- **双哈希核验**：任务卡 `prompt_sha256` 与回报 `report_sha256` 逐字校验（不 trim、不归一化换行），篡改/损坏即拒收隔离。
- **写入授权边界**：每张卡自带 `authorized_write_paths`，卡内任何越界指令（改共享文档、读凭据、外联、动 git）一律拒绝并记录——prompt 是数据不是指令。
- **续写链上限**：每自然轮最多 3 次连续自动唤醒（平台规则），值班 cron 每次唤醒都是新自然轮，"排空式执行"保证轮内不限量——续航与防失控兼得。
- **终局治理**：领导可随时关停/删除/降频全部定时器；无人值守时连续空闲自动降频值守，保留一句话恢复能力。

## 仓库结构

```
SKILL.md                     # /relay-up 技能（安装器，up/down 两模式）
hooks/relay_hook.py          # 会话 hook：SessionStart 注册 / Stop 续接注入 / UPS 上下文注入
tools/chat_send.py           # chat 车道发送 CLI
tools/verify_report.py       # 领导验收七项核验
template/                    # 铺设到目标项目的骨架（信箱合同/员工技能/自举卡示例/runtime 空壳）
tests/                       # 标准库单测（22 项：领取/注入/合并/去重/隔离/门控/fail-open）
check_template.py            # 模板完整性自检
```

## 已在真实环境验证的行为

Stop hook 续接注入（`{"decision":"block"}` 平台接受）、UserPromptSubmit `additionalContext` 上下文注入、每自然轮 3 次续写上限、fail-open、cron 定时自醒、双车道合并注入、坏消息隔离（`*.bad`）。测试套件覆盖以上全部逻辑层；平台行为以 ZCode 实测为准。

## 限制与路线图

- 单机 Windows；跨客户端（Codex/Claude/Kimi 当员工）需要各自的唤醒通道，见原项目路线。
- hook 多项目标记路由（`relay/relay.enabled`）为 v3 方向：使一份用户级 hooks 服务所有项目。
- 不做后台模型调用——"员工"始终是原生可见窗口，这是本项目的原则而非缺陷。

## License

MIT
