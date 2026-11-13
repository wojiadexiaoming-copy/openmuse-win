"""v2.8 §39.7：browser_evaluate 抓取一致性校验与失败熔断（通用，不含站点特判）。"""
from tools.browser.worker import BrowserWorker


class SeqPage:
    url = "https://example.test/"

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0
        self.main_frame = self
        self.frames = [self]

    def evaluate(self, expression):
        self.calls += 1
        result = self._results.pop(0) if self._results else None
        if isinstance(result, Exception):
            raise result
        return result


def make_worker(*results):
    page = SeqPage(*results)
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = page
    worker._do_guard = lambda: None
    worker._human_delay = lambda: None
    return worker, page


def test_same_query_different_row_count_flagged():
    worker, _ = make_worker(
        [{"train": "K1622"}],
        [{"train": "K1622"}, {"train": "G903"}, {"train": "G1"}, {"train": "G2"}],
    )
    first = worker._do_evaluate({"expression": "fetch('/api/trains?from=TJP&to=IOQ')"})
    assert "[数据不一致]" not in first

    second = worker._do_evaluate({"expression": "fetch('/api/trains?from=TJP&to=IOQ')"})
    assert "[数据不一致]" in second
    assert "返回 1 条" in second and "返回 4 条" in second


def test_repeated_fetch_failure_opens_circuit():
    worker, page = make_worker(
        "TypeError: Failed to fetch",
        "TypeError: Failed to fetch",
        {"ok": True},
    )
    expression = "fetch('/api/x').then(r=>r.json())"
    first = worker._do_evaluate({"expression": expression})
    assert "[抓取未核验]" in first
    second = worker._do_evaluate({"expression": expression})
    assert "[抓取未核验]" in second

    third = worker._do_evaluate({"expression": expression})
    assert "[重复抓取熔断]" in third
    assert page.calls == 2, "熔断后不得再执行同一表达式"


def test_expect_mismatch_flags_data():
    worker, _ = make_worker([
        {"fromStation": "天津西", "toStation": "深圳北", "train": "K1622"}
    ])
    out = worker._do_evaluate({
        "expression": "fetch('/api/trains')",
        "expect": {"from": "北京", "to": "深圳"},
    })
    assert "[数据不一致]" in out
    assert "from 期望 '北京'" in out


def test_expect_match_marks_verified():
    worker, _ = make_worker([
        {"fromStation": "天津", "toStation": "深圳北", "date": "2026-10-22"}
    ])
    out = worker._do_evaluate({
        "expression": "fetch('/api/trains')",
        "expect": {"from": "天津", "to": "深圳", "date": "20261022"},
    })
    assert "[抓取已核验]" in out
    assert "[数据不一致]" not in out


def test_plain_observation_result_unchanged():
    worker, _ = make_worker("ok")
    out = worker._do_evaluate({"expression": "1+1"})
    assert out == "[执行结果] \"ok\""
