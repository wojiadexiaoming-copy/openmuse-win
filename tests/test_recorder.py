"""屏幕录屏与桌面测试 DSL（spec §20）单元测试。"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobs.desktop_test import desktop_ai_test, execute_dsl_step
from tools.recorder import ScreenRecorder


def test_screen_recorder_mock(tmp_path):
    rec = ScreenRecorder(recordings_dir=tmp_path, force_backend="mock")
    vpath = rec.start("demo_run")
    assert rec.is_recording is True
    assert Path(vpath).exists()
    assert vpath.endswith(".mp4")

    st = rec.status()
    assert st["is_recording"] is True
    assert st["name"].startswith("demo_run_")
    assert st["backend"] == "mock"

    out = rec.stop()
    assert rec.is_recording is False
    assert out == vpath
    assert Path(out).exists()


def test_dsl_step_execution(monkeypatch):
    # 测试基础 DSL 动作执行
    res_wait = execute_dsl_step({"action": "wait", "seconds": 0.01})
    assert res_wait["success"] is True

    res_unknown = execute_dsl_step({"action": "invalid_cmd"})
    assert res_unknown["success"] is False
    assert "未知" in res_unknown["detail"]


def test_desktop_ai_test_workflow(tmp_path, monkeypatch):
    # Mock 实际操作系统点击与打字，以支持在无屏/各种环境下测试完整管线
    monkeypatch.setattr("jobs.desktop_test.desktop_open_app", lambda args: "已启动应用")
    monkeypatch.setattr("jobs.desktop_test.desktop_type", lambda args: "已输入文本")
    monkeypatch.setattr("jobs.desktop_test.desktop_hotkey", lambda args: "已发送快捷键")
    monkeypatch.setattr("jobs.desktop_test.desktop_screenshot", lambda args: "downloads/test.jpg")

    script = [
        {"action": "launch", "app": "记事本"},
        {"action": "type", "text": "Hello AlterEgo"},
        {"action": "press", "key": "enter"},
        {"action": "screenshot", "label": "关键帧"},
    ]

    rec = ScreenRecorder(recordings_dir=tmp_path, force_backend="mock")
    res = desktop_ai_test(
        app_name="记事本",
        test_script=script,
        recordings_dir=tmp_path,
        recorder=rec,
    )

    assert res["status"] == "success"
    assert res["app_name"] == "记事本"
    assert res["steps_executed"] == 4
    assert Path(res["video_path"]).exists()
