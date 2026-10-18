# AlterEgo Web UI · 接口与数据架构

> 后端：Flask（`ui/server.py`），默认 `http://localhost:5000`。
> 本机单机部署，无需鉴权；如需局域网手机访问，加 `Authorization: Bearer <token>`（token 存 Vault）。

---

## 1. 数据模型

```typescript
/** 会话 */
interface Session {
  id: string;            // "sess_9f2c"
  title: string;          // 会话标题（自动取首条消息前 12 字）
  updated_at: string;    // ISO8601
}

/** 消息（对话流基本单位） */
interface Message {
  id: string;                       // "msg_xxx"
  session_id: string;
  role: "user" | "agent";
  content: string;                  // 支持极简 markdown（代码块/行内 code）
  tool_calls?: ToolCall[];          // agent 消息可带
  approval_card?: ApprovalCard;     // agent 消息可内嵌一张审批卡
  attachments?: Attachment[];       // 用户上传的附件
  created_at: string;               // ISO8601，前端显示 HH:MM
}

interface ToolCall {
  tool: string;                     // "file_read"
  args: Record<string, unknown>;    // 参数原文
  status: "ok" | "error" | "running";
  duration_ms?: number;
}

interface Attachment {
  name: string;          // "截图.png"
  size: number;          // bytes
  mime: string;          // "image/png"
  url: string;           // "/uploads/<id>"，前端直接 <img src>
}

/** 审批卡（§36 三级粒度） */
interface ApprovalCard {
  approval_id: string;              // "appr_9f2c"
  action: string;                   // "发布视频到 Bilibili"
  detail: string;                   // 参数详情（\n 分行）
  risk_level: "L1" | "L2" | "L3";
  risk_note?: string;               // "对外发布，不可撤回"
  /** 前端可勾选的批准粒度；L3 固定 ["once","session"]，不给 always */
  scope_options: ("once" | "session" | "always")[];
  requested_at: string;             // ISO8601
  expires_at: string;               // ISO8601，超时自动拒绝（fail closed）
  status: "pending" | "approved" | "rejected" | "expired";
}

/** 运行中任务（右栏） */
interface TaskStatus {
  task_id: string;
  name: string;          // "上传并发布到 Bilibili"
  detail?: string;       // "正在执行发布流程…"
  progress: number;      // 0-100
  eta_sec?: number;      // 预计剩余秒
  status: "running" | "paused_takeover" | "done" | "failed";
  steps: TaskStep[];     // 点开任务弹窗时的实时步骤清单（spec §31.6）
}

/** 任务步骤（弹窗用，随 task_update SSE 全量推送） */
interface TaskStep {
  step_id: string;
  name: string;          // "加载工具命名空间：credentials"（给人看的粒度）
  status: "pending" | "running" | "done" | "failed" | "skipped";
  started_at?: string;
  ended_at?: string;
  detail?: string;       // 失败时写原因
}

/** 接管状态（浮窗轮询用） */
interface TakeoverStatus {
  task_id: string;
  target: string;        // "Chrome" | "Recordly" | 桌面程序名
  status: "active" | "completed" | "cancelled" | "timeout";
  elapsed_sec: number;
}
```

---

## 2. REST 接口

### 2.1 会话

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 会话列表（按 updated_at 倒序） |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions/<id>/messages?limit=50&before=<msg_id>` | 消息历史（分页） |
| DELETE | `/api/sessions/<id>` | 删除会话 |

**POST /api/sessions**
```json
// Request: {} （空即可，标题自动生成）
// Response 200:
{ "id": "sess_9f2c", "title": "新会话", "updated_at": "2026-10-10T20:31:00+08:00" }
```

### 2.2 聊天

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 发消息 |

**POST /api/chat**
```json
// Request:
{
  "session_id": "sess_9f2c",
  "content": "帮我把这段视频发布到B站",
  "attachments": ["upload_id_1"]   // 可选，先调 /api/uploads 拿 id
}
// Response 200（ack；agent 回复走 SSE 的 message 事件推送）:
{
  "message": { "id": "msg_u1", "role": "user", "content": "帮我把这段视频发布到B站", "created_at": "..." }
}
// 错误：
// 400 { "error": "empty_content" }   429 { "error": "agent_busy", "retry_after": 5 }
```

### 2.3 审批（§36）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/approvals/pending` | 右栏待审批队列 |
| POST | `/api/approvals/<id>/resolve` | 通过 / 拒绝 |

**POST /api/approvals/`<id>`/resolve**
```json
// Request:
{
  "approved": true,
  "scope": "once"        // "once" | "session" | "always"
                         // L3 传 "always" 会被后端拒绝（400 invalid_scope）
}
// Response 200:
{ "approval_id": "appr_9f2c", "approved": true, "scope": "once" }
// 错误：
// 404 { "error": "not_found" }        卡片不存在
// 410 { "error": "expired" }         已超时自动拒绝，前端置灰
// 400 { "error": "invalid_scope" }   L3 不允许 always
```

### 2.4 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks/running` | 右栏运行中任务 |
| POST | `/api/tasks/<id>/cancel` | 取消任务 |

### 2.5 接管（§15 + §31.5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/takeover/<task_id>/done` | 浮窗[完成] |
| POST | `/api/takeover/<task_id>/cancel` | 浮窗[取消] |
| GET | `/api/takeover/<task_id>/status` | 浮窗轮询（5s） |

```json
// GET /api/takeover/<task_id>/status → 200:
{ "task_id": "t1", "target": "Chrome", "status": "active", "elapsed_sec": 323 }
// status 非 active 时浮窗自动关闭
```

### 2.6 上传

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/uploads` | multipart/form-data，返回 `{"upload_id": "...", "url": "/uploads/..."}` |

---

## 3. 实时事件（SSE）

```
GET /api/events          # Content-Type: text/event-stream
```

| event | data | 触发时机 |
|---|---|---|
| `message` | `Message` | agent 回复（流式时每段一个，可带 tool_calls） |
| `approval_request` | `ApprovalCard` | 需要用户审批 → 对话流插入卡片 + 右栏队列 + toast |
| `approval_resolved` | `{approval_id, approved, scope}` | 用户在任一端 resolved 后广播（防多开页面状态不一致） |
| `task_update` | `TaskStatus` | 任务进度/状态变化 → 右栏更新进度条 |
| `takeover_request` | `{task_id, target}` | Agent 请求接管 → 后端自动弹原生浮窗（pywebview），Web 页只 toast 提示 |

断线重连：浏览器 EventSource 自动重连；后端实现 `Last-Event-ID` 续传（可选 MVP 可不做）。

---

## 4. 核心流程时序

### 4.1 发消息 → 审批 → 执行
```
用户        前端JS           Flask后端            Agent
 │ POST /api/chat            │                    │
 ├─────────►│─────────►      │                    │
 │          │          run_turn()                 │
 │          │               ├───────────────────► │
 │          │  SSE: message（"需要你确认" + approval_card）
 │          │◄──────────────┤                    │
 │  点[通过]（scope=session）│                    │
 │ POST /api/approvals/appr_9f2c/resolve          │
 ├─────────►│─────────►      │                    │
 │          │  SSE: approval_resolved             │
 │          │◄──────────────┤                    │
 │          │               │  继续执行工具…       │
 │          │  SSE: message（执行结果）            │
 │          │◄──────────────┤                    │
```

### 4.2 接管流程
```
Agent → 后端: takeover_request(task_id, target="Chrome")
后端 → pywebview: 弹无边框置顶窗加载 takeover.html?task_id=..&target=..
用户: 在真正的 Chrome 里扫码登录
用户 → 点浮窗[完成] → POST /api/takeover/<id>/done
后端 → event_bus: takeover_done → Agent 从 paused_takeover 恢复
Agent: 重新截图 + 读无障碍树（§15.6-3）→ 继续 running
浮窗: 收到 done 响应后自动关闭
```

### 4.3 审批超时（fail closed）
```
后端: expires_at 到达 → status=expired → SSE approval_resolved{approved:false}
前端: 卡片置灰显示"已超时，自动拒绝"，右栏队列移除
```

---

## 5. 页面 ↔ 接口映射

| 页面 | 初始加载 | 实时更新 |
|---|---|---|
| `index.html` 左栏会话 | `GET /api/sessions` | —（新建会话后重拉） |
| `index.html` 对话流 | `GET /api/sessions/<id>/messages` | SSE `message` / `approval_request` |
| `index.html` 右栏审批 | `GET /api/approvals/pending` | SSE `approval_request` / `approval_resolved` |
| `index.html` 右栏任务 | `GET /api/tasks/running` | SSE `task_update` |
| `takeover.html` 浮窗 | URL 参数 `task_id` `target` | 轮询 `GET /api/takeover/<id>/status`（5s）+ 按钮调 done/cancel |

---

## 6. 前端演示模式

后端未启动时（`fetch` 失败），`app.js` 自动进演示模式：
右下角 toast 提示"进入演示模式"，用 `MOCK` 数据渲染布局原型（含审批卡交互、倒计时）。
**演示模式不写任何真实数据**，仅用于看布局。接后端后自动恢复真实模式。
