# Richer Node Execution v1

Date: 2026-03-30

## Goal

Enable a single node/step to perform a bounded multi-action loop instead of a single LLM call or single workload action.

This does NOT change:
- Graph structure (a multi-action node is still one node externally)
- Scheduler model (no parallel semantics)
- Checkpoint / recovery contract (still step-level granularity)
- Existing single-action step types (they continue to work unchanged)

This DOES change:
- What happens inside `_execute_step` for eligible nodes
- The richness of a single node's output (from one-shot to iterative)
- The observable task complexity the system can handle

## Current State

### `_execute_step` dispatch

```
_execute_step(step)
  -> match step_type:
     python_function -> execute_python_workload()     [single call]
     web_search      -> execute_web_search_workload()  [single search]
     network_fetch   -> execute_fetch_url_workload()   [single fetch]
     filesystem      -> execute_filesystem_workload()  [single fs op]
     terminal_command-> execute_terminal_workload()    [single command]
     browser_interaction -> execute_browser_workload() [single action]
     template_runtime -> execute_template_runtime_workload() [single call]
     artifact_writer -> _execute_artifact_writer_step() [single write]
     llm_task (default) -> adapter.generate(prompt)    [single LLM call]
```

Every path produces exactly one action, one output. A "research" node that needs to search, read results, search again, then synthesize requires 4 separate graph steps today.

### Provider interface

```python
class BaseProviderAdapter:
    def generate(self, prompt: str) -> ProviderResponse  # single-turn text generation
    def chat(self, messages: list[dict]) -> ProviderResponse  # multi-turn but still single response
```

The adapter currently only supports single-turn generation. It does not support tool-use / function-calling protocols.

## Design

### Core Concept: Node Action Loop

A node eligible for multi-action execution runs an internal loop:

```
1. Build initial context (task description + available tools + prior evidence)
2. LLM decides: final_answer OR tool_call(tool_name, tool_args)
3. If final_answer: node complete, emit output
4. If tool_call: execute tool, add observation to context, go to 2
5. If budget exhausted: synthesize best-effort output from accumulated observations
```

The "tools" available to the LLM within a node are the existing workload functions, now callable iteratively.

### Execution Modes

Not all steps use the action loop. The system has three execution modes:

| Mode | When | Behavior |
|---|---|---|
| `single_action` | step_type is a specific workload (web_search, filesystem, etc.) | Unchanged. One call, one output. |
| `single_llm` | step_type is llm_task, no tool-use configured | Unchanged. One LLM call, one output. |
| `agentic_loop` | step_type is llm_task, tools configured in step definition | New. LLM + tool loop. |

The `agentic_loop` mode is opt-in per step, controlled by the step definition / capability binding.

**Agentic loop eligibility (hard rules):**

A step is eligible for agentic_loop ONLY when ALL of:
- `step_type == "llm_task"`
- `args.available_tools` is a non-empty list
- `writer_kind` is None (not an artifact writer step)
- step is not a fixed-adapter runtime step (not `python_function`, `template_runtime`, `blocked`)
- step's declared side_effect_class is `none` or `external_read`

If any condition fails, the step falls back to `single_llm` or `single_action` as appropriate.

This prevents accidentally putting artifact writers, browser submit steps, or terminal side-effect steps into loop mode.

### Node Loop Contract

```python
@dataclass
class NodeLoopConfig:
    """Configuration for a node's agentic execution loop."""
    max_iterations: int = 10          # hard cap on tool calls
    max_duration_seconds: int = 120   # wall-clock timeout for entire loop
    max_total_tokens: int = 50_000    # token budget across all LLM calls in loop
    available_tools: list[str] = ()   # tool names this node may use
    require_final_answer: bool = True # must the LLM produce a final synthesis?
    allow_partial_on_budget: bool = True # if budget exhausted, emit partial?
```

Sources for this config (priority order):
1. Step definition `args.loop_config` (from execution package binding)
2. Runtime profile overrides
3. System defaults

### Available Tools Within a Node

Each tool maps to an existing workload function:

| Tool Name | Maps To | Description | Side Effect |
|---|---|---|---|
| `web_search` | `execute_web_search_workload` | Search bounded corpus or network | read-only |
| `fetch_url` | `execute_fetch_url_workload` | Fetch a URL / local file | read-only |
| `browse` | `execute_browser_workload` | Browser interaction (open, click, extract) | read-only / weak |
| `read_file` | `execute_filesystem_workload` | Read a file | read-only |
| `run_command` | `execute_terminal_workload` | Run an allowlisted terminal command | read-only |

**Deferred to v1.1:**

| Tool Name | Reason |
|---|---|
| `write_artifact` | Introduces persistent side effects, artifact naming/lifecycle, rerun semantics |
| `browse` (submit action) | Form submission is a write side effect; v1 browse limited to read actions |

v1 tools are read-only or weak-side-effect only. This keeps the loop semantics simple: if a node fails or is rerun, no persistent side effects need to be rolled back.

Tools are **declared per-node** in the step definition. A "research" node might have `[web_search, fetch_url]`. A "form submission" node might have `[browse]`. A "data analysis" node might have `[read_file, run_command]`.

### Adapter Interface Extension

The provider adapter needs a new method for agentic execution:

```python
class BaseProviderAdapter:
    # Existing
    def generate(self, prompt: str) -> ProviderResponse
    def chat(self, messages: list[dict]) -> ProviderResponse

    # New
    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],       # tool definitions in OpenAI function-calling format
    ) -> ProviderToolResponse
```

```python
@dataclass
class ProviderToolResponse:
    content: str | None                  # text content (final answer or reasoning)
    tool_calls: list[ToolCall] | None    # tool calls requested by the LLM
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_metadata: dict[str, Any]
    finish_reason: str                   # "stop", "tool_calls", "length"

@dataclass
class ToolCall:
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any]
```

**Single-intent contract (hard rule):**

Each `ProviderToolResponse` is exactly one of:
1. `tool_calls` is non-empty → intent is "execute these tools". `content` may contain reasoning but is NOT treated as final answer.
2. `tool_calls` is empty/None AND `content` is non-empty → intent is "final answer".
3. Both empty → treated as budget-exhausted, triggers partial synthesis or failure.

Mixed mode (free text that implicitly describes actions) is explicitly forbidden. The loop executor validates this contract on every LLM response. If the response is ambiguous, the executor treats it as a final answer (safe fallback).

This constraint exists because the prompt-based fallback parser for non-function-calling providers must produce the same clean separation. Allowing ambiguous mixed responses would make the fallback parser fragile and non-deterministic.

For providers that don't support native tool-use (e.g., mock mode, basic local endpoints), the adapter implements a **prompt-based fallback**: it includes tool descriptions in the system prompt and parses structured output (JSON action blocks) from the LLM response.

### Loop Executor

New module: `app/services/run/node_loop_executor.py`

```python
@dataclass
class NodeLoopResult:
    final_output: dict[str, Any]        # the node's output payload
    provider_response: ProviderResponse | None  # last LLM response for token accounting
    extra_artifacts: list[dict[str, Any]]
    iteration_count: int
    total_tokens_used: int
    budget_exhausted: bool
    iteration_trace: list[NodeLoopIteration]  # full trace for observability

@dataclass
class NodeLoopIteration:
    iteration_index: int
    action: str                          # "tool_call" or "final_answer"
    tool_name: str | None
    tool_args: dict[str, Any] | None

    # Observation: what the LLM sees as the tool result (may be truncated/summarized)
    observation_summary: str | None

    # Execution receipt: what actually happened when the tool ran
    tool_status: str | None              # "success", "error", "timeout"
    tool_error: str | None               # error message if tool_status != "success"
    tool_execution_duration_ms: int | None

    tokens_used: int
    duration_ms: int                     # total iteration time including tool execution

def execute_node_loop(
    *,
    config: NodeLoopConfig,
    adapter: BaseProviderAdapter,
    runtime_config: ResolvedModelConfig,
    system_prompt: str,
    task_prompt: str,
    tool_definitions: list[dict],
    tool_executors: dict[str, Callable],
    run_context: dict[str, Any],
) -> NodeLoopResult:
    ...
```

### Integration with `_execute_step`

The change to `_execute_step` is minimal:

```python
# After all specific step_type branches, before the default llm_task path:

if self._step_supports_agentic_loop(step_definition):
    return self._execute_agentic_step(
        run=run,
        step=step,
        step_definition=step_definition,
        runtime_profile=runtime_profile,
        model_config_repository=model_config_repository,
        execution_mode=execution_mode,
        previous_outputs_by_step=previous_outputs_by_step,
    )

# Otherwise, existing single-call LLM path
```

`_step_supports_agentic_loop` checks:
- step definition has `args.loop_config` or `args.available_tools`
- execution contract allows agentic execution for this node

### Evidence / Output Accumulation

Each iteration's tool result is accumulated as an "observation" in the conversation context:

```
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": task_prompt},
    {"role": "assistant", "content": null, "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "search results..."},
    {"role": "assistant", "content": null, "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "page content..."},
    {"role": "assistant", "content": "Final synthesis: ..."},
]
```

The final node output includes:
- `summary`: LLM's final answer text
- `content`: full synthesized content
- `structured_output`: structured data from the final answer
- `iteration_trace`: list of all iterations (tool calls + results)
- `evidence_items`: key observations collected during the loop

### Failure / Retry Semantics

| Scenario | Behavior |
|---|---|
| Tool call fails | Log the failure as an observation, let the LLM decide: retry with different args, try a different tool, or give up |
| LLM call fails | Retry within the existing `provider_max_attempts` per-call. If all retries fail, the entire node fails. |
| Budget exhausted (iterations) | If `allow_partial_on_budget`: synthesize from accumulated observations. Otherwise: node fails. |
| Budget exhausted (time) | Same as iteration budget. |
| Budget exhausted (tokens) | Same. |
| Node fails | Existing step-level failure/recovery semantics apply (escalation, intervention, retry_step). No change. |

Key principle: **within the loop, the LLM handles local failures (bad search, empty page). The node-level failure only fires when the loop itself cannot continue.**

### Observability / Testability

**Observability:**
- `iteration_trace` stored in step `output_payload_json` under `iteration_trace` key
- Each iteration includes: tool name, args summary, result summary, tokens, duration
- Persisted as a `runtime_node_loop_trace` artifact
- Visible in frontend via step output payload

**Testability:**
- In mock mode: `chat_with_tools` returns predetermined tool calls then a final answer
- Mock tool executors return canned observations
- `NodeLoopConfig.max_iterations = 1` collapses to single-action for quick tests
- Iteration trace is fully deterministic in mock mode

**Frontend type addition:**

```typescript
export type NodeLoopIteration = {
  iteration_index: number;
  action: "tool_call" | "final_answer";
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  observation_summary?: string | null;     // what the LLM sees
  tool_status?: "success" | "error" | "timeout" | null;  // what actually happened
  tool_error?: string | null;
  tool_execution_duration_ms?: number | null;
  tokens_used: number;
  duration_ms: number;
};
```

Step output payload gains optional `iteration_trace: NodeLoopIteration[]`.

## Implementation Plan

### Phase 1: Foundation (adapter + loop executor)

1. Add `ProviderToolResponse`, `ToolCall` dataclasses to `provider_runtime.py`
2. Add `chat_with_tools` to `BaseProviderAdapter` and `OpenAICompatibleAdapter`
3. Implement prompt-based fallback for mock mode and basic endpoints
4. Create `node_loop_executor.py` with `execute_node_loop` and `NodeLoopConfig`
5. Enforce single-intent contract in loop executor (tool_calls XOR final_answer)

### Phase 2: Integration (wire into `_execute_step`, initially web_search + fetch_url only)

6. Add `_step_supports_agentic_loop` with hard eligibility rules
7. Implement `_execute_agentic_step` that bridges step definition → loop executor
8. Add tool executor wrappers for `web_search` and `fetch_url` only
9. Add `NodeLoopConfig` extraction from step definition

### Phase 3: Observability

10. Persist `iteration_trace` (with observation/receipt distinction) in step output
11. Create `runtime_node_loop_trace` artifact
12. Add `NodeLoopIteration` frontend type
13. Display iteration trace in desktop UI

### Phase 4a: Build-side declaration (manual)

14. Allow execution package step definitions to declare `available_tools` and `loop_config`
15. Manual / test injection only — no auto-proposal yet
16. Expand tool set: add `browse` (read-only actions), `read_file`, `run_command`

### Phase 4b: Build-side auto-proposal (deferred)

17. Build planning proposes agentic steps when capability binding supports it
18. Only after Phase 1-4a have been validated in real runs

## What This Enables

With richer node execution, a single "weekly research briefing" workflow can have:

| Step | Tools Available | What It Does |
|---|---|---|
| `gather_sources` | `web_search`, `fetch_url` | Search for articles → read promising ones → search again if coverage gaps → compile source list |
| `analyze_themes` | (llm only, single call) | Synthesize themes from gathered sources |
| `draft_report` | `read_file`, `write_artifact` | Read template → draft sections → review against sources → refine |
| `human_review` | (human gate) | Operator reviews draft |

Step 1 alone might execute 5-8 tool calls internally, instead of requiring 5-8 separate graph nodes. This keeps the graph simple while making each node genuinely useful.

## Boundaries

- v1 does NOT support inter-node tool calls (one node calling another node's tools)
- v1 does NOT support streaming tool results to the frontend during execution
- v1 does NOT change the graph-level checkpoint model (still one checkpoint per completed step)
- v1 does NOT add new workload types — it reuses existing ones as tools
- v1 does NOT include `write_artifact` in the tool set (no persistent side effects within loop)
- v1 `chat_with_tools` is single-intent per response: tool_calls XOR final_answer, no ambiguous mixed mode
- v1 build planner does NOT auto-propose agentic steps — manual declaration only until runtime is validated
- v1 tool-use format follows the OpenAI function-calling convention for maximum provider compatibility
