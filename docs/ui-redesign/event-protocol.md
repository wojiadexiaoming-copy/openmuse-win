# 前后端数据传递协议：SSE 事件表

> 目标：头像实时动画、右侧 4 tab 的进度、审批红点，**全部**由下面这些事件驱动。
> 前端不轮询、不猜状态；后端在正确的位置 `broadcast`，前端只做渲染。

## 1. 事件总览

现有：`approval_request`、`approval_resolved`、`message`（已在 `/api/events`）。
新增（必须）：

| 事件 | 驱动 | 触发时机 |
|---|---|---|
| `agent_state` | 头像动画+状态文字、tab 红点 | Agent 状态每次变化 |
| `plan_update` | Tab 1 任务 | Planner 产出 plan / 每步状态变化 |
| `tool_call_started` | Tab 3 动态 | 工具开始执行前 |
| `tool_call_done` | Tab 3 动态 | 工具执行结束（成功/失败） |
| `subagent_update` | Tab 3 动态（子 Agent 分组） | 子 Agent 状态变化/完成 |
| `memory_write` | Tab 4 状态 | 记忆管线写入一条记忆 |
| `token_usage` | Tab 4 状态 | 每轮 LLM 调用返回后（累计值） |

## 2. Payload Schema（JSON，`event:` 名 + `data:`）

```jsonc
// agent_state —— 核心事件，头像和 tab 都听它
{ "state": "idle|thinking|acting|awaiting_approval|error|done",
  "detail": "正在执行：读取文件 src/main.py",   // 人话，后端拼好
  "tool": "file_read",                            // acting 时才有
  "model": "qwen2.5-coder-32b",                   // 当前实际用的模型
  "retry": 2 }                                    // error 时才有： 第几次重试

// plan_update
{ "steps": [ { "id": "s1", "title": "搜索文档", "status": "pending|running|done|failed" } ] }

// tool_call_started
{ "id": "tc_8f3a", "tool": "browser_navigate",
  "summary": "正在打开网页 github.com" }          // 人话摘要，后端拼

// tool_call_done
{ "id": "tc_8f3a", "tool": "browser_navigate",
  "status": "ok|error", "duration_ms": 1234,
  "result_summary": "返回 200，页面标题 GitHub" }  // 一句话结果，不许全文

// subagent_update
{ "id": "sa_1", "brief": "搜索 MCP 文档",
  "status": "running|done|failed",
  "result_summary": "找到 3 篇相关文档" }          // done 时才有

// memory_write
{ "kind": "fact|preference|lesson", "text": "用户偏好深色 UI" }

// token_usage —— 累计值，前端直接显示
{ "input": 12345, "output": 678 }
```

## 3. 后端埋点位置（给反重力：精确到函数）

在 `server/app.py` 现有 `event_bus` 上加 `broadcast(name, payload)`（已有 approval 用法，复用）。

| 埋点 | 文件:函数 | 说明 |
|---|---|---|
| thinking | `core/orchestrator.py: run_turn()` | 调用 LLMClient.chat 之前发 `thinking` |
| acting | `core/orchestrator.py: _exec_tools()` | 每个 tool_call 执行前发 `acting`（detail 用 ACTION_CN_MAP 拼人话） |
| awaiting_approval | `core/orchestrator.py` 审批触发处 | 发审批请求时**同时**发 `agent_state=awaiting_approval`（detail=待批事项人话） |
| done / error | `core/orchestrator.py: run_turn()` | 正常结束发 `done`；异常/retry 发 `error`（带 retry 次数） |
| idle | `core/orchestrator.py` | 审批 resolve 后、每轮开始前回到 `idle` |
| plan_update | Planner 产出 plan 后 + 每步执行前后 | steps 数组全量下发，前端整单替换（简单可靠） |
| tool_call_* | `core/orchestrator.py: _exec_tools()` | started 在鉴权/执行前，done 在拿到结果后；`result_summary` 取结果前 60 字人话化 |
| subagent_update | `core/subagents.py: SubagentManager` | 状态机变化处（现有回调点） |
| memory_write | 记忆管线写入处（`core/memory_pipeline.py` 或后台线程） | 每写一条发一条 |
| token_usage | `core/llm_client.py: chat()` 返回后 | 累加到 session 级计数器再 broadcast |

### 人话模板集中管理
新建 `core/state_text.py`：
```python
ACTION_CN = {"file_read": "正在读取文件 {path}", "shell_exec": "正在执行命令 {cmd}",
             "browser_navigate": "正在打开网页 {domain}", ...}
def tool_summary(tool, args) -> str: ...
def approval_summary(action, args) -> str: ...
```
前端**不许**自己拼人话，只显示 `detail`/`summary` 字段。

## 4. 前端接入（`app.js`）

- 现有 `EventSource('/api/events?token=...')` 上加 7 个新事件的 listener（复用现有连接，不开新连接）。
- 状态存储：`UI.store = { agentState, plan: [], activity: [], approvals: [], status: {...} }`；
  事件 handler 只写 store + 触发对应 tab/头像的 render 函数。
- `agent_state` handler：切换头像 class + 状态文字（300ms 节流）+ 审批红点逻辑。
- 重连：EventSource 自带重连；重连后前端发 `GET /api/state/snapshot`（新增接口，
  一次性返回当前 plan/activity/approvals/status 全量）补齐断线期间的事件——
  **这个快照接口必须做**，否则刷新页面后右侧 tab 全空。

## 5. 验收标准

1. 后端跑一轮"打开计算器"任务，前端头像依次出现 thinking→acting（≥2 次）→done，
   状态文字每次都是人话、无 JSON。
2. Tab 3 里每条工具调用可点开看到参数（折叠 JSON），耗时显示正确。
3. 触发一次审批：头像变橙闪烁、Tab 2 自动切过来+红点、状态文字是人话。
4. 刷新页面：右侧 4 tab 通过 snapshot 接口恢复，不空白。
5. 全程聊天区**不出现**任何 `{"name": ...}` 裸 JSON（见 display-rules.md）。
