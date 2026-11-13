"""M2 测试：审批架构（§36）。"""
import threading
import time

from agent.approval import ApprovalManager


def _mgr():
    return ApprovalManager(":memory:", timeout_sec=2)


def test_l1_passthrough():
    m = _mgr()
    assert m.check("file_read", {"path": "a.txt"}) is True


def test_l2_fail_closed_without_ui():
    m = _mgr()
    assert m.check("file_write", {"path": "a.txt", "content": "x"}) is False


def test_l2_approved_once():
    m = _mgr()
    assert m.check("file_write", {"path": "a.txt", "content": "x"},
                   on_ask=lambda c: (True, "once")) is True
    # once：下次还要问
    assert m.check("file_write", {"path": "a.txt", "content": "x"}) is False


def test_session_scope():
    m = _mgr()
    args = {"path": "a.txt", "content": "x"}
    assert m.check("file_write", args, on_ask=lambda c: (True, "session")) is True
    assert m.check("file_write", args) is True  # 本任务内免批


def test_always_scope_and_revoke():
    m = _mgr()
    args = {"path": "a.txt", "content": "x"}
    assert m.check("file_write", args, on_ask=lambda c: (True, "always")) is True
    assert m.check("file_write", args) is True
    wl = m.list_whitelist()
    assert len(wl) == 1
    m.revoke(wl[0]["wkey"])
    assert m.check("file_write", args) is False


def test_l3_escalation_from_l2_source():
    m = _mgr()
    # L2 来源的 file_write → 升级为 L3，按 L3 流程走（仍可批，但永不进白名单逻辑由调用方保证）
    assert m.classify("file_write", {}) == "L2"
    assert m.check("file_write", {"path": "a"}, prov_level=2,
                   on_ask=lambda c: (c.risk == "L3", "once")) is True


def test_batch_merge():
    """同工具同风险的第二张卡并入第一张，共用决策。"""
    m = _mgr()
    results = {}

    def slow_ask(card):
        time.sleep(0.5)
        return True, "once"

    def run(i):
        results[i] = m.check("file_write", {"path": f"{i}.txt", "content": "x"},
                             on_ask=slow_ask)

    t1 = threading.Thread(target=run, args=(1,))
    t1.start()
    time.sleep(0.1)  # 让第一张卡先进入 pending
    t2 = threading.Thread(target=run, args=(2,))
    t2.start()
    t1.join()
    t2.join()
    assert results[1] is True and results[2] is True


def test_stats():
    m = _mgr()
    for _ in range(3):
        m.check("file_write", {"path": "a", "content": "x"},
                on_ask=lambda c: (True, "once"))
    m.check("file_write", {"path": "a", "content": "x"},
            on_ask=lambda c: (False, "once"))
    s = m.stats("file_write")
    assert s == {"approved": 3, "rejected": 1}


def test_humanize_approval():
    from agent.approval import humanize_approval
    act, summary, detail, intent = humanize_approval("shell_exec", {"command": "echo hello"})
    assert act == "执行系统终端命令"
    # summary 说人话，不贴原始命令（2026-10-15 用户要求）
    assert "echo hello" not in summary
    assert "echo hello" in detail  # 原始命令在 detail 里
    assert intent != ""
    # 特定命令的人话摘要
    _, s2, _, _ = humanize_approval("shell_exec", {"command": "taskkill /IM msedge.exe /F"})
    assert s2 == "关闭 Edge 浏览器进程"

    m = _mgr()
    card_captured = []
    m.check("shell_exec", {"command": "python -c \"print(1)\""},
            on_ask=lambda c: (card_captured.append(c) or True, "once"))
    assert len(card_captured) == 1
    assert card_captured[0].action_cn == "执行系统终端命令"
    # summary 说人话，原始命令在 detail 里
    assert "python -c" not in card_captured[0].summary_cn
    assert "python -c" in card_captured[0].detail_cn
    assert card_captured[0].intent_cn != ""


def test_list_history():
    m = _mgr()
    m.check("file_write", {"path": "a.txt", "content": "hello"},
            on_ask=lambda c: (True, "once"))
    m.check("shell_exec", {"command": "taskkill /IM msedge.exe /F"},
            on_ask=lambda c: (False, "once"))
    hist = m.list_history(limit=10)
    assert len(hist) == 2
    # 倒序排列：最新的在最前
    assert hist[0]["action"] == "shell_exec"
    assert hist[0]["action_cn"] == "执行系统终端命令"
    assert hist[0]["decision"] == "rejected"
    assert hist[1]["action"] == "file_write"
    assert hist[1]["action_cn"] == "写入/创建本地文件"
    assert hist[1]["decision"] == "approved"


def test_resolve_card_idempotency_and_versioning():
    m = _mgr()

    # 1. not found
    res = m.resolve_card("non-existent-id", True)
    assert res.get("ok") is False
    assert res.get("reason") == "not_found"

    # Set up a real pending card
    ready_evt = threading.Event()
    def async_ask(card):
        ready_evt.set()
        # Wait for resolve_card to set event
        p = m._pending.get(card.approval_id)
        if p:
            p["event"].wait(timeout=2.0)
            return p.get("decision", False), p.get("scope", "once")
        return False, "once"

    th = threading.Thread(
        target=lambda: m.check("file_write", {"path": "test.txt", "content": "123"}, on_ask=async_ask)
    )
    th.start()
    assert ready_evt.wait(timeout=1.0) is True

    card_id = list(m._pending.keys())[0]

    # 2. Version conflict when providing an obsolete version
    card = m._pending[card_id]["card"]
    bad_version_res = m.resolve_card(card_id, True, version_id=card.version_id + 99)
    assert bad_version_res.get("ok") is False
    assert bad_version_res.get("reason") == "version_conflict"

    # 3. Successful resolution
    good_res = m.resolve_card(card_id, True, version_id=card.version_id)
    assert good_res.get("ok") is True
    assert good_res.get("approved") is True

    # 4. Repeated resolution should return already_consumed
    repeat_res = m.resolve_card(card_id, True)
    assert repeat_res.get("ok") is False
    assert repeat_res.get("reason") == "already_consumed"

    th.join(timeout=1.0)


def test_settings_policy_and_target_overrides(tmp_path):
    from agent.settings import Settings

    settings_file = tmp_path / "settings.json"
    settings = Settings(settings_file)
    mgr = ApprovalManager(":memory:", settings=settings)

    # 1. By default, desktop_click is allow (L1 routine tool)
    assert mgr.check("desktop_click", {"x": 100, "y": 100}) is True

    # 2. User explicitly configures desktop_click = ask
    settings.set("desktop_click", "ask")
    # Now desktop_click must prompt (not short-circuited by L1)
    asked = []
    res = mgr.check("desktop_click", {"x": 100, "y": 100}, on_ask=lambda c: (asked.append(c) or True, "once"))
    assert res is True
    assert len(asked) == 1
    assert asked[0].action == "desktop_click"

    # 3. User sets target override: deny for app:Recordly
    settings.set("desktop_click", "allow")
    settings.set("desktop_click", "deny", target="app:Recordly")

    # Clicking inside Recordly must be denied
    assert mgr.check("desktop_click", {"app": "Recordly", "x": 100, "y": 100}) is False

    # Clicking outside Recordly (e.g. Notepad) must be allowed
    assert mgr.check("desktop_click", {"app": "Notepad", "x": 100, "y": 100}) is True

    # 4. User sets target override: ask for domain:sensitive.com
    settings.set("browser_click", "allow")
    settings.set("browser_click", "ask", target="domain:sensitive.com")

    asked_browser = []
    b_res = mgr.check("browser_click", {"url": "https://sensitive.com/pay", "selector": "#btn"},
                      on_ask=lambda c: (asked_browser.append(c) or True, "once"))
    assert b_res is True
    assert len(asked_browser) == 1

    # browser_click on other domains is allowed without asking
    assert mgr.check("browser_click", {"url": "https://example.com", "selector": "#btn"}) is True




