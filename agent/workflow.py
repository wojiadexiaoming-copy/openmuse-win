"""§25 Workflow 编排（固定流水线）[必选]

子 Agent 是"临场发挥"，Workflow 是"固定套路"。
重复性流程（如视频管线）写成代码 DAG，不让 LLM 每次现场想。
"""
from __future__ import annotations

import logging

log = logging.getLogger("workflow")

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class WorkflowStep:
    step_id: str
    name: str                           # "实测录屏"
    kind: str                           # tool | subagent | llm | approval | condition
    config: dict = field(default_factory=dict)
    on_success: str = ""                # 下一步 step_id，空=结束
    on_failure: str = ""                # 失败跳到哪，空=abort
    retry: int = 2
    timeout_sec: int = 1800


@dataclass
class Workflow:
    workflow_id: str
    name: str
    steps: list[WorkflowStep] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    def step_map(self) -> dict[str, WorkflowStep]:
        return {s.step_id: s for s in self.steps}


@dataclass
class WorkflowRun:
    run_id: str
    workflow_id: str
    status: str = "running"             # running | done | failed | aborted
    current_step: str = ""
    step_results: dict = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0


class WorkflowEngine:
    """DAG 执行器：按步骤定义顺序跑，支持重试/分支/条件。"""

    def __init__(self):
        self._workflows: dict[str, Workflow] = {}
        self._runs: dict[str, WorkflowRun] = {}

    def register(self, wf: Workflow):
        self._workflows[wf.workflow_id] = wf

    def get(self, workflow_id: str) -> Workflow | None:
        return self._workflows.get(workflow_id)

    def start(self, workflow_id: str, inputs: dict | None = None) -> str:
        """启动一次运行，返回 run_id。实际执行由调用方驱动 step()。"""
        wf = self.get(workflow_id)
        if wf is None:
            raise ValueError(f"未知 workflow: {workflow_id}")
        run_id = f"wr-{uuid.uuid4().hex[:8]}"
        first = wf.steps[0].step_id if wf.steps else ""
        run = WorkflowRun(run_id=run_id, workflow_id=workflow_id,
                           current_step=first, started_at=time.time())
        if inputs:
            # inputs 合并到 run 上下文
            run.step_results["_inputs"] = dict(inputs)
        self._runs[run_id] = run
        log.info(f"[workflow] 启动 {workflow_id} → run {run_id}")
        return run_id

    def current_step(self, run_id: str) -> WorkflowStep | None:
        run = self._runs.get(run_id)
        wf = self._workflows.get(run.workflow_id) if run else None
        if not run or not wf:
            return None
        return wf.step_map().get(run.current_step)

    def complete_step(self, run_id: str, success: bool, result: dict | None = None):
        """调用方执行完一步后上报，进入下一步或结束。"""
        run = self._runs.get(run_id)
        wf = self._workflows.get(run.workflow_id) if run else None
        if not run or not wf:
            raise ValueError(f"未知 run: {run_id}")
        step = wf.step_map().get(run.current_step)
        if step is None:
            run.status = "failed"
            return
        run.step_results[step.step_id] = {
            "success": success, "result": result or {},
            "at": time.time(),
        }
        nxt = step.on_success if success else step.on_failure
        if not nxt:
            run.status = "done" if success else "failed"
            run.finished_at = time.time()
            run.current_step = ""
            log.info(f"[workflow] run {run_id} 结束：{run.status}")
        else:
            # 简单重试：失败且 retry>0 则重跑本步（调用方负责计数，这里只跳转）
            run.current_step = nxt

    def abort(self, run_id: str):
        run = self._runs.get(run_id)
        if run:
            run.status = "aborted"
            run.finished_at = time.time()

    def status(self, run_id: str) -> dict:
        run = self._runs.get(run_id)
        if not run:
            return {"error": "未知 run"}
        return {"run_id": run_id, "status": run.status,
                "current_step": run.current_step,
                "steps_done": len(run.step_results)}


def make_workflow_tools(engine: "WorkflowEngine"):
    """构建 Workflow 工具。"""
    from agent.tools_registry import ToolDefinition

    def _start(args: dict) -> str:
        try:
            rid = engine.start(args["workflow_id"], args.get("inputs"))
            step = engine.current_step(rid)
            return (f"[Workflow 已启动] run_id={rid}，"
                    f"当前步骤：{step.name if step else '无'}")
        except Exception as e:
            return f"[工具执行失败] {e}"

    def _status(args: dict) -> str:
        s = engine.status(args["run_id"])
        return f"[Workflow] {s}"

    def _complete(args: dict) -> str:
        try:
            engine.complete_step(args["run_id"],
                                  bool(args.get("success", True)),
                                  args.get("result"))
            step = engine.current_step(args["run_id"])
            st = engine.status(args["run_id"])
            if st["status"] in ("done", "failed", "aborted"):
                return f"[Workflow] run 结束：{st['status']}"
            return f"[Workflow] 下一步：{step.name if step else '无'}"
        except Exception as e:
            return f"[工具执行失败] {e}"

    return [
        ToolDefinition(
            name="workflow_start",
            description="启动一个预定义 Workflow（如视频管线）。",
            parameters={"type": "object",
                         "properties": {
                             "workflow_id": {"type": "string"},
                             "inputs": {"type": "object"}},
                         "required": ["workflow_id"]},
            func=_start,
        ),
        ToolDefinition(
            name="workflow_status",
            description="查 Workflow 运行状态。",
            parameters={"type": "object",
                         "properties": {"run_id": {"type": "string"}},
                         "required": ["run_id"]},
            func=_status,
        ),
        ToolDefinition(
            name="workflow_complete_step",
            description="上报当前步骤完成，进入下一步。",
            parameters={"type": "object",
                         "properties": {
                             "run_id": {"type": "string"},
                             "success": {"type": "boolean"},
                             "result": {"type": "object"}},
                         "required": ["run_id"]},
            func=_complete,
        ),
    ]
