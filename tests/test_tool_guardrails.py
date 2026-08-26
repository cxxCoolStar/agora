from agora.tool_guardrails import ToolGuardrailConfig, ToolGuardrailController, canonical_tool_args


def test_canonical_arguments_ignore_key_order() -> None:
    assert canonical_tool_args({"path": "a.txt", "offset": 1}) == canonical_tool_args({"offset": 1, "path": "a.txt"})


def test_identical_failure_warns_then_blocks_when_enabled() -> None:
    guard = ToolGuardrailController(ToolGuardrailConfig(hard_stop_enabled=True))
    for _ in range(5):
        assert guard.before_call("read_file", {"path": "missing.txt"}).allows_execution
        guard.after_call("read_file", {"path": "missing.txt"}, "not found", failed=True)
    decision = guard.before_call("read_file", {"path": "missing.txt"})
    assert decision.action == "block"
    assert decision.code == "repeated_exact_failure_block"


def test_idempotent_same_result_warns_and_blocks_when_enabled() -> None:
    guard = ToolGuardrailController(ToolGuardrailConfig(hard_stop_enabled=True))
    args = {"path": "README.md"}
    assert guard.after_call("read_file", args, "same", failed=False).action == "allow"
    assert guard.after_call("read_file", args, "same", failed=False).code == "idempotent_no_progress_warning"
    for _ in range(3):
        guard.after_call("read_file", args, "same", failed=False)
    assert guard.before_call("read_file", args).code == "idempotent_no_progress_block"


def test_consecutive_duplicate_large_result_is_stubbed_and_warned() -> None:
    guard = ToolGuardrailController()
    result = "x" * 512
    first = guard.observe_call("read_file", {"path": "a.txt"}, result, tool_call_id="call-1")
    second = guard.observe_call("read_file", {"path": "a.txt"}, result)
    third = guard.observe_call("read_file", {"path": "a.txt"}, result)
    assert first.stub is None
    assert second.stub and "call-1" in second.stub
    assert third.notice and "3 consecutive" in third.notice


def test_polling_tools_do_not_receive_identical_call_warning() -> None:
    guard = ToolGuardrailController()
    for _ in range(3):
        observation = guard.observe_call("vendor_get_result", {"job": "1"}, "unchanged")
    assert observation.notice is None


def test_web_search_cap_is_always_enforced() -> None:
    guard = ToolGuardrailController(ToolGuardrailConfig(max_web_searches=2))
    assert guard.before_call("web_search", {"query": "a"}).allows_execution
    assert guard.before_call("web_search", {"query": "b"}).allows_execution
    decision = guard.before_call("web_search", {"query": "c"})
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt
