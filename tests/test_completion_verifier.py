"""Completion requires evidence created or updated during this turn."""
import os
from types import SimpleNamespace

from PIL import Image

from agent.verifier import capture_artifacts, verify_outcome


def _image(path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def test_old_images_do_not_satisfy_current_turn(tmp_path):
    _image(tmp_path / "old.png")
    baseline = capture_artifacts(str(tmp_path))

    passed, failed = verify_outcome(
        "生成图片并下载", workspace=str(tmp_path), baseline=baseline,
    )

    assert not passed
    assert "本轮" in failed[0] and "图片" in failed[0]


def test_image_download_without_count_requires_valid_image(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    (tmp_path / "download.png").write_bytes(b"not an image")

    assert not verify_outcome(
        "下载图片到本地", workspace=str(tmp_path), baseline=baseline,
    )[0]

    _image(tmp_path / "download.png")

    assert verify_outcome(
        "下载图片到本地", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])


def test_screenshots_cannot_satisfy_image_generation(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    _image(tmp_path / "desktop_shot_1.png")
    _image(tmp_path / "nested" / "shot_2.jpg")
    _image(tmp_path / "DESKTOP_SHOT_3.png")

    assert capture_artifacts(str(tmp_path)) == {}
    assert not verify_outcome(
        "打开豆包生图并下载", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_empty_files_and_corrupt_images_are_not_evidence(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    (tmp_path / "empty.png").touch()
    (tmp_path / "broken.jpg").write_bytes(b"jpg")

    assert not verify_outcome(
        "生成 2 张图片", workspace=str(tmp_path), baseline=baseline,
    )[0]
    assert not verify_outcome(
        "生成 3 个文件", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_modified_image_counts_as_current_turn_evidence(tmp_path):
    path = tmp_path / "result.png"
    _image(path, color="red")
    baseline = capture_artifacts(str(tmp_path))
    _image(path, color="blue")
    # Explicit nanosecond change avoids depending on filesystem clock resolution.
    old_mtime = baseline["result.png"][1]
    os.utime(path, ns=(old_mtime + 1_000_000_000, old_mtime + 1_000_000_000))

    assert verify_outcome(
        "生图并下载", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])


def test_empty_baseline_means_strict_current_turn_check(tmp_path):
    (tmp_path / "empty.txt").touch()

    assert not verify_outcome(
        "生成 1 个文件", workspace=str(tmp_path), baseline={},
    )[0]

    (tmp_path / "empty.txt").write_text("ready", encoding="utf-8")

    assert verify_outcome(
        "生成 1 个文件", workspace=str(tmp_path), baseline={},
    ) == (True, [])


def test_counts_require_requested_file_type(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    _image(tmp_path / "image.png")
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "clip.mp4").write_bytes(b"video")

    assert not verify_outcome(
        "生成 2 张图片", workspace=str(tmp_path), baseline=baseline,
    )[0]
    assert not verify_outcome(
        "生成 2 段视频", workspace=str(tmp_path), baseline=baseline,
    )[0]
    assert not verify_outcome(
        "生成 1 份 PDF 文件", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_mixed_quantity_goals_check_each_type(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    _image(tmp_path / "first.png")
    _image(tmp_path / "second.png")

    assert not verify_outcome(
        "生成 2 张图片和 1 段视频", workspace=str(tmp_path), baseline=baseline,
    )[0]

    (tmp_path / "clip.mp4").write_bytes(b"video")

    assert verify_outcome(
        "生成 2 张图片和 1 段视频", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])


def test_explicit_count_cannot_be_hidden_by_vague_plan(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    _image(tmp_path / "only-one.png")
    plan = SimpleNamespace(steps=[SimpleNamespace(done_when="任务已完成")])

    assert not verify_outcome(
        "生成 2 张图片", plan=plan, workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_excluded_trees_do_not_supply_evidence(tmp_path, monkeypatch):
    excluded = (
        ".git", ".venv", "venv", "node_modules", "cache", ".cache",
        "__pycache__", "browsers", ".pytest_cache",
    )
    for directory in excluded:
        _image(tmp_path / directory / "result.png")
    # Prove exclusions are pruned before os.walk descends into them.
    real_walk = os.walk
    visited = []

    def watched_walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            visited.append(entry[0])
            yield entry

    monkeypatch.setattr("agent.verifier.os.walk", watched_walk)

    assert capture_artifacts(str(tmp_path)) == {}
    assert visited == [str(tmp_path)]
    assert not verify_outcome(
        "生成图片并下载", workspace=str(tmp_path), baseline={},
    )[0]


def test_unknown_chat_skips_workspace_scan(tmp_path, monkeypatch):
    def fail_scan(*args, **kwargs):
        raise AssertionError("ordinary chat should not scan artifacts")

    monkeypatch.setattr("agent.verifier._candidate_files", fail_scan)

    assert verify_outcome(
        "随便聊聊，给我 3 个建议", workspace=str(tmp_path), baseline={},
    ) == (True, [])


def test_old_video_does_not_satisfy_current_duration(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "old.mp4").write_bytes(b"video")
    baseline = capture_artifacts(str(tmp_path))

    assert not verify_outcome(
        "录制时长 10 秒", workspace=str(tmp_path), baseline=baseline,
    )[0]

    (tmp_path / "new.mp4").write_bytes(b"video")

    assert verify_outcome(
        "录制时长 10 秒", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])


def test_legacy_calls_keep_total_file_and_duration_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")

    assert verify_outcome(
        "生成 2 个文件", workspace=str(tmp_path),
    ) == (True, [])

    (tmp_path / "old.mp4").write_bytes(b"dummy video")

    assert verify_outcome(
        "录制时长 10 秒", workspace=str(tmp_path),
    ) == (True, [])


def test_explicit_extension_does_not_count_other_image_formats(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    _image(tmp_path / "first.png")
    _image(tmp_path / "second.jpg")

    assert not verify_outcome(
        "生成 2 个 PNG 文件", workspace=str(tmp_path), baseline=baseline,
    )[0]
    assert not verify_outcome(
        "生成 2 个 .png 文件", workspace=str(tmp_path), baseline=baseline,
    )[0]

    _image(tmp_path / "second.png")

    assert verify_outcome(
        "生成 2 个 PNG 文件", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])


def test_new_empty_video_cannot_satisfy_duration(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    baseline = capture_artifacts(str(tmp_path))
    (tmp_path / "empty.mp4").touch()

    assert not verify_outcome(
        "录制时长 10 秒", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_runtime_database_updates_are_not_file_deliverables(tmp_path):
    for name in ("spans.db", "conversations.db"):
        (tmp_path / name).write_bytes(b"existing runtime state")
    baseline = capture_artifacts(str(tmp_path))
    for name in ("spans.db", "conversations.db"):
        (tmp_path / name).write_bytes(b"updated runtime state with more content")

    assert not verify_outcome(
        "创建文件", workspace=str(tmp_path), baseline=baseline,
    )[0]
    assert not verify_outcome(
        "生成 2 个文件", workspace=str(tmp_path), baseline=baseline,
    )[0]

    (tmp_path / "deliverable.txt").write_text("user output", encoding="utf-8")

    assert verify_outcome(
        "创建文件", workspace=str(tmp_path), baseline=baseline,
    ) == (True, [])
    assert not verify_outcome(
        "生成 2 个文件", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_runtime_sidecars_logs_and_tokens_are_not_file_deliverables(tmp_path):
    baseline = capture_artifacts(str(tmp_path))
    for name in (
        "spans.db-wal", "spans.db-shm", "runtime.sqlite", "state.sqlite3",
        "app.log", "app.log.1", "memory.bin", "auth.token", "token.json",
        "session.lock", "process.pid",
    ):
        (tmp_path / name).write_bytes(b"runtime data")

    assert not verify_outcome(
        "创建文件", workspace=str(tmp_path), baseline=baseline,
    )[0]


def test_explanations_of_image_generation_do_not_require_artifacts(tmp_path, monkeypatch):
    def fail_scan(*args, **kwargs):
        raise AssertionError("an explanation should not scan for generated files")

    monkeypatch.setattr("agent.verifier._candidate_files", fail_scan)

    for goal in (
        "解释如何生成图片",
        "请解释如何生成 2 张图片",
        "介绍怎么生图并下载",
        "如何创建文件",
    ):
        assert verify_outcome(
            goal, workspace=str(tmp_path), baseline={},
        ) == (True, [])


def test_explanation_followed_by_execution_still_requires_artifact(tmp_path):
    assert not verify_outcome(
        "解释如何生成图片，然后帮我生成图片",
        workspace=str(tmp_path), baseline={},
    )[0]


def test_multiple_roots_combine_images_and_keep_distinct_names(tmp_path):
    project = tmp_path / "project"
    downloads = tmp_path / "Downloads"
    _image(project / "old.png")
    _image(downloads / "old.png")
    roots = [str(project), downloads]
    baseline = capture_artifacts(roots)

    assert set(baseline) == {
        os.path.normcase(os.path.abspath(project / "old.png")),
        os.path.normcase(os.path.abspath(downloads / "old.png")),
    }

    _image(project / "first.png")
    _image(project / "second.png")
    _image(downloads / "first.png")

    assert verify_outcome(
        "生成 3 张图片并下载", workspace=roots, baseline=baseline,
    ) == (True, [])
    assert not verify_outcome(
        "生成 4 张图片并下载", workspace=roots, baseline=baseline,
    )[0]


def test_repeated_and_overlapping_roots_do_not_count_files_twice(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    repeated = [project, str(project), project / "."]
    baseline = capture_artifacts(repeated)
    _image(nested / "only.png")

    assert not verify_outcome(
        "生成 2 张图片", workspace=repeated, baseline=baseline,
    )[0]
    assert set(capture_artifacts(repeated)) == {"nested/only.png"}

    overlapping = [project, nested, project]
    assert len(capture_artifacts(overlapping)) == 1
    assert not verify_outcome(
        "生成 2 张图片", workspace=overlapping, baseline={},
    )[0]


def test_path_workspace_preserves_single_root_relative_keys(tmp_path):
    _image(tmp_path / "nested" / "result.png")

    assert set(capture_artifacts(tmp_path)) == {"nested/result.png"}
