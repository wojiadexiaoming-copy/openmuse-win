"""v2.6/v2.7 修复回归：① 导航落地重定向反馈与累计熔断（纯函数）；② shell_exec 白名单拒绝回带清单。

背景：2026-10-21 任务 25 步复盘——列表页被站点静默重定向回首页，"[已打开]"掩盖真相；
2026-10-22 复盘发现旧计数器按路径聚合，某个查询参数变体成功落地会清空同路径全部累计，
坏 URL 形态可以无限重试绕过熔断。v2.7 改为 exact(路径+规范化查询) 与 path 双维度累计。
"""
from tools.browser.worker import BrowserWorker
from tools.shell_exec import _run as shell_run


def test_redirect_feedback_warns_on_same_host_path_change():
    hist = {}
    note = BrowserWorker._redirect_feedback(
        "https://flights.example.com/online/list/round-she-bkk?depdate=2026-10-23&retdate=2026-10-25",
        "https://flights.example.com/online/channel", hist)
    assert "[URL重定向]" in note
    assert "/online/list/round-she-bkk" in note and "/online/channel" in note
    assert "铁律17" not in note  # 首次只提示不熔断


def test_redirect_feedback_escalates_after_repeat_variants():
    """同一精确 URL 变形重复被重定向，累计 ≥2 次即熔断。"""
    hist = {}
    for _ in range(2):
        note = BrowserWorker._redirect_feedback(
            "https://flights.example.com/online/list/round-she-bkk?depdate=x",
            "https://flights.example.com/online/channel", hist)
    assert "[重复重定向熔断·铁律17]" in note
    assert "累计 2 次" in note
    assert "禁止再次直接拼接同类 URL" in note


def test_redirect_feedback_normalizes_query_variants():
    """查询参数顺序不同但语义相同 → 命中同一个 exact 计数器。"""
    hist = {}
    BrowserWorker._redirect_feedback(
        "https://x.com/list/a?b=2&a=1", "https://x.com/home", hist)
    note = BrowserWorker._redirect_feedback(
        "https://x.com/list/a?a=1&b=2", "https://x.com/home", hist)
    assert "[重复重定向熔断·铁律17]" in note


def test_redirect_feedback_success_does_not_clear_other_url_forms():
    """回归：好形态 B 成功落地，不得清掉坏形态 A 的 exact 累计。"""
    hist = {}
    bad = "https://x.com/online/list/a?d=1"
    good = "https://x.com/online/list/a?d=2"
    BrowserWorker._redirect_feedback(bad, "https://x.com/home", hist)
    # 同路径另一个查询变体成功 → 只清它自己的 exact 键与 path 键
    assert BrowserWorker._redirect_feedback(good, good, hist) == ""
    # 坏形态再次被重定向 → exact 累计到 2，必须熔断
    note = BrowserWorker._redirect_feedback(bad, "https://x.com/home", hist)
    assert "[重复重定向熔断·铁律17]" in note


def test_redirect_feedback_other_path_success_keeps_path_counter():
    """回归：不同路径的成功落地不得重置本路径的累计。"""
    hist = {}
    bad = "https://x.com/online/list/a?d=1"
    BrowserWorker._redirect_feedback(bad, "https://x.com/home", hist)
    assert BrowserWorker._redirect_feedback(
        "https://x.com/other/page", "https://x.com/other/page", hist) == ""
    note = BrowserWorker._redirect_feedback(bad, "https://x.com/home", hist)
    assert "[重复重定向熔断·铁律17]" in note


def test_redirect_feedback_resets_when_target_reached():
    hist = {}
    for _ in range(2):
        BrowserWorker._redirect_feedback(
            "https://x.com/online/list/a?d=1", "https://x.com/home", hist)
    # 第三次终于落到所求路径 → 计数清零且无提示
    note = BrowserWorker._redirect_feedback(
        "https://x.com/online/list/a?d=1", "https://x.com/online/list/a?d=1", hist)
    assert note == ""
    assert hist == {}
    # 之后再被重定向从 1 重新计起，不携带历史熔断
    note = BrowserWorker._redirect_feedback(
        "https://x.com/online/list/a?d=1", "https://x.com/home", hist)
    assert "[URL重定向]" in note and "铁律17" not in note


def test_redirect_feedback_silent_on_normal_cases():
    hist = {}
    # 同路径落地（含 query/尾部斜杠差异）→ 无提示
    assert BrowserWorker._redirect_feedback(
        "https://x.com/list/a", "https://x.com/list/a/", hist) == ""
    # 跨域跳转（登录流程等）→ 不干预
    assert BrowserWorker._redirect_feedback(
        "https://x.com/login?next=/a", "https://auth.y.com/confirm", hist) == ""
    # 异常输入 → 无提示
    assert BrowserWorker._redirect_feedback("", "", hist) == ""


def test_shell_exec_rejection_carries_whitelist_hint():
    r = shell_run({"command": "date"})
    assert "[拒绝] 不在白名单: date" in r
    assert "可用命令" in r
    assert "python" in r and "git" in r  # 引导模型改用白名单内等价命令
    assert "重复提交白名单外的命令只会浪费步数" in r
