"""Hermes-inspired, per-run guardrails for tool-call loops.

The controller is deliberately side-effect free.  A future tool executor owns
whether a decision becomes a warning appended to a tool result, a synthetic
result, or a controlled finalization of the run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


IDEMPOTENT_TOOLS = frozenset({"list_dir", "read_file", "search_files", "web_search", "web_fetch"})
MUTATING_TOOLS = frozenset({"write_file", "patch", "exec", "delegate_task"})
REPEATABLE_TOOLS = frozenset({"process"})
REPEATABLE_SUFFIXES = ("_poll", "_get_result")


def canonical_tool_args(args: Mapping[str, Any] | None) -> str:
    """Return a stable representation so equivalent JSON has one signature."""
    if args is not None and not isinstance(args, Mapping):
        raise TypeError("tool arguments must be an object")
    return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        return cls(tool_name=tool_name, args_hash=_hash(canonical_tool_args(args)))


@dataclass(frozen=True)
class ToolGuardrailDecision:
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature:
            data["signature"] = {"tool_name": self.signature.tool_name, "args_hash": self.signature.args_hash}
        return data


@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for one Agent run. Warnings are on; hard stops opt in."""

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    identical_call_warn_after: int = 3
    max_web_searches: int = 50
    max_subagents: int = 50
    duplicate_result_stub_min_chars: int = 512
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOLS)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOLS)


@dataclass(frozen=True)
class IdenticalCallObservation:
    notice: str | None = None
    stub: str | None = None


class ToolGuardrailController:
    """Track repeated failures and no-progress calls for a single Agent run."""

    def __init__(self, config: ToolGuardrailConfig | None = None) -> None:
        self.config = config or ToolGuardrailConfig()
        self.reset()

    def reset(self) -> None:
        self._exact_failures: dict[ToolCallSignature, int] = {}
        self._same_tool_failures: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._identical_signature: ToolCallSignature | None = None
        self._identical_result_hash = ""
        self._identical_count = 0
        self._first_call_id = ""
        self._web_searches = 0
        self._subagents = 0
        self.halt_decision: ToolGuardrailDecision | None = None

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)
        cap_decision = self._check_cap(tool_name, args or {}, signature)
        if cap_decision:
            return cap_decision
        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        failures = self._exact_failures.get(signature, 0)
        if failures >= self.config.exact_failure_block_after:
            return self._halt(
                "block", "repeated_exact_failure_block", tool_name, failures, signature,
                f"Blocked {tool_name}: identical arguments have failed {failures} times. Change strategy instead of retrying unchanged.",
            )
        if self._is_idempotent(tool_name):
            _result_hash, count = self._no_progress.get(signature, ("", 0))
            if count >= self.config.no_progress_block_after:
                return self._halt(
                    "block", "idempotent_no_progress_block", tool_name, count, signature,
                    f"Blocked {tool_name}: the same read-only call returned the same result {count} times. Use it or change the query.",
                )
        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool,
    ) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed:
            exact_count = self._exact_failures.get(signature, 0) + 1
            same_count = self._same_tool_failures.get(tool_name, 0) + 1
            self._exact_failures[signature] = exact_count
            self._same_tool_failures[tool_name] = same_count
            self._no_progress.pop(signature, None)
            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                return self._halt(
                    "halt", "same_tool_failure_halt", tool_name, same_count, signature,
                    f"Stopped {tool_name}: it failed {same_count} times this run. Diagnose or use a different approach.",
                )
            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return self._decision(
                    "warn", "repeated_exact_failure_warning", tool_name, exact_count, signature,
                    f"{tool_name} failed {exact_count} times with identical arguments. Inspect the error and change strategy.",
                )
            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return self._decision(
                    "warn", "same_tool_failure_warning", tool_name, same_count, signature,
                    f"{tool_name} failed {same_count} times this run. Diagnose before trying it again.",
                )
            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failures.pop(signature, None)
        self._same_tool_failures.pop(tool_name, None)
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _hash(result or "")
        previous_hash, previous_count = self._no_progress.get(signature, ("", 0))
        count = previous_count + 1 if previous_hash == result_hash else 1
        self._no_progress[signature] = (result_hash, count)
        if self.config.warnings_enabled and count >= self.config.no_progress_warn_after:
            return self._decision(
                "warn", "idempotent_no_progress_warning", tool_name, count, signature,
                f"{tool_name} returned the same result {count} times. Use the existing result or change the query.",
            )
        return ToolGuardrailDecision(tool_name=tool_name, count=count, signature=signature)

    def observe_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        tool_call_id: str = "",
        failed: bool = False,
    ) -> IdenticalCallObservation:
        """Return context-safe guidance for consecutive identical successful calls."""
        signature = ToolCallSignature.from_call(tool_name, args)
        is_text = isinstance(result, str)
        result_hash = _hash(result) if is_text else ""
        if is_text and signature == self._identical_signature and result_hash == self._identical_result_hash:
            self._identical_count += 1
        else:
            self._identical_signature = signature if is_text else None
            self._identical_result_hash = result_hash
            self._identical_count = 1 if is_text else 0
            self._first_call_id = tool_call_id if is_text else ""

        notice = None
        if self._identical_count >= self.config.identical_call_warn_after and not self._is_repeatable(tool_name):
            notice = (
                f"[Agora guardrail: {self._identical_count} consecutive identical {tool_name} calls returned the same result. "
                "Do not repeat it; change arguments, use another tool, or proceed with the result already available.]"
            )
        stub = None
        if (
            is_text
            and not failed
            and self._identical_count >= 2
            and len(result) >= self.config.duplicate_result_stub_min_chars
        ):
            reference = f" (tool_call_id {self._first_call_id})" if self._first_call_id else ""
            stub = f"[Agora guardrail: this result is byte-identical to an earlier {tool_name} result{reference}; refer to that result instead.]"
        return IdenticalCallObservation(notice=notice, stub=stub)

    def _check_cap(
        self, tool_name: str, args: Mapping[str, Any], signature: ToolCallSignature
    ) -> ToolGuardrailDecision | None:
        if tool_name == "web_search":
            if self.config.max_web_searches and self._web_searches >= self.config.max_web_searches:
                return self._halt(
                    "block", "loop_web_search_cap", tool_name, self._web_searches, signature,
                    f"Blocked web_search: this run has reached its {self.config.max_web_searches} call limit.",
                )
            self._web_searches += 1
        elif tool_name == "delegate_task" and args.get("action", "spawn") == "spawn":
            if self.config.max_subagents and self._subagents >= self.config.max_subagents:
                return self._halt(
                    "block", "loop_subagent_cap", tool_name, self._subagents, signature,
                    f"Blocked delegate_task: this run has reached its {self.config.max_subagents} subagent limit.",
                )
            self._subagents += 1
        return None

    def _is_idempotent(self, tool_name: str) -> bool:
        return tool_name not in self.config.mutating_tools and tool_name in self.config.idempotent_tools

    @staticmethod
    def _is_repeatable(tool_name: str) -> bool:
        return tool_name in REPEATABLE_TOOLS or tool_name.endswith(REPEATABLE_SUFFIXES)

    def _halt(
        self, action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature, message: str
    ) -> ToolGuardrailDecision:
        decision = self._decision(action, code, tool_name, count, signature, message)
        self.halt_decision = decision
        return decision

    @staticmethod
    def _decision(
        action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature, message: str
    ) -> ToolGuardrailDecision:
        return ToolGuardrailDecision(action, code, message, tool_name, count, signature)


def append_guardrail_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    return f"{result}\n\n[Tool loop {decision.action}: {decision.code}; {decision.message}]"


def guardrail_synthetic_result(decision: ToolGuardrailDecision) -> str:
    return json.dumps({"error": decision.message, "guardrail": decision.to_metadata()}, ensure_ascii=False)
