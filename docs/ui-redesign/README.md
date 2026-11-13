# AlterEgo UI 重设计包（对标 Muse）

> 起因：2026-10-12 用户反馈——桌面端 UI 与 Muse 相比"一个地下一个天上"，
> 且聊天区"基本什么都往外倒"（截图实证：tool_call 原始 JSON 直接显示在气泡里）。

## 文件清单

| 文件 | 内容 |
|---|---|
| `ui-redesign.md` | 布局重设计：三栏对标 Muse、右侧 4 个 tab 详规、头像实时动画状态机 |
| `event-protocol.md` | 前后端数据传递：SSE 事件表（payload schema + 后端埋点位置），头像动画与 tab 进度全靠它驱动 |
| `display-rules.md` | 显示/隐藏规则矩阵 + 拿用户截图里的两个反例做 before/after |

## 执行顺序（给反重力）

1. 先 `event-protocol.md` —— 后端埋点（orchestrator 发事件），这是地基；
   头像动画和 4 个 tab 的实时进度全靠这些事件驱动，没有它前端做不出"实时"。
2. 再 `display-rules.md` —— 聊天区渲染规则，先把 raw JSON 倒灌修掉（见效最快）。
3. 最后 `ui-redesign.md` —— 布局与 4 tab + 头像动画。

## 关键依赖说明

- 实时头像动画**不需要**等 F4/B1 的 job queue 改造：`/api/events` 的 SSE 是独立端点，
  同步的 `/api/chat` 在执行中途照样可以 `event_bus.broadcast`。先埋点就有实时效果；
  job queue 解决的是并发 scalability，另算。
- `display-rules.md` 是纯前端改动，可独立先上。
