"""Bounded agentic loop executor for richer node execution.

A node eligible for agentic_loop mode runs an internal tool-use loop:
1. Build initial context (task description + available tools)
2. LLM decides: final_answer OR tool_call(name, args)
3. If tool_call: execute tool, add observation, loop back to 2
4. If final_answer or budget exhausted: emit node output

Single-intent contract enforced per LLM response: tool_calls XOR final_answer.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.logging import log_event
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderToolResponse,
    ResolvedModelConfig,
    ToolCall,
)

logger = logging.getLogger(__name__)


@dataclass
class NodeLoopConfig:
    """Configuration for a node's agentic execution loop."""

    max_iterations: int = 10
    max_duration_seconds: int = 120
    max_total_tokens: int = 50_000
    available_tools: list[str] = field(default_factory=list)
    require_final_answer: bool = True
    allow_partial_on_budget: bool = True


@dataclass
class NodeLoopIteration:
    """Record of a single iteration in the node loop."""

    iteration_index: int
    action: str  # "tool_call" or "final_answer"
    tool_name: Optional[str] = None
    tool_args: Optional[dict[str, Any]] = None
    observation_summary: Optional[str] = None
    tool_status: Optional[str] = None  # "success", "error", "timeout"
    tool_error: Optional[str] = None
    tool_execution_duration_ms: Optional[int] = None
    tokens_used: int = 0
    duration_ms: int = 0


@dataclass
class NodeLoopResult:
    """Result of a complete node loop execution."""

    final_output: dict[str, Any]
    total_tokens_used: int
    iteration_count: int
    budget_exhausted: bool
    iteration_trace: list[NodeLoopIteration] = field(default_factory=list)


ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


def execute_node_loop(
    *,
    config: NodeLoopConfig,
    adapter: BaseProviderAdapter,
    system_prompt: str,
    task_prompt: str,
    tool_definitions: list[dict[str, Any]],
    tool_executors: dict[str, ToolExecutor],
    run_context: dict[str, Any] | None = None,
) -> NodeLoopResult:
    """Execute a bounded agentic tool-use loop.

    The loop calls the LLM with tool definitions, executes requested tools,
    feeds observations back, and repeats until:
    - LLM returns a final answer (no tool_calls)
    - max_iterations reached
    - max_duration_seconds elapsed
    - max_total_tokens consumed

    Args:
        config: Loop budget and policy configuration.
        adapter: LLM provider adapter with chat_with_tools support.
        system_prompt: System prompt describing the agent's role.
        task_prompt: User prompt describing the task.
        tool_definitions: OpenAI function-calling format tool definitions.
        tool_executors: Map of tool_name -> callable(tool_name, tool_args) -> result_dict.
        run_context: Optional context dict for traceability.

    Returns:
        NodeLoopResult with final output, token usage, and iteration trace.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]
    iteration_trace: list[NodeLoopIteration] = []
    total_tokens = 0
    start_time = time.monotonic()
    budget_exhausted = False
    final_content: str | None = None

    for iteration_index in range(config.max_iterations):
        elapsed_seconds = time.monotonic() - start_time
        if elapsed_seconds >= config.max_duration_seconds:
            budget_exhausted = True
            log_event(logger, logging.WARNING, "node_loop.budget.time", iteration=iteration_index, elapsed=elapsed_seconds)
            break
        if total_tokens >= config.max_total_tokens:
            budget_exhausted = True
            log_event(logger, logging.WARNING, "node_loop.budget.tokens", iteration=iteration_index, total_tokens=total_tokens)
            break

        iteration_start = time.monotonic()
        try:
            response = adapter.chat_with_tools(messages, tool_definitions)
        except Exception as exc:
            iteration_trace.append(NodeLoopIteration(
                iteration_index=iteration_index,
                action="tool_call",
                tool_status="error",
                tool_error=f"LLM call failed: {exc}",
                duration_ms=int((time.monotonic() - iteration_start) * 1000),
            ))
            budget_exhausted = True
            break

        total_tokens += response.total_tokens

        if response.is_final_answer:
            final_content = response.content
            iteration_trace.append(NodeLoopIteration(
                iteration_index=iteration_index,
                action="final_answer",
                observation_summary=_truncate(response.content, 500),
                tokens_used=response.total_tokens,
                duration_ms=int((time.monotonic() - iteration_start) * 1000),
            ))
            break

        if not response.is_tool_call:
            # Both empty — treat as budget exhausted
            budget_exhausted = True
            iteration_trace.append(NodeLoopIteration(
                iteration_index=iteration_index,
                action="final_answer",
                observation_summary="LLM returned empty response",
                tool_status="error",
                tokens_used=response.total_tokens,
                duration_ms=int((time.monotonic() - iteration_start) * 1000),
            ))
            break

        # Execute tool calls (process first tool call only in v1 for simplicity)
        tool_call = response.tool_calls[0]
        tool_exec_start = time.monotonic()
        observation, tool_status, tool_error = _execute_tool(
            tool_call=tool_call,
            tool_executors=tool_executors,
        )
        tool_exec_duration = int((time.monotonic() - tool_exec_start) * 1000)

        iteration_trace.append(NodeLoopIteration(
            iteration_index=iteration_index,
            action="tool_call",
            tool_name=tool_call.tool_name,
            tool_args=tool_call.tool_args,
            observation_summary=_truncate(observation, 500),
            tool_status=tool_status,
            tool_error=tool_error,
            tool_execution_duration_ms=tool_exec_duration,
            tokens_used=response.total_tokens,
            duration_ms=int((time.monotonic() - iteration_start) * 1000),
        ))

        # Append assistant message (with tool call) and tool result to conversation
        # arguments must be a JSON string, not a dict (OpenAI/DeepSeek format requirement)
        serialized_args = (
            json.dumps(tool_call.tool_args, ensure_ascii=False)
            if isinstance(tool_call.tool_args, dict)
            else str(tool_call.tool_args or "{}")
        )
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tool_call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.tool_name,
                        "arguments": serialized_args,
                    },
                }
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.tool_call_id,
            "content": str(observation or ""),
        })
    else:
        # Loop exhausted max_iterations without breaking
        budget_exhausted = True

    # Build final output
    if final_content is None and config.allow_partial_on_budget and iteration_trace:
        observations = [
            it.observation_summary
            for it in iteration_trace
            if it.observation_summary and it.tool_status != "error"
        ]
        final_content = _synthesize_partial(observations)

    return NodeLoopResult(
        final_output={
            "summary": final_content or "Node loop completed without final output.",
            "content": final_content,
            "iteration_count": len(iteration_trace),
            "budget_exhausted": budget_exhausted,
        },
        total_tokens_used=total_tokens,
        iteration_count=len(iteration_trace),
        budget_exhausted=budget_exhausted,
        iteration_trace=iteration_trace,
    )


def _execute_tool(
    *,
    tool_call: ToolCall,
    tool_executors: dict[str, ToolExecutor],
) -> tuple[str, str, str | None]:
    """Execute a tool call and return (observation_text, status, error_or_none)."""
    executor = tool_executors.get(tool_call.tool_name)
    if executor is None:
        return (
            f"Error: tool '{tool_call.tool_name}' is not available.",
            "error",
            f"Unknown tool: {tool_call.tool_name}",
        )
    try:
        result = executor(tool_call.tool_name, tool_call.tool_args)
        summary = result.get("summary") or result.get("content") or str(result)[:1000]
        return summary, "success", None
    except Exception as exc:
        return (
            f"Error executing tool '{tool_call.tool_name}': {exc}",
            "error",
            str(exc),
        )


def _truncate(text: str | None, max_len: int) -> str | None:
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _synthesize_partial(observations: list[str | None]) -> str:
    """Best-effort synthesis from accumulated observations when budget is exhausted."""
    valid = [obs for obs in observations if obs]
    if not valid:
        return "Node loop exhausted budget without producing observations."
    return "Partial result from accumulated observations:\n" + "\n---\n".join(valid)
