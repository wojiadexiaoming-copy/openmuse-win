import pytest
from unittest.mock import MagicMock
from tools.browser.human_curve import (
    generate_human_curve,
    human_mouse_move,
    human_mouse_click,
    auto_solve_turnstile,
)


def test_generate_human_curve_short_dist():
    pts = generate_human_curve(100.0, 100.0, 100.5, 100.8)
    assert len(pts) == 1
    assert pts[0][0] == pytest.approx(100.5)
    assert pts[0][1] == pytest.approx(100.8)
    assert pts[0][2] >= 4.0


def test_generate_human_curve_normal_dist():
    pts = generate_human_curve(50.0, 50.0, 500.0, 400.0)
    assert len(pts) >= 15
    # The final point must reach target coordinates
    end_pt = pts[-1]
    assert end_pt[0] == pytest.approx(500.0)
    assert end_pt[1] == pytest.approx(400.0)
    # Check that all points have valid positive delays
    for x, y, delay in pts:
        assert delay >= 4.0


def test_human_mouse_move_mock():
    moved = []

    class MockMouse:
        def move(self, x, y):
            moved.append((x, y))

    class MockPage:
        mouse = MockMouse()
        _current_mouse_pos = (100.0, 100.0)

    page = MockPage()
    human_mouse_move(page, 200.0, 200.0)
    assert len(moved) > 0
    assert page._current_mouse_pos == (200.0, 200.0)


def test_human_mouse_move_no_mouse():
    # Should not raise any exception if page has no mouse
    class EmptyPage:
        pass

    page = EmptyPage()
    human_mouse_move(page, 100.0, 100.0)


def test_human_mouse_click_mock():
    clicked = []
    moved = []

    class MockMouse:
        def move(self, x, y):
            moved.append((x, y))

        def click(self, x, y, delay=0):
            clicked.append((x, y, delay))

    class MockPage:
        mouse = MockMouse()
        _current_mouse_pos = (50.0, 50.0)

    page = MockPage()
    human_mouse_click(page, 150.0, 250.0)
    assert len(clicked) == 1
    assert clicked[0][0] == pytest.approx(150.0)
    assert clicked[0][1] == pytest.approx(250.0)
    assert clicked[0][2] > 0  # Dwell time passed


def test_auto_solve_turnstile_no_cf():
    class CleanPage:
        frames = []

        def query_selector(self, sel):
            return None

    assert auto_solve_turnstile(CleanPage()) is False


def test_auto_solve_turnstile_detects_and_solves():
    class MockFrame:
        url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/test"

    class MockIframeEl:
        def bounding_box(self):
            return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}

    class MockTurnstilePage:
        frames = [MockFrame()]
        _title_calls = 0

        class mouse:
            @staticmethod
            def move(x, y):
                pass

            @staticmethod
            def click(x, y, delay=0):
                pass

        def query_selector(self, sel):
            return MockIframeEl()

        def title(self):
            self._title_calls += 1
            if self._title_calls > 1:
                return "ChatGPT"
            return "稍后"

        def evaluate(self, script):
            return "valid_turnstile_token_1234567890abcdef1234567890"

    page = MockTurnstilePage()
    assert auto_solve_turnstile(page, timeout_sec=2.0) is True
