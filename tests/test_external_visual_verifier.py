"""Separate application-visible visual results from requested local delivery."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.verifier import capture_artifacts, extract_criteria, outcome_requirement, verify_outcome


def completed_evidence(count=1):
    return {"status": "completed", "goal_match": True, "result_count": count,
            "snapshot_id": "snapshot-current", "task_origin_id": "task-current",
            "source": "visual_observer"}


@pytest.mark.parametrize("goal,count", [
    ("用豆包桌面程序生成一张心理学的海报", 1),
    ("在豆包中生成两张图片", 2),
    ("打开豆包，然后生成三张风景图", 3),
    ("通过豆包生图", 1),
    ("豆包里生成 4 张海报", 4),
    ("使用浏览器中的豆包网页生成十张图片", 10),
    ("在指定的桌面应用内生成十二张海报", 12),
    ("在星光网页应用里制作一张海报", 1),
    ("在豆包生成一张图片，不用下载到本地", 1),
])
def test_explicit_external_image_goals_have_visual_requirements(goal, count):
    assert outcome_requirement(goal) == {
        "mode": "external_visual", "kind": "图片", "minimum_count": count,
        "external_generation": True,
    }


@pytest.mark.parametrize("suffix", [
    "并下载", "并保存", "并导出", "放到本地", "交付图片文件", "发送给我", "发给小明",
    "上传到网站", "分享给同事", "然后解释原因", "然后整理说明", "再解释原理", "后总结内容",
])
def test_external_generation_with_additional_delivery_or_work_is_not_ui_only(suffix):
    requirement = outcome_requirement("在豆包生成两张图片" + suffix)
    assert requirement == {"mode": "local_artifact", "kind": "图片", "minimum_count": 2,
                           "external_generation": True}


@pytest.mark.parametrize("goal,kind,count", [
    ("生成三张图片", "图片", 3),
    ("用Python生成两张图片", "图片", 2),
    ("生成以豆包为主题的一张海报", "图片", 1),
    ("提到豆包之后生成一张图片", "图片", 1),
    ("下载豆包生成的图片到本地", "图片", 1),
    ("生成两份PDF文件", "文件", 2),
    ("创建三份文件", "文件", 3),
    ("在豆包生成图片，不下载但保存到本地", "图片", 1),
])
def test_local_and_ambiguous_requests_keep_local_requirements(goal, kind, count):
    requirement = outcome_requirement(goal)
    assert requirement["mode"] == "local_artifact"
    assert requirement["kind"] == kind and requirement["minimum_count"] == count


@pytest.mark.parametrize("goal", [
    "解释如何在豆包生成三张图片", "告诉我在豆包怎么生成海报", "在豆包怎么生图",
    "用豆包生成图片的教程", "介绍怎么生图并下载两张图片", "随便聊聊，给我三个建议",
])
def test_explanations_and_ordinary_chat_do_not_require_outputs(goal, monkeypatch):
    monkeypatch.setattr("agent.verifier._candidate_files", lambda *a, **k: pytest.fail("unexpected scan"))
    assert outcome_requirement(goal) == {"mode": "ordinary", "kind": "", "minimum_count": 0,
                                         "external_generation": False}
    assert verify_outcome(goal, baseline={}) == (True, [])


def test_external_goal_requires_evidence_even_without_baseline_or_with_vague_plan(monkeypatch):
    monkeypatch.setattr("agent.verifier._candidate_files", lambda *a, **k: pytest.fail("unexpected local scan"))
    plan = SimpleNamespace(steps=[SimpleNamespace(done_when="完成")])
    goal = "用豆包桌面程序生成一张心理学的海报"
    for baseline in (None, {}):
        passed, failed = verify_outcome(goal, plan=plan, baseline=baseline)
        assert not passed and "界面结果检查" in failed[0]
        assert "保存到工作目录" not in failed[0]
        assert verify_outcome(goal, plan=plan, baseline=baseline,
                              external_evidence=completed_evidence()) == (True, [])


@pytest.mark.parametrize("change", [
    {"status": "unknown"}, {"status": "submitted"}, {"status": "generating"}, {"status": True},
    {"goal_match": False}, {"goal_match": "true"}, {"goal_match": 1},
    {"result_count": 0}, {"result_count": -1}, {"result_count": True}, {"result_count": "1"},
    {"result_count": 1.0}, {"snapshot_id": ""}, {"snapshot_id": "  "}, {"snapshot_id": None},
    {"task_origin_id": ""}, {"task_origin_id": None}, {"source": "assistant"}, {"source": "screenshot"},
])
def test_only_strict_completed_visual_evidence_can_pass(change):
    evidence = completed_evidence()
    evidence.update(change)
    assert not verify_outcome("在豆包生成一张图片", baseline={}, external_evidence=evidence)[0]


@pytest.mark.parametrize("evidence", [None, {}, [], "completed", True])
def test_missing_or_unstructured_visual_evidence_never_completes(evidence):
    assert not verify_outcome("在豆包生图", baseline={}, external_evidence=evidence)[0]


def test_external_chinese_count_requires_enough_distinct_observed_results():
    goal = "在豆包生成三张海报"
    assert not verify_outcome(goal, external_evidence=completed_evidence(2))[0]
    assert verify_outcome(goal, external_evidence=completed_evidence(3)) == (True, [])
    assert extract_criteria("生成两张图片和三段视频") == ["数量检查：2张图片", "数量检查：3段视频"]


def test_local_chinese_count_still_requires_changed_decodable_images(tmp_path):
    old = tmp_path / "old.png"
    Image.new("RGB", (8, 8), "red").save(old)
    baseline = capture_artifacts(tmp_path)
    goal = "在豆包生成两张图片并下载到本地"
    evidence = completed_evidence(2)
    assert not verify_outcome(goal, workspace=tmp_path, baseline=baseline, external_evidence=evidence)[0]
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "new1.png")
    (tmp_path / "new2.png").write_bytes(b"not an image")
    Image.new("RGB", (8, 8)).save(tmp_path / "desktop_shot_current.png")
    assert not verify_outcome(goal, workspace=tmp_path, baseline=baseline, external_evidence=evidence)[0]
    Image.new("RGB", (8, 8), "green").save(tmp_path / "new2.png")
    assert verify_outcome(goal, workspace=tmp_path, baseline=baseline, external_evidence=evidence) == (True, [])


def test_local_screenshot_cannot_replace_missing_ui_evidence(tmp_path):
    Image.new("RGB", (8, 8), "red").save(tmp_path / "poster.png")
    Image.new("RGB", (8, 8), "red").save(tmp_path / "desktop_shot_current.png")
    assert not verify_outcome("在豆包生成一张海报", workspace=tmp_path, baseline={})[0]


def test_explanation_then_explicit_generation_is_an_execution_goal(tmp_path):
    goal = "解释如何生成图片，然后帮我在豆包生成两张海报"
    requirement = outcome_requirement(goal)
    assert requirement["mode"] != "ordinary" and requirement["external_generation"] is True
    assert not verify_outcome(goal, workspace=tmp_path, baseline={})[0]



@pytest.mark.parametrize("goal", [
    "在豆包生成一张海报和一段视频",
    "在豆包生成一张海报和一段30秒视频",
    "在豆包生成一张海报并告诉我配色理由",
    "在豆包生成一张海报并打开记事本",
])
def test_additional_outputs_or_actions_cannot_be_skipped_by_visual_completion(goal):
    requirement = outcome_requirement(goal)
    assert requirement["external_generation"] is True
    assert requirement["mode"] == "local_artifact"
