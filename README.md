# OpenMuse-Win (AlterEgo)

Windows 原生单机个人 AI Agent。0→1 自研编排层，不基于 LangChain / AutoGen / CrewAI 等整套框架。

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23446166.svg)](https://doi.org/10.5281/zenodo.23446166)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

> **论文正式发布**：
> 《**AlterEgo: Towards the Zero-Cost Personal Agent**》  
> - **DOI**: [10.5281/zenodo.23446166](https://doi.org/10.5281/zenodo.23446166)  
> - **Zenodo 页面**: [https://zenodo.org/records/23446166](https://zenodo.org/records/23446166)  
> - **公开开源仓库**: [https://github.com/wojiadexiaoming-copy/openmuse-win](https://github.com/wojiadexiaoming-copy/openmuse-win)
> 
> 🏆 **BoHack 2026 评审核验指引（42小时冲刺成果）**：  
> 本项目带 1.0 开源基线 (`openmuse-win`) 参赛。在活动期间 **42 小时冲刺** 内，累计完成 **+273 个新增文件**、**+41,869 行新增代码**，重点攻坚多进程硬隔离、复杂 SPA 浏览器原生事件链、微信移动端双向审批与全新桌面高保真工作台重构。  
> - 📄 完整对照报告：[**《🏆 BoHack 2026 42小时技术增量与基线对照报告》**](docs/🏆%20BoHack%202026%2042小时技术增量与基线对照报告.md) / [ASCII 镜像](docs/BoHack_2026_42h_Increment_Report.md)  
> - 📝 详细更新日志：[**CHANGELOG.md**](CHANGELOG.md)  
> - 🌿 对应发布分支：[`v1.1-20261114`](https://github.com/wojiadexiaoming-copy/openmuse-win/tree/v1.1-20261114)

> 许可状态：AGPL-3.0 + 商业许可（双许可），**最终条款待律师审核**，见 `LICENSE_STATUS.md`。

## 真实评测 | Real-World Evaluation

2026-10-29 实拍，三段任务连续完成，均含步骤字幕。
Recorded on Oct 29, 2026: three tasks completed in one continuous session, with step subtitles burned in.

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

## 当前系统能力与里程碑

按 `docs/spec.md` 与 `docs/完善路线图.md` 推进，核心里程碑已全线贯通：

- **编排与治理**：ReAct 反应式主循环、Planner 多步规划与动态子 Agent、上下文滑动窗口与 Worker 异步会话蒸馏。
- **安全与权限**：三级审批卡片机制（L1 自动/L2 确认/L3 强约束）、Windows 凭据保险箱（Vault for Machine）、工作目录越界隔离。
- **桌面与多通道**：Windows 原生 UIA 桌面控制、PyWebView 托盘浮窗、WeChat 远程审批与双向通知通道。
- **数据生命周期**：统一 `data/` 目录隔离、多进程单实例互斥锁（SingleInstanceLock）、SQLite 在线热备份与保留期自动清理（Retention）。
- **观测与度量**：全链路 Tracing、Token 用量实时统计、Server-Sent Events (SSE) 全量状态广播。

## 系统架构 | System Architecture

AlterEgo 的核心设计只有一句话：大脑在云端免费模型，执行永远在用户本机。
One sentence sums up the design: the brain runs on free cloud models, execution always stays on the user's machine.

### 总体架构 | System Overview

```mermaid
flowchart TB
    subgraph S1["交互层 Interaction"]
        i_ui["桌面 UI · PyWebView 托盘浮窗<br/>Desktop UI · PyWebView tray window"]
        i_wx["微信远程通道<br/>WeChat remote channel"]
        i_cli["终端 CLI<br/>Terminal CLI"]
    end
    subgraph S2["编排层 Orchestration"]
        o_react["主循环 ReAct<br/>ReAct main loop"]
        o_plan["Planner 多步规划<br/>Multi-step planner"]
        o_sub["动态子 Agent<br/>Dynamic sub-agents"]
        o_ctx["上下文窗口 + 蒸馏<br/>Context window + distillation"]
    end
    subgraph S3["工具层 Tools"]
        t_brow["浏览器工具<br/>Browser tools · Playwright / CDP"]
        t_desk["桌面 UIA 控制<br/>Desktop UIA control"]
        t_shell["Shell · 文件 · 搜索<br/>Shell · File · Web search"]
    end
    subgraph S4["模型层 Models"]
        m_router["LLM Router · 零成本 Failover<br/>Zero-cost failover · OpenAI-compatible"]
        m_nv["NVIDIA<br/>Free API"]
        m_ms["ModelScope<br/>Free API"]
        m_zp["Zhipu<br/>Free API"]
        m_ag["Agnes<br/>Free API"]
    end
    subgraph S5["数据与安全层 Data & Security"]
        d_vault["凭据保险箱<br/>Credential vault"]
        d_sql["SQLite 热备份<br/>SQLite backup"]
        d_data["data/ 目录隔离<br/>data/ isolation"]
        d_lock["单实例锁<br/>Single-instance lock"]
        d_appr["审批卡 · 链路追踪<br/>Approval · Tracing"]
    end
    i_ui --> o_react
    i_wx --> o_react
    i_cli --> o_react
    o_react --> o_plan
    o_react --> o_sub
    o_react --> o_ctx
    o_react --> t_brow
    o_plan --> t_brow
    o_sub --> t_brow
    o_sub --> t_desk
    o_ctx --> t_brow
    o_ctx --> t_shell
    t_brow --> m_router
    t_desk --> m_router
    t_shell --> m_router
    m_router --> m_nv
    m_router --> m_ms
    m_router --> m_zp
    m_router --> m_ag
    m_nv --> d_vault
    m_ms --> d_sql
    m_zp --> d_data
    m_ag --> d_lock
    m_ag --> d_appr
    classDef blue fill:#eff6ff,stroke:#2563eb
    classDef violet fill:#f5f3ff,stroke:#7c3aed
    classDef teal fill:#f0fdfa,stroke:#0d9488
    classDef amber fill:#fffbeb,stroke:#d97706
    classDef rose fill:#fff1f2,stroke:#e11d48
    class i_ui,i_wx,i_cli blue
    class o_react,o_plan,o_sub,o_ctx violet
    class t_brow,t_desk,t_shell teal
    class m_router,m_nv,m_ms,m_zp,m_ag amber
    class d_vault,d_sql,d_data,d_lock,d_appr rose
```

五层结构：交互层接用户，编排层做规划与执行，工具层操作浏览器与桌面，模型层走四家官方免费 API 做零成本 failover，数据与安全层兜底。
Five layers: interaction takes user input, orchestration plans and executes, tools drive the browser and desktop, the model layer fails over across four official free APIs at zero cost, and the data & security layer holds everything together.

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

## 🚀 版本历史 | Changelog

当前版本 v1.5.31：微信审批回执、授权降噪与前端可见。
Current version v1.5.31: WeChat approval receipts, authorization noise reduction, frontend visibility.

完整版本迭代历史见 [CHANGELOG.md](CHANGELOG.md)。
Full version history: [CHANGELOG.md](CHANGELOG.md).

## 快速开始（Windows）

> 宿主机 + Hyper-V 虚拟机共享同一目录开发时：venv 必须每台机器各建一份。
> 直接运行 `ensure_env.bat`（自动识别/创建/装依赖），再 `run_ui.bat` / `run_cli.bat`。
> 不要复制另一台机器的 `.venv*`、`vault-*`。完整规则见 `docs/双机开发环境.md`。

```powershell
# 1. 准备本机环境（幂等；requirements.txt 变化时自动补装）
ensure_env.bat

# 2. 配 config
copy config.example.yaml config.yaml
# 填 llm.base_url / llm.model，API Key 走环境变量 ALTEREGO_LLM_API_KEY
# （M2 才接 Vault，M1 用环境变量，见 config.example.yaml）

# 3. 跑
run_ui.bat            # 桌面 UI 壳
run_cli.bat           # 终端 CLI（等效 python -m agent.main）
```

## 仓库结构

```
agent/        编排核心（models/llm/registry/context/orchestrator/main）
tools/        工具实现（base + shell/file/web_search）
prompts/      系统提示词（§6）
ui/           桌面 UI（H5 原型 + pywebview 壳，待真机验证）
tests/        测试（pytest）
docs/         技术规格书全文（spec.md，唯一需求来源）
PROGRESS.md   进度与待办清单（给 Antigravity 看）
CHANGELOG.md  版本迭代历史（原 README 内嵌章节）
```

