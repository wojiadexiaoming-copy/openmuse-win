"""Isolated UIA tests: no real focus, clicks, typing, or private windows."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tools import desktop_accessibility as uia


class Info:
    def __init__(self, identifier, name="", kind="Edit", *, password=False,
                 focused=False, enabled=True, focusable=True, handle=0):
        self.runtime_id = (42, identifier)
        self.handle = handle
        self._name = name
        self.control_type = kind
        self.rectangle = SimpleNamespace(left=0, top=0, right=200, bottom=100)
        self.element = SimpleNamespace(
            CurrentIsPassword=password, CurrentIsEnabled=enabled,
            CurrentIsKeyboardFocusable=focusable, CurrentHasKeyboardFocus=focused,
        )
        self.children = []
        self.parent = None
        self.patterns = {}

    @property
    def name(self):
        if self.element.CurrentIsPassword:
            raise AssertionError("Password name must never be read")
        return self._name

    def add(self, *children):
        self.children.extend(children)
        for child in children:
            child.parent = self
        return self


class Backend:
    def __init__(self, windows, focused=None):
        self.roots = {window.handle: window for window in windows}
        self.active = windows[0].handle if windows else 0
        self.current_focus = focused
        self.focus_calls = []
        self.caret = None

    def foreground(self):
        return self.active

    def is_window(self, hwnd):
        return hwnd in self.roots

    def windows(self):
        return [(handle, info.name) for handle, info in self.roots.items()]

    def window(self, hwnd):
        return self.roots[hwnd]

    def focused(self):
        return self.current_focus

    def parent(self, info):
        return info.parent

    def children(self, info):
        return iter(info.children)

    def pattern(self, info, name):
        return info.patterns.get(name)

    def set_focus(self, info):
        self.focus_calls.append(info)
        if self.current_focus:
            self.current_focus.element.CurrentHasKeyboardFocus = False
        self.current_focus = info
        info.element.CurrentHasKeyboardFocus = True

    def native_caret(self, hwnd):
        return self.caret


@pytest.fixture
def install_backend(monkeypatch):
    def install(backend):
        @contextmanager
        def fake_backend():
            yield backend
        monkeypatch.setattr(uia, "_backend", fake_backend)
        return backend
    return install


def window(identifier=1, title="Target", handle=100):
    return Info(identifier, title, "Window", handle=handle)


def call(operation, **arguments):
    return uia._execute(operation, arguments)


def test_probe_reads_controls_without_activating_and_keeps_runtime_refs(install_backend):
    edit = Info(2, "Prompt")
    root = window().add(edit)
    backend = install_backend(Backend([root], edit))

    result = call("probe_window", window_title="Target", max_controls=80)

    assert result["ok"] and result["supported"]
    assert result["window"] == {
        "hwnd": 100, "title": "Target",
        "rect": {"left": 0, "top": 0, "right": 200, "bottom": 100},
        "foreground": True,
    }
    assert result["controls"][0]["control_ref"] == "uia:42.2"
    assert result["controls"][0]["name"] == "Prompt"
    assert backend.focus_calls == []


def test_exact_window_wins_but_ambiguous_partial_window_is_rejected(install_backend):
    one, two = window(), window(2, "Target settings", 200)
    backend = install_backend(Backend([one, two]))
    selected, error = uia._select_window(backend, "Target")
    assert selected is one and error is None
    selected, error = uia._select_window(backend, "Targ")
    assert selected is None and error["error_type"] == "window_ambiguous"
    assert "settings" not in str(error)


def test_duplicate_exact_titles_are_ambiguous(install_backend):
    backend = install_backend(Backend([window(), window(2, "Target", 200)]))
    selected, error = uia._select_window(backend, "Target")
    assert selected is None and error["error_type"] == "window_ambiguous"


def test_empty_probe_and_unreadable_password_flag_are_not_success(install_backend):
    root = window()
    install_backend(Backend([root]))
    result = call("probe_window", hwnd=100, max_controls=80)
    assert not result["ok"] and result["error_type"] == "no_controls"

    unreadable = Info(2, "private")
    unreadable.element = SimpleNamespace()
    root.add(unreadable)
    result = call("probe_window", hwnd=100, max_controls=80)
    assert not result["ok"] and result["error_type"] == "controls_unreadable"


def test_enumeration_is_bounded_even_for_a_cyclic_provider(install_backend):
    root, child = window(), Info(2, "Loop", "Pane")
    root.add(child)
    child.children = [child]
    install_backend(Backend([root]))
    result = call("probe_window", hwnd=100, max_controls=5)
    assert len(result["controls"]) == 5
    assert result["truncated"]


def test_password_snapshot_and_focus_never_read_name_or_value(install_backend):
    class PasswordPattern:
        CurrentIsReadOnly = False
        @property
        def CurrentValue(self):
            raise AssertionError("Password value must never be read")

    edit = Info(2, "secret password", password=True, focused=True)
    edit.patterns["Value"] = PasswordPattern()
    root = window().add(edit)
    install_backend(Backend([root], edit))
    snapshot = call("probe_window", hwnd=100, max_controls=80)
    assert snapshot["controls"][0]["name"] == ""
    assert snapshot["controls"][0]["is_password"]
    result = call("read_focus", hwnd=100)
    assert result["ok"] and result["focus"]["is_password"]
    assert result["focus"]["name"] == ""
    assert not result["focus"]["value_supported"]
    assert "value" not in result["focus"]


def test_writable_edit_value_pattern_confirms_focus_and_text(install_backend):
    edit = Info(2, "Prompt", focused=True)
    edit.patterns["Value"] = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue="hello")
    install_backend(Backend([window().add(edit)], edit))

    result = call("read_focus", hwnd=100)

    assert result["ok"]
    assert result["focus"]["editable"]
    assert result["focus"]["value_supported"]
    assert result["focus"]["value"] == "hello"
    assert "writable_value_pattern" in result["focus"]["evidence"]


def test_document_type_or_readonly_value_alone_never_proves_editability(install_backend):
    document = Info(2, "Document", "Document", focused=True)
    document.patterns["Value"] = SimpleNamespace(CurrentIsReadOnly=True, CurrentValue="read only")
    install_backend(Backend([window().add(document)], document))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and not result["focus"]["editable"]


def test_active_text_pattern2_caret_supports_contenteditable(install_backend):
    edit = Info(2, "Composer", "Document", focused=True)
    caret_range = SimpleNamespace(GetBoundingRectangles=lambda: [10, 20, 1, 16])
    edit.patterns["TextV2"] = SimpleNamespace(
        GetCaretRange=lambda: (True, caret_range),
        DocumentRange=SimpleNamespace(GetText=lambda limit: "typed prompt"),
    )
    install_backend(Backend([window().add(edit)], edit))
    result = call("read_focus", hwnd=100)
    focus = result["focus"]
    assert result["ok"] and focus["editable"]
    assert focus["value"] == "typed prompt"
    assert focus["caret_rect"] == {"left": 10, "top": 20, "right": 11, "bottom": 36}
    assert "active_text_pattern2_caret" in focus["evidence"]


def test_inactive_text_pattern2_caret_is_not_editable(install_backend):
    edit = Info(2, "Page", "Document", focused=True)
    edit.patterns["TextV2"] = SimpleNamespace(GetCaretRange=lambda: (False, None))
    install_backend(Backend([window().add(edit)], edit))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and not result["focus"]["editable"]
    assert not result["focus"]["value_supported"]


def test_native_caret_must_match_target_and_focused_control_rect(install_backend):
    edit = Info(2, "Composer", "Custom", focused=True)
    backend = install_backend(Backend([window().add(edit)], edit))
    backend.caret = {"hwnd": 100, "rect": {"left": 10, "top": 20, "right": 11, "bottom": 36}}
    result = call("read_focus", hwnd=100)
    assert result["focus"]["editable"]
    assert result["focus"]["caret_rect"] == backend.caret["rect"]
    assert not result["focus"]["value_supported"]

    backend.caret["hwnd"] = 200
    assert not call("read_focus", hwnd=100)["focus"]["editable"]
    backend.caret["hwnd"] = 100
    backend.caret["rect"]["left"] = 300
    assert not call("read_focus", hwnd=100)["focus"]["editable"]


def test_focus_rejects_other_foreground_and_unrelated_focused_element(install_backend):
    edit = Info(2, "Prompt", focused=True)
    other = window(3, "Private other app", 200).add(Info(4, "private text", focused=True))
    backend = install_backend(Backend([window().add(edit), other], edit))
    backend.active = 200
    result = call("read_focus", hwnd=100)
    assert not result["ok"] and result["error_type"] == "window_not_foreground"
    assert "Private" not in str(result) and "private text" not in str(result)
    backend.active = 100
    backend.current_focus = other.children[0]
    result = call("read_focus", hwnd=100)
    assert not result["ok"] and result["error_type"] == "focus_outside_window"
    assert "private text" not in str(result)


def test_focus_control_confirms_exact_reference_after_set_focus(install_backend):
    edit = Info(2, "Prompt")
    edit.patterns["Value"] = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue="")
    backend = install_backend(Backend([window().add(edit)]))
    result = call("focus_control", hwnd=100, control_ref="uia:42.2")
    assert result["ok"] and result["focus"]["editable"]
    assert backend.focus_calls == [edit]
    assert "requested_control_ref_match" in result["focus"]["evidence"]


def test_duplicate_control_reference_does_not_focus_arbitrary_first(install_backend):
    one, two = Info(2, "One"), Info(2, "Two")
    backend = install_backend(Backend([window().add(one, two)]))
    result = call("focus_control", hwnd=100, control_ref="uia:42.2")
    assert not result["ok"] and result["error_type"] == "control_ambiguous"
    assert backend.focus_calls == []


def test_focus_failure_is_not_reported_as_success(install_backend):
    edit = Info(2, "Prompt")
    backend = install_backend(Backend([window().add(edit)]))
    backend.set_focus = lambda info: None
    result = call("focus_control", hwnd=100, control_ref="uia:42.2")
    assert not result["ok"]
    assert result["error_type"] == "focus_outside_window"


def test_backend_missing_is_explicit_and_does_not_expose_exception_content(monkeypatch):
    @contextmanager
    def missing_backend():
        raise uia.BackendUnavailable("backend_missing")
        yield
    monkeypatch.setattr(uia, "_backend", missing_backend)
    result = call("read_focus", hwnd=100)
    assert result == {"supported": False, "ok": False, "error_type": "backend_missing"}


@pytest.mark.parametrize("operation, arguments", [
    ("probe_window", {"window_title": None}),
    ("probe_window", {"hwnd": -1}),
    ("probe_window", {"max_controls": 0}),
    ("focus_control", {"hwnd": 100, "control_ref": "bad"}),
    ("read_focus", {"hwnd": True}),
])
def test_public_invalid_arguments_never_start_worker(monkeypatch, operation, arguments):
    monkeypatch.setattr(uia, "_run_isolated", lambda *args: pytest.fail("must not spawn"))
    result = getattr(uia, operation)(**arguments)
    assert not result["ok"] and result["error_type"] == "invalid_arguments"


def test_public_probe_clamps_enumeration_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(uia, "_run_isolated", lambda *args: calls.append(args) or {})
    uia.probe_window("Target", max_controls=10000)
    assert calls[0][1]["max_controls"] == uia.MAX_CONTROLS


def test_timeout_terminates_worker_instead_of_waiting_for_hung_uia(monkeypatch):
    calls = []
    class Connection:
        def poll(self, timeout):
            calls.append(("poll", timeout))
            return False
        def close(self):
            pass
    class Process:
        pid = None
        alive = True
        def start(self):
            self.pid = 123
        def join(self, timeout):
            calls.append(("join", timeout))
        def is_alive(self):
            return self.alive
        def terminate(self):
            calls.append(("terminate",))
            self.alive = False
        def close(self):
            pass
    receiver, sender = Connection(), Connection()
    process = Process()
    context = SimpleNamespace(Pipe=lambda duplex: (receiver, sender),
                              Process=lambda **kwargs: process)
    monkeypatch.setattr(uia.multiprocessing, "get_context", lambda method: context)
    result = uia._run_isolated("read_focus", {"hwnd": 100})
    assert result["error_type"] == "uia_timeout"
    assert ("terminate",) in calls
    assert ("poll", uia.UIA_TIMEOUT_SECONDS) in calls


def test_long_left_sidebar_cannot_starve_nested_right_editor(install_backend):
    left = Info(2, "Sidebar", "Pane", focusable=False)
    for index in range(1000):
        left.add(Info(1000 + index, "history item", "Text", focusable=False))
    edit = Info(9, "Composer", "Edit")
    right = Info(3, "Content", "Pane", focusable=False)
    inner = Info(4, "Inner", "Group", focusable=False)
    deeper = Info(5, "Composer container", "Pane", focusable=False).add(edit)
    right.add(inner.add(deeper))
    root = window().add(left, right)
    install_backend(Backend([root]))

    result = call("probe_window", hwnd=100, max_controls=80)

    assert result["ok"]
    assert len(result["controls"]) <= 80
    assert result["controls"][0]["control_ref"] == "uia:42.9"
    assert result["controls"][0]["control_type"] == "Edit"
    assert result["scanned_controls"] <= uia.MAX_SEARCH_CONTROLS
    assert result["truncated"]


def test_wide_tree_output_prioritizes_editor_beyond_first_eighty(install_backend):
    root = window()
    for index in range(150):
        root.add(Info(1000 + index, "history item", "Text", focusable=False))
    edit = Info(9, "Composer", "Edit")
    root.add(edit)
    install_backend(Backend([root]))

    result = call("probe_window", hwnd=100, max_controls=80)

    assert result["ok"]
    assert len(result["controls"]) == 80
    assert result["controls"][0]["control_ref"] == "uia:42.9"
    assert result["scanned_controls"] == 151
    assert result["truncated"]


def test_fair_walker_and_snapshot_never_exceed_global_scan_budget(install_backend):
    root = window()
    root.add(*(Info(1000 + index, "", "Text", focusable=False) for index in range(1000)))
    backend = install_backend(Backend([root]))

    assert len(list(uia._walk(backend, root, 17))) == 17
    result = call("probe_window", hwnd=100, max_controls=3)
    assert len(result["controls"]) == 3
    assert result["scanned_controls"] == uia.MAX_SEARCH_CONTROLS
    assert result["truncated"]


def test_com_apartment_reuses_existing_sta_and_balances_initialization(monkeypatch):
    import sys
    calls = []
    class ChangedApartment(Exception):
        hresult = -2147417850
    def initialize(flags):
        calls.append(flags)
        if flags == 0:
            raise ChangedApartment()
    fake_comtypes = SimpleNamespace(CoInitializeEx=initialize,
                                    CoUninitialize=lambda: calls.append("uninitialize"))
    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "coinit_flags", 0, raising=False)
    marker = object()
    monkeypatch.setattr(uia, "_NativeUIA", lambda: marker)
    with uia._backend() as backend:
        assert backend is marker
    assert calls == [0, 2, "uninitialize"]


def test_native_handle_reference_fallback_when_runtime_id_unreadable():
    class RuntimeUnavailable:
        handle = 123
        @property
        def runtime_id(self):
            raise RuntimeError("unavailable")
    assert uia._reference(RuntimeUnavailable()) == "hwnd:123"


def text_pattern(document_readonly=False, selected_readonly=False, *, selection_count=1):
    selected = SimpleNamespace(GetAttributeValue=lambda attribute: selected_readonly)
    ranges = SimpleNamespace(Length=selection_count, GetElement=lambda index: selected)
    document = SimpleNamespace(GetAttributeValue=lambda attribute: document_readonly,
                               GetText=lambda limit: "actual composer text")
    return SimpleNamespace(DocumentRange=document, GetSelection=lambda: ranges)


def test_focused_group_is_editable_only_with_writable_full_range_and_single_selection(install_backend):
    group = Info(2, "Composer", "Group", focused=True)
    group.patterns["Text"] = text_pattern()
    group.patterns["LegacyIAccessible"] = SimpleNamespace(CurrentRole=20, CurrentState=0x100004)
    install_backend(Backend([window().add(group)], group))

    result = call("read_focus", hwnd=100)

    assert result["ok"] and result["focus"]["editable"]
    assert result["focus"]["value_supported"]
    assert result["focus"]["value"] == "actual composer text"
    assert "writable_text_range_and_single_selection" in result["focus"]["evidence"]
    assert "active_text_pattern2_caret" not in result["focus"]["evidence"]
    assert "writable_value_pattern" not in result["focus"]["evidence"]


class FalseyReservedAttribute:
    def __bool__(self):
        return False


@pytest.mark.parametrize("attribute", [True, None, 0, "", "false", FalseyReservedAttribute(), object()])
@pytest.mark.parametrize("position", ["document", "selection"])
def test_text_readonly_unknown_mixed_unsupported_or_true_never_proves_editability(
        install_backend, attribute, position):
    group = Info(2, "Composer", "Group", focused=True)
    group.patterns["Text"] = text_pattern(
        document_readonly=attribute if position == "document" else False,
        selected_readonly=attribute if position == "selection" else False,
    )
    install_backend(Backend([window().add(group)], group))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and not result["focus"]["editable"]
    assert not result["focus"]["value_supported"]
    assert "writable_text_range_and_single_selection" not in result["focus"]["evidence"]


@pytest.mark.parametrize("selection_count", [0, 2, True, "1", None])
def test_text_pattern_requires_one_current_selection(install_backend, selection_count):
    group = Info(2, "Composer", "Group", focused=True)
    group.patterns["Text"] = text_pattern(selection_count=selection_count)
    install_backend(Backend([window().add(group)], group))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and not result["focus"]["editable"]


def test_missing_current_selection_and_attribute_errors_fail_closed(install_backend):
    group = Info(2, "Composer", "Group", focused=True)
    pattern = text_pattern()
    pattern.GetSelection = lambda: None
    group.patterns["Text"] = pattern
    install_backend(Backend([window().add(group)], group))
    assert not call("read_focus", hwnd=100)["focus"]["editable"]

    def unavailable(attribute):
        raise RuntimeError("attribute not supported")
    pattern.DocumentRange.GetAttributeValue = unavailable
    assert not call("read_focus", hwnd=100)["focus"]["editable"]


def test_password_group_never_queries_text_range_or_reads_text(install_backend):
    group = Info(2, "password", "Group", focused=True, password=True)
    def forbidden(*args):
        pytest.fail("Password text attributes or values must not be read")
    group.patterns["Text"] = SimpleNamespace(
        DocumentRange=SimpleNamespace(GetAttributeValue=forbidden, GetText=forbidden),
        GetSelection=forbidden,
    )
    install_backend(Backend([window().add(group)], group))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and result["focus"]["is_password"]
    assert not result["focus"]["editable"]
    assert not result["focus"]["value_supported"]
    assert "value" not in result["focus"]


def test_group_focus_and_legacy_grouping_role_without_text_evidence_remain_noneditable(install_backend):
    group = Info(2, "Composer", "Group", focused=True)
    group.patterns["LegacyIAccessible"] = SimpleNamespace(CurrentRole=20, CurrentState=0x100004)
    install_backend(Backend([window().add(group)], group))
    result = call("read_focus", hwnd=100)
    assert result["ok"] and not result["focus"]["editable"]
