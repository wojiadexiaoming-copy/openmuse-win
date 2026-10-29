# OpenMuse-Win

<div align="center">

**面向 Windows 原生环境的工业级自主 AI 智能体（Autonomous AI Agent）**

*自研极简 ReAct 编排层 · 浏览器 ref 活体句柄 & SoM 框选 · Windows 桌面 UIA Pattern 直调 · 人机协同分屏*

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23446166.svg)](https://doi.org/10.5281/zenodo.23446166)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6.svg?logo=windows)](https://www.microsoft.com/windows)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python)](https://www.python.org)

</div>

---

## 🌟 项目简介

**OpenMuse-Win** 是一套专为 **Windows 10 / 11** 原生环境打造的端到端单机自主 AI Agent 操作系统。

与市场上基于 LangChain、CrewAI 或 AutoGen 等高封装框架的二次包装项目不同，OpenMuse-Win 采用 **0 依赖从零自研的轻量 ReAct 调度核心**，致力于彻底解决大模型在驱动真实操作系统与现代富交互应用（SPA）时的两大致命痛点：
1. **浏览器内操作脆弱**：动态类名混淆、滚动坐标漂移、浮层元素误击穿、反爬风控拦截；
2. **桌面控制侵入性强**：无脑抢夺人类物理鼠标、光标乱晃、UAC 权限穿透失控、破坏性指令无防线。

OpenMuse-Win 在架构底层构建了**「浏览器不可变 ref 句柄 + Set-of-Mark (SoM) 框选」**与**「Windows 桌面 UIA Pattern 四层交互阶梯」**，配合严格的 **L1~L3 三级安全审批与物理光标让出机制**，实现了真正安全、拟人、确定性闭环的人机协同自动化。

---

## 🎬 真实评测 | Real-World Evaluation

2026-10-29 实拍，三段任务连续完成，均含步骤字幕。
Recorded on Oct 29, 2026: three tasks completed in one continuous session, with step subtitles burned in.

以下实拍即本项目运行画面（论文名 AlterEgo）。
Footage below shows this project in action (paper name: AlterEgo).

<table>
  <tr>
    <td align="center" width="33%">
      <video controls width="100%" src="https://alterego.inodetree.com/AlterEgo-demo-1-trip.mp4"></video><br/>
      <b>1. 规划北京到美国 7 天旅行 | 7-Day Beijing–US Trip Planning</b>（1:55）<br/>
      查机票、比价、选日期 · Flights, prices, dates<br/>
      <a href="https://alterego.inodetree.com/AlterEgo-demo-1-trip.mp4">直接看 | Direct link</a>
    </td>
    <td align="center" width="33%">
      <video controls width="100%" src="https://alterego.inodetree.com/AlterEgo-demo-2-poster.mp4"></video><br/>
      <b>2. 豆包生成 2027 元旦海报 | 2027 New Year Poster via Doubao</b>（0:45）<br/>
      红金配色、立体数字 · Red-gold, 3D numerals<br/>
      <a href="https://alterego.inodetree.com/AlterEgo-demo-2-poster.mp4">直接看 | Direct link</a>
    </td>
    <td align="center" width="33%">
      <video controls width="100%" src="https://alterego.inodetree.com/AlterEgo-demo-3-desktop.mp4"></video><br/>
      <b>3. 桌面文件夹与 Markdown 文档 | Desktop Folder &amp; Markdown File</b>（1:39）<br/>
      新建文件夹、写文档、L2 审批 · Folder, doc, L2 approval<br/>
      <a href="https://alterego.inodetree.com/AlterEgo-demo-3-desktop.mp4">直接看 | Direct link</a>
    </td>
  </tr>
</table>

---

## 📑 学术论文与技术报告 (Paper & Citation)

本项目实证研究与系统架构论文已正式在 **Zenodo** 公开发布：

> **AlterEgo: Towards the Zero-Cost Personal Agent**  
> *Shihaoming (Independent Researcher)*  
> 🏷️ **DOI**: [10.5281/zenodo.23446166](https://doi.org/10.5281/zenodo.23446166)  
> 🌐 **Zenodo 归档**: [https://zenodo.org/records/23446166](https://zenodo.org/records/23446166)  
> 📄 **论文 PDF 直达**: [alterego-paper.pdf](https://zenodo.org/records/23446166/files/alterego-paper.pdf?download=1)

如果您在学术研究、系统复现、工程开发或技术报告中参考了本项目的系统架构与设计理念，请引用本论文：

```bibtex
@article{shi2026alterego,
  title={AlterEgo: Towards the Zero-Cost Personal Agent},
  author={Shi, Haoming},
  year={2026},
  publisher={Zenodo},
  doi={10.5281/zenodo.23446166},
  url={https://doi.org/10.5281/zenodo.23446166}
}
```

---

## 🏛️ 全景技术架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          人机协同控制层 (User Layer)                         │
│   Web 协同工作台 (Bottle + SSE)   │  Windows 原生 Toast 通知  │  微信远程通道 │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                        核心编排与认知层 (Cognitive Engine)                    │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ 角色路由引擎 (Role Router): Main 主控 | Planner 规划 | Subagent 子代理│   │
│   ├─────────────────────────────────────────────────────────────────────┤   │
│   │ 纯自研 ReAct 状态机 (0 黑盒依赖、毫秒级工具派发、确定性自愈重试)      │   │
│   ├─────────────────────────────────────────────────────────────────────┤   │
│   │ 自适应上下文压缩器 (128K+ 上下文管理、智能记忆蒸馏、关键帧保留)        │   │
│   ├─────────────────────────────────────────────────────────────────────┤   │
│   │ 本机安全金库 (Vault): Windows DPAPI 用户硬件级绑定加密、全链路 Scrub  │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                        双核自主执行引擎 (Execution Engine)                   │
│  ┌───────────────────────────────────┐   ┌────────────────────────────────┐ │
│  │ 现代化 Web 浏览器控制 (Web Core)   │   │ Windows 桌面原生控制 (OS Core)  │ │
│  │ • 不可变活体句柄 [ref=aeN]        │   │ • 四层交互阶梯 (Pattern 直调)   │ │
│  │ • Set-of-Mark (SoM) 语义边框标注  │   │ • 物理光标抢占保护 (空闲让出)    │ │
│  │ • 活跃浮层容器自适应感知隔离       │   │ • 安全桌面与 UAC 拦截防线       │ │
│  │ • 四步拟人闭环输入与落地回读核验   │   │ • 破坏性不可逆操作二次确认门禁   │ │
│  │ • 拟人生物运动曲线 (Fitts's Law)   │   │ • 80%/20% 极简桌面分屏人机协同   │ │
│  │ • 跨标签页一镜到底全自动录屏管线   │   │ • 中文无损剪贴板直写通道       │ │
│  └───────────────────────────────────┘   └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 核心架构设计与技术亮点

### 1. 现代化 Web 控制：不可变句柄（ref）与 Set-of-Mark（SoM）
针对 React/Vue/SPA 富应用中由虚拟列表、Flex 自适应布局和动态 Hash 类名引发的定位脆弱问题，系统弃用了传统的绝对坐标盲猜与脆弱的 CSS 选择器：
- **活体不可变句柄 (`[ref=aeN]`)**：单遍 JavaScript 扫描视野内所有可交互节点，赋予唯一的 DOM 活体句柄 `aeN`。大模型只需下发 `browser_click(ref="ae12")` 或 `browser_fill_input(ref="ae5")`，底层引擎根据物理渲染盒动态计算中心点，彻底免疫滚动位移与 DPI 缩放影响；
- **视觉 Set-of-Mark (SoM) 语义层**：在页面视图注入轻量语义边框（蓝色按钮、橙色输入框、紫色浮层元素），角标数字与 `aeN` 绝对 1:1 对齐，消除多模态视觉模型的感知漂移；
- **活跃浮层自适应隔离**：自动探测处于激活状态的 Modal / Dialog / 抽屉浮层，将查找作用域严格限制在浮层内部，杜绝穿透点击背景常驻搜索框；
- **拟人闭环输入落地核验**：执行（聚焦激活 -> 状态就绪 -> 原型链注入 -> 落地回读），若受控组件值未发生物理变化，强制阻断虚报成功并输出自愈策略；
- **反爬与生物熵拟人模拟**：原型链深度消除 `navigator.webdriver`，补齐完整 `window.chrome` 运行时；鼠标移动注入自然手腕弧度与 Fitts's Law 速度曲线。

### 2. Windows 桌面控制：UIA Pattern 直调阶梯与光标保护
针对传统桌面自动化“光标乱飞、抢夺鼠标、盲目点击”的弊病，确立了桌面控制的四层交互阶梯：
- **第一层：UIA Pattern 直调优先**：对于具备 UI Automation 模式的控件（按钮、复选框、下拉项、输入框），直接调用 `Invoke`、`Toggle`、`Value.SetValue`。**全程不需要瞬移物理鼠标光标**，人类手头工作与视觉零干扰；
- **第二层：短句柄（`c1`..`c64`）中心点击**：自动遍历当前前台窗口控件树生成短句柄并安全点击；
- **第三层与第四层**：相对/绝对物理坐标点击，兜底走视觉模型截图目测定位；
- **物理光标让出机制**：每次物理键鼠操作前通过 `GetLastInputInfo` 毫秒级探测人类物理输入。一旦人类正在使用鼠标（空闲时间 < 2 秒），自动化立即让出控制权并暂停等待；
- **破坏性操作二次确认**：对系统关键路径、文件清空、注销关机等高危动作底层设卡，必须获得显式确认方可执行。

### 3. 多角色模型路由、数据主权与安全金库（Vault）
- **多模型按角色路由与严格降级约束**：支持主控（Main）、规划（Planner）、子任务（Subagent）与后台分析（Worker）分别挂载不同的大模型服务商。**角色的候选链条（Role Chain）是严格封闭的白名单，严禁跨角色越界转移**（如敏感代码分析子代理绝不会外溢到公共对话模型）；
- **数据主权优先的受控故障转移（Privacy-Preserving Failover）**：遭遇 429 限流或服务不可用时，系统仅在用户显式授权的当前角色链条内有序容灾。若未授权备用商或备用商缺少 Key，**在网络层之前即被熔断阻断（0 字节上下文外泄），任务立即抛出 `RoutingUnavailable` 安全停机**，绝不进行黑盒漫游；支持全局或单角色彻底关闭容灾（`fallback: false` / `client.pin()`）；
- **Windows DPAPI 本地金库**：所有 API 密钥均通过 Windows 操作系统原生 `CryptProtectData` 加密持久化，密钥绑定当前物理机与当前登录用户。密钥在进入大模型上下文字符串、运行日志或前端展示前，经由 `Vault.scrub()` 全链路自动脱敏，杜绝泄密。

### 4. 极简桌面 Web 协同控制台
- 基于 Bottle 微服务引擎与原生 JavaScript 构建，无需庞大的 Node.js 构建链；
- 原生支持 **Server-Sent Events (SSE)** 实时流式日志、多智能体活动追踪、实时 Token 消耗遥测；
- 支持**多源模型全动态管理**：前端界面支持即时模糊搜索模型、一键热切换、自定义私有端点持久化注册，无需重启服务。

### 5. 数据主权与受控故障转移基准测试（Privacy & Failover Benchmark）

针对社区关切的“自动故障转移是否会暗中向未经授权的第三方泄露用户 Prompt”，OpenMuse-Win 提供了完备的可复现自动化测试（位于 [`tests/test_privacy_failover_tradeoff.py`](tests/test_privacy_failover_tradeoff.py)），任何开发者均可直接运行验证：

```bash
pytest tests/test_privacy_failover_tradeoff.py -v
```

#### 🛡️ 三大核心测试场景与实测断言：
1. **场景一：严格单模型授权停机测试**
   - **设定**：用户仅配置 Provider A。强制 A 遭遇 429 限流；
   - **实测断言**：路由器捕获后判定无后续授权候选，**立即抛出 `RoutingUnavailable` 安全中断**；网络抓包证实对未授权 Provider B 的网络请求数为 **0**，外发数据量为 **0 字节**。
2. **场景二：按角色绑定的降级目标刚性约束**
   - **设定**：`main_brain` 允许在 A 和 B 之间容灾，但私密分析角色 `subagent` 仅允许 A；
   - **实测断言**：A 故障时，`main_brain` 正常切换 B，而 `subagent` 坚决拒绝调用 B 并直接阻断停机，**证明角色绑定具有绝对的向下隔离约束力**。
3. **场景三：未配 Key 或显式禁用的备用模型零外发**
   - **设定**：链条包含备用模型，但未注入密钥或配置 `enabled: false`；
   - **实测断言**：路由器在预检层标记 `provider_disabled` / `missing_key`，`request_attempted` 恒为 `False`，网络层零握手。

#### 📊 成本、隐私与可用性工程权衡矩阵：

| 拓扑模式 | 配置范式 | 隐私与数据主权 | 自动化可用性 | 适用业务场景 |
| :--- | :--- | :---: | :---: | :--- |
| **严格数据主权模式**<br>*(Strict Sovereignty)* | 角色仅绑定单一私有模型，或全局设置 `fallback.enabled: false` | ⭐️⭐️⭐️⭐️⭐️<br>(100% 封闭) | ⭐️⭐️<br>(遇限流立即停机) | 核心商业机密处理、代码安全审计、私有合规端点。 |
| **按角色分级隔离模式**<br>*(Tiered Role Isolation)*<br>**【官方推荐】** | 敏感角色（Subagent / Code）单靶点封闭；日常交互角色（Main / Planner）配置多商兜底 | ⭐️⭐️⭐️⭐️<br>(关键数据零外溢) | ⭐️⭐️⭐️⭐️<br>(日常任务顺畅) | 兼顾机密资产保护与常规开发办公自动化的生产级架构。 |
| **高弹性零成本模式**<br>*(Elastic Zero-Cost)* | 角色配置多家免费/低成本 Provider 链条（如 Nvidia → ModelScope → 智谱） | ⭐️⭐️⭐️<br>(受各服务商条款约束) | ⭐️⭐️⭐️⭐️⭐️<br>(秒级自愈，>99% 完成率) | 公开网页数据检索、批量自动化、常规办公自动化。 |

---

## 🗺️ 架构详图 | Architecture in Detail

公开版全景架构图的细化补充：任务如何一步步执行、风险如何分级放行、部署拓扑长什么样。
A visual supplement to the panorama above: how tasks execute step by step, how risks are gated, and what deployment looks like.

### 任务执行流水线 | Task Execution Pipeline

```mermaid
flowchart LR
    p_task["用户任务<br/>User task"]
    p_plan["Planner 拆解<br/>Planner breaks down"]
    p_react["ReAct 循环<br/>Think → Act → Observe<br/>循环直到完成"]
    p_gate["审批闸门<br/>Approval gate"]
    p_exec["工具执行<br/>Tool execution<br/>浏览器 / 桌面"]
    p_verify["验收回传<br/>Verify & return"]
    p_sse["SSE 推送<br/>SSE live updates"]
    p_fail["模型失败 → Failover 下一家免费模型<br/>Model failure → failover to next free model"]
    p_tier["L1 自动 · L2 卡片确认 · L3 强约束<br/>L1 auto · L2 card confirm · L3 strict"]
    p_take["用户接管 → 人工完成 → 回锁继续<br/>Takeover → human finishes → resume"]
    p_audit["全程留痕：Tracing 链路追踪 · Token 用量统计 · 验收器核验<br/>Full audit trail: tracing, token usage, verifier checks"]
    p_task --> p_plan --> p_react --> p_gate --> p_exec --> p_verify --> p_sse
    p_react -.-> p_fail
    p_gate -.-> p_tier
    p_exec -.-> p_take
    p_sse -.-> p_audit
    classDef blue fill:#eff6ff,stroke:#2563eb
    classDef violet fill:#f5f3ff,stroke:#7c3aed
    classDef teal fill:#f0fdfa,stroke:#0d9488
    classDef amber fill:#fffbeb,stroke:#d97706
    class p_task,p_sse blue
    class p_plan,p_react violet
    class p_exec,p_verify teal
    class p_gate,p_tier amber
    class p_fail,p_take,p_audit amber
```

一句话任务进来后，Planner 拆解、ReAct 循环执行、审批闸门按风险定级放行、工具执行、验收回传，全程经 SSE 实时推送。
A one-sentence task goes through Planner breakdown, ReAct execution, risk-tiered approval gating, tool execution, and verified return, streaming over SSE all the way.

### 安全与审批架构 | Security & Approval

```mermaid
flowchart TB
    s_risk["风险分级<br/>Risk classification<br/>每次工具调用先定级"]
    s_l1["L1 · 自动放行<br/>Auto · low risk<br/>只读查询 / 状态读取 / 无副作用操作"]
    s_l2["L2 · 确认卡<br/>Approval card · medium risk<br/>文件写入 / 浏览器操作 / 桌面卡片 / 微信回执 1 / 同意"]
    s_l3["L3 · 强约束<br/>Strict · high risk<br/>越界操作 / 破坏性操作 / 必须明确批准"]
    s_base["防护底座 Defense in depth<br/>凭据保险箱 · 目录隔离 · 单实例锁<br/>热备份 + 清理 · 日志脱敏 · Tracing 审计"]
    s_risk --> s_l1
    s_risk --> s_l2
    s_risk --> s_l3
    s_l1 --> s_base
    s_l2 --> s_base
    s_l3 --> s_base
    classDef green fill:#ecfdf5,stroke:#059669
    classDef amber fill:#fffbeb,stroke:#d97706
    classDef rose fill:#fff1f2,stroke:#e11d48
    classDef slate fill:#f8fafc,stroke:#334155
    class s_l1 green
    class s_l2 amber
    class s_l3 rose
    class s_risk,s_base slate
```

每一次工具调用先按风险定级：L1 自动放行、L2 卡片确认、L3 强约束；底座是凭据保险箱、目录隔离、单实例锁、热备份与日志脱敏。
Every tool call is risk-classified first: L1 auto, L2 card confirmation, L3 strict; the base is the credential vault, directory isolation, single-instance lock, backups, and log scrubbing.

### 部署拓扑 | Deployment

```mermaid
flowchart LR
    subgraph PC["用户 Windows 电脑 User's Windows PC"]
        w_env["ensure_env.bat → venv 依赖环境<br/>ensure_env.bat → venv dependencies"]
        w_cfg["config.yaml + 环境变量 ALTEREGO_LLM_API_KEY<br/>config.yaml + ALTEREGO_LLM_API_KEY env var"]
        w_run["run_ui.bat 桌面 UI / run_cli.bat 终端<br/>run_ui.bat desktop UI · run_cli.bat CLI"]
        w_data["本地数据：data/ · SQLite · Vault<br/>Local data: data/ · SQLite · Vault"]
        w_env --> w_cfg --> w_run --> w_data
    end
    subgraph API["云 LLM API Cloud LLM APIs<br/>官方免费 · OpenAI 兼容"]
        w_nv["NVIDIA"]
        w_ms["ModelScope"]
        w_zp["Zhipu"]
        w_ag["Agnes"]
    end
    w_wx["微信通道 WeChat channel<br/>远程审批与双向通知<br/>Remote approval & two-way notifications"]
    w_run -- "模型推理 (HTTPS) · 零成本" --> w_nv
    w_run --> w_ms
    w_run --> w_zp
    w_run --> w_ag
    w_run -- "状态通知 / 审批回执" --> w_wx
    classDef blue fill:#eff6ff,stroke:#2563eb
    classDef amber fill:#fffbeb,stroke:#d97706
    classDef teal fill:#f0fdfa,stroke:#0d9488
    class w_env,w_cfg,w_run,w_data blue
    class w_nv,w_ms,w_zp,w_ag amber
    class w_wx teal
```

部署就是一台 Windows 电脑：ensure_env.bat 建环境、config.yaml + 环境变量配 key、run_ui.bat / run_cli.bat 启动；云端只出模型推理，微信通道只做远程审批与通知。
Deployment is one Windows PC: ensure_env.bat sets up the environment, config.yaml plus an env var holds the key, run_ui.bat / run_cli.bat start it; the cloud only does inference, WeChat only does remote approval and notifications.

---

## 📦 快速开始

### 1. 环境准备
- 操作系统：**Windows 10 / Windows 11 (64-bit)**
- 运行环境：**Python 3.10 或更高版本**（推荐 Python 3.11/3.12）
- 浏览器内核：系统自带 Microsoft Edge 或 Google Chrome

### 2. 获取代码与依赖安装
克隆本仓库到本地，并在项目根目录下运行一键环境配置脚本：

```cmd
git clone https://github.com/wojiadexiaoming-copy/openmuse-win.git
cd openmuse-win

:: 运行一键环境检测与虚拟环境创建（自动安装 requirements.txt）
ensure_env.bat
```

### 3. 模型配置
复制示例配置文件：

```cmd
copy config.example.yaml config.yaml
```

使用文本编辑器打开 `config.yaml`，填入您使用的大语言模型端点与模型名称（支持任何 OpenAI 兼容接口，例如 DeepSeek、Qwen、Agnes、OpenAI 等）：

```yaml
llm:
  provider: "openai_compatible"
  base_url: "https://your-api-endpoint.com/v1"
  model: "your-model-name"
  temperature: 0.2
  max_tokens: 8192
  supports_images: true
```

*注：API 密钥可通过系统环境变量注入（如 `ALTEREGO_LLM_API_KEY`），或在首次启动 Web UI 时通过内置安全金库录入。*

### 4. 启动运行

- **启动交互式桌面 Web 控制台**：
  ```cmd
  run_ui.bat
  ```
  控制台将在本地启动并弹出协同窗口，默认左侧分屏联动浏览器，右侧协同交互。

- **启动终端 CLI 对话模式**：
  ```cmd
  run_cli.bat
  ```

---

## 🛠️ 项目目录结构

```
openmuse-win/
├── agent/                  # 自研 ReAct 编排核心、角色路由、上下文压缩与主状态机
├── tools/                  # 工具箱：浏览器自动化 (browser/)、桌面控制 (desktop/)、Shell/File 等
├── ui/                     # 极简 Web 控制台服务端 (server.py) 与前端单页应用 (static/)
├── channels/               # 多通道支持（Web 控制台、微信 iLink 协议长轮询等）
├── vault/                  # 本机安全金库：Windows DPAPI 硬件密钥隔离与审计
├── memory/                 # 记忆管理与持久化蒸馏流水线
├── prompts/                # 系统级 Prompt、Tool Calling 协议模板
├── tests/                  # 完备的单元测试与端到端集成测试套件 (pytest)
├── config.example.yaml     # 配置文件范例
├── ensure_env.bat          # Windows 一键环境准备脚本
├── run_ui.bat              # Web UI 控制台启动脚本
├── run_cli.bat             # 命令行 CLI 启动脚本
├── pyproject.toml          # 项目配置与 pytest 规范
├── requirements.txt        # 核心 Python 依赖清单
└── LICENSE                 # GNU AGPL-3.0 开源许可证
```

---

## 🛡️ 安全模型与控制权限

为了保障在物理 Windows 宿主机上运行代码的安全性，OpenMuse 实施了分级治理策略：
- **L1 级（安全只读）**：信息检索、网页浏览、截图、快照、无副作用操作，由 Agent 自主执行；
- **L2 级（低危可逆）**：普通文件创建、常规网页表单填写、安全桌面应用前置，带审计日志执行；
- **L3 级（高危破坏）**：系统底层命令执行（`shell_exec`）、重要文件覆盖、破坏性指令，**底层强制挂起并向人类操作者请求单次许可（Approval）**，超时未获授权自动拒绝。

---

## 📄 开源许可证

本项目基于 **[GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE)** 协议开源。
欢迎社区贡献代码、提出 Issue 与改进建议。
