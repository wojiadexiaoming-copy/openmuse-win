# 更新日志 (Changelog)

所有针对 OpenMuse-Win (AlterEgo) 的版本演进与关键更新均记录在此。

---

## [v1.1.0] - 2026-11-14 (BoHack 2026 黑客松 42 小时攻坚冲刺版)

> **基线版本**：v1.0.0 (Tag `v1.0.0`, 2026-10-18 提交的赛前报名版本)  
> **分支名称**：`v1.1-20261114`  
> **核心成果**：42 小时内完成净增 **+273 个源码与文档文件**、**+41,869 行核心代码**、**+30 个测试套件 / 1,600+ 项测试用例**。  
> **详细对照报告**：详见 [`docs/BoHack_2026_42h_Increment_Report.md`](docs/BoHack_2026_42h_Increment_Report.md)。

### 🌟 新增与核心特性 (Features)

#### 1. 多模型角色分级绑定与可视化路由矩阵 (Per-Role Model Binding)
- 支持 **主脑 (Main Brain)**、**后台工作者 (Worker)**、**子任务智能体 (Subagent)**、**任务规划器 (Planner)**、**容灾兜底 (Fallback)** 5 级角色独立绑定不同模型。
- 前端设置弹窗提供实时下拉菜单切换，即时热重载底层客户端，无需重启软件。
- 智能感知多模型服务商协议（OpenAI 兼容 / 百炼 MaaS / Google Gemini / 本地 EchoBird），自动匹配对应 API Key。

#### 2. 多级容灾降级备用链 (Cascading Fallback Chain)
- 支持配置任意级数的大模型故障转移链（如 Agnes -> Qwen -> Kimi -> Gemini）。
- 主模型遇到 429 速率限制、服务过载或网络中断时，自动按顺位递归降级尝试，全部失败才阻断报错。
- 彻底解决并发与时序竞争：添加/切换备用模型防连击、瞬态同步、防服务端旧数据覆写、即时磁盘 `fsync` 物理提交。

#### 3. 行为设置全参数动态调整与持久化 (Behavior Settings & Governance)
- **审批超时时间 (Approval Timeout)**：支持 1 / 3 / 5 / 10 / 15 / 30 分钟动态下拉调整，后端 `ApprovalManager.timeout_sec` 即时热生效，超时严格按 fail-closed 自动拒绝。
- **危险动作默认授权粒度 (Hazardous Default Scope)**：支持 本会话 (session) / 仅本次 (once) / 始终允许 (always) 参数调整，与审批弹窗预选按钮动态联动。
- **托盘常驻与自动降级**：双独立开关状态绑定，全量配置持久化安全写入 `config.yaml`。

#### 4. 接口与密钥在线管理与编辑 (API Key & Endpoint Editing)
- 已启用的接口与预设接口支持随时点击卡片进入参数调整弹窗。
- 支持回填服务名称、Base URL、脱敏 Key、支持模型列表与上下文长度，支持保存调整与安全删除。

#### 5. 架构可靠性与多进程沙箱隔离 (Architecture Hardening & Sandbox)
- **子进程硬隔离 (`agent/subagent.py`)**：引入 `multiprocessing` 独立进程沙箱，单进程异常崩溃或死循环不影响主进程。
- **内存配额看门狗**：动态监控子进程内存，超时或超额自动熔断回收。
- **会话检查点 16 倍性能提升 (`agent/session_store.py`)**：引入增量签名水位（Watermark Cache），消除了长会话下的 $O(N^2)$ 保存性能损耗。

#### 6. 复杂 SPA 浏览器原生事件链模拟与瞬态 SoM (Browser Worker Overhaul)
- **瞬态 SoM (Set-of-Mark) 标注**：视觉编号框采用瞬态注入并截前回显，点击完成后自动淡出，不遮挡二次交互。
- **完整原生事件链派发**：补全 `focus -> keydown -> textInput -> input -> change -> blur` 全套原生事件派发，支持现代 SPA (React / Vue / Angular) 内部双向绑定探针。
- **单写者租约与防死循环熔断**：多智能体并发时引入 Single-Writer Lease，防止多个 Agent 抢夺同一页面。

#### 7. 微信 ClawBot 双向通信与移动端授权闭环 (WeChat ClawBot Channel)
- 原生微信扫码绑定，支持移动端长轮询与无感同步。
- 电脑端触发高危操作（文件删除、终端执行、大额调用）时，手机微信秒级推送审批卡片，手机回复“同意/拒绝”即可完成远程审批。

#### 8. 用量看板与历史 Token 目录价折算 (Token Usage Dashboard)
- 启动时自动从 `conversations.db` 预加载历史累计 Token 用量，彻底解决开机显示 0 的问题。
- 支持近 7 天 / 近 30 天 / 全部自由切换，展示 35 格 GitHub 风格每日活跃热力图。
- 自动按官方目录价折算实际开销，清晰透明。

#### 9. 自主任务长程编排与开机断点续接 (Task Resume & Orchestration)
- 软件重启后自动扫描最新会话状态，若发现未闭环的任务，UI 顶部弹出恢复通知条，支持用户一键“继续执行”或“放弃提醒”。
- 严格区分 `done`、`incomplete`、`waiting_user` 终态，杜绝智能体虚假汇报完成。

#### 10. 自主进化与经验记忆蒸馏 (Memory Distiller & Skill Review)
- 高成功率交互（连续成功 2 次以上）自动抽象为标准化 Reusable Skill 技能包。
- 晚间后台自动启动 Review 流程，去重、修剪冲突与失效技能，实现终身记忆自愈。

---

## [v1.0.0] - 2026-10-18 (赛前基线版本)

- 初始开源发布（Tag `v1.0.0`, commit `f23fca2`）。
- 自研 ReAct 编排层、不可变活体句柄 `[ref=aeN]` 浏览器控制。
- Windows 原生 UIA 桌面控制与 Windows DPAPI 凭据保险箱。
- 基础 Web 控制台与单轮调用。
