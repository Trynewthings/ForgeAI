# Fan-out / Fan-in DAG Runtime v1

Date: 2026-03-30

## Goal

Extend the runtime scheduler to execute non-linear DAG workflows, starting with the minimum viable fan-out / fan-in subset.

This document answers six design questions before implementation begins.

## Current State

The runtime is built on a strict single-active-step assumption:

- `SchedulerCurrentStepTracker.current_step` is one `RunStep`
- `pending_step` is a singular `str | None` in checkpoint, operation state, recovery summary, and frontend types
- `_run_steps_in_order()` is a sequential for-loop
- `_build_linear_read_model()` explicitly rejects graphs with outdegree > 1 or indegree > 1
- Recovery logic (`_prepare_steps_for_requested_action`) assumes linear ordering — everything after target step gets reset

All of these must change for DAG execution.

## v1 Scope

### Supported

- DAG with fan-out (one node → multiple successors)
- DAG with fan-in (multiple predecessors → one node)
- Diamond pattern: A → {B, C} → D
- Mixed: linear segments interleaved with fan-out / fan-in
- Strict all-upstream-success join policy (v1 only)

### Explicitly deferred

- Conditional branches (edge predicates)
- Partial join (fan-in with degraded inputs)
- Loop semantics
- Dynamic graph mutation at runtime
- Cross-branch data passing (v1 branches are independent)

---

## 1. Ready-Set Model

### Definition

At any point during execution, the **ready set** is the set of nodes whose predecessors have ALL completed successfully.

```
ready_set(graph, completed_set) = {
    node for node in graph.nodes
    if node not in completed_set
    and node not in failed_set
    and all(pred in completed_set for pred in predecessors(node))
}
```

### WorkflowGraphScheduler changes

Current:
- Input: `ordered_step_names: list[str]` (linear)
- Output: calls `_run_steps_in_order()` sequentially

v1 DAG:
- Input: `DAGReadModel` containing adjacency, predecessors, node_instances
- Output: iterative dispatch of ready sets until exit node is reached or failure halts

### DAGReadModel

```python
@dataclass
class DAGReadModel:
    read_model_kind: str = "dag_workflow_graph_v1"
    node_ids: list[str]
    step_name_by_node: dict[str, str]
    predecessors: dict[str, set[str]]    # node_id -> set of predecessor node_ids
    successors: dict[str, set[str]]      # node_id -> set of successor node_ids
    entry_node_ids: set[str]
    exit_node_ids: set[str]
    materialization_statuses: dict[str, str]
```

### Construction

`_build_dag_read_model()` replaces `_build_linear_read_model()` as the general case:
- Validates the graph is a DAG (topological sort must succeed)
- All nodes must have step_name mappings
- Entry nodes have no predecessors, exit nodes have no successors
- Linear graphs are a valid subset (fan-out/fan-in count = 0)

If the graph fails DAG validation, fall back to `CompatibilityStepScheduler`.

---

## 2. Parallel Execution Model

### v1: ThreadPoolExecutor

For v1, use `concurrent.futures.ThreadPoolExecutor` within the existing synchronous scheduler:

```python
with ThreadPoolExecutor(max_workers=max_parallel) as pool:
    while ready_set:
        futures = {}
        for node_id in ready_set:
            step = steps_by_node[node_id]
            futures[pool.submit(_execute_single_step, step)] = node_id
        for future in as_completed(futures):
            node_id = futures[future]
            result = future.result()
            # update completed_set, compute new ready_set
```

### Why not async or Celery sub-jobs

- **Async**: The entire execution service is synchronous (SQLAlchemy session, provider calls). Converting to async is a large migration for minimal v1 benefit.
- **Celery sub-jobs**: Adds distributed coordination complexity (job result polling, sub-job failure propagation, transaction boundaries). Valuable for v2 when branches are heavy, but overkill for v1 where branches are bounded by max_parallel.

### v1 concurrency boundary

- `max_parallel`: configured per run, default 3, max 8
- Source: `WorkflowExecutionContract.parallel_execution_policy.max_concurrent_nodes`
- If not set: default to 3

### DB session handling

Each parallel step execution must use its own DB session (SQLAlchemy sessions are not thread-safe):

```python
def _execute_single_step(step, ...):
    with self._create_scoped_session() as session:
        # execute step using scoped session
        # commit step result
```

The main thread holds the run-level session for:
- Updating run status
- Writing checkpoints
- Coordinating completion state

### Thread responsibility boundary (hard rule)

Parallelism is strictly limited to node execution:

- **Worker threads**: execute `_execute_step()` only, return output payload
- **Coordinator thread** (main): computes ready set, dispatches workers, collects results, updates run-level state, writes checkpoints, commits status transitions

Worker threads must NOT:
- Mutate `Run` model fields
- Write checkpoints
- Update `operation_state` on the `Run`
- Call `_apply_runtime_operation()`

All run-level state mutation is serialized through the coordinator thread.

---

## 3. Join / Barrier Semantics

### v1: Strict all-success join

A fan-in node becomes ready ONLY when ALL its predecessors are in `completed_set` with status `completed`:

```python
def is_ready(node_id, completed_set, failed_set):
    preds = predecessors[node_id]
    if any(p in failed_set for p in preds):
        return False  # blocked — upstream failure
    return all(p in completed_set for p in preds)
```

### Blocked propagation

If a predecessor fails:
- The fan-in node is marked `blocked`
- All descendants of the blocked node are transitively blocked
- Blocked nodes are never added to `ready_set`
- They appear in checkpoint as `blocked_node_ids`

### Blocked reason class (hard rule)

`blocked` must always carry a `blocked_reason_class`:

- `blocked_by_failed_dependency` — upstream node failed, this node cannot proceed
- `blocked_by_human_gate` — waiting for human approval before execution

These are semantically different and must be distinguished even if the UI renders both as "blocked":
- `blocked_by_failed_dependency` is resolved by `retry_step` on the failed upstream
- `blocked_by_human_gate` is resolved by `confirm_run` approval
- `resume_run` does NOT automatically unblock `blocked_by_failed_dependency` nodes

### Why not partial join in v1

Partial join (allow fan-in with degraded inputs) requires:
- A join policy per node (which inputs are required vs optional)
- Degraded output synthesis
- More complex checkpoint/recovery state

This is valuable but adds too much surface area for v1.

---

## 4. Failure Propagation

### Single-branch failure policy

When a branch node fails:

1. **That node**: marked `failed`, enters `waiting_for_intervention` if escalation enabled
2. **Other running branches**: continue to completion (do NOT abort)
3. **Fan-in nodes downstream of failed branch**: marked `blocked`
4. **Nodes only reachable through blocked fan-in**: transitively `blocked`
5. **Independent branches**: unaffected, continue normally

### Run-level state

- If ALL branches succeed → fan-in proceeds → run continues
- If ANY branch fails → fan-in blocked → run enters `partially_completed` or `waiting_for_intervention`
- Only branches that share no fan-in with the failed branch continue

### Authority

Failure propagation is computed by the scheduler from the DAG read model:

```python
def compute_blocked_set(failed_set, successors, predecessors):
    blocked = set()
    queue = list(failed_set)
    while queue:
        node_id = queue.pop()
        for succ in successors[node_id]:
            if succ not in blocked:
                blocked.add(succ)
                queue.append(succ)
    return blocked
```

This is a runtime decision, not a build-time one.

### Human gate on failure

If `execution_contract.failure_escalation_policy.escalate_to_human` is true (existing field):
- Failed branch → `waiting_for_intervention`
- User chooses: retry_step, skip_branch (v2), or abort_run

If not:
- Failed branch → `failed_terminal`
- Downstream blocked → `blocked`
- Run stops at the fan-in barrier

---

## 5. Checkpoint / Recovery Model

### Checkpoint: from single step to node sets

Current checkpoint:
```json
{
  "pending_step": "step_name",
  "last_successful_step": "step_name",
  "completed_step_names": ["a", "b"]
}
```

DAG checkpoint:
```json
{
  "pending_steps": ["c", "d"],
  "ready_steps": ["c", "d"],
  "running_steps": [],
  "completed_step_names": ["a", "b"],
  "blocked_step_names": [],
  "failed_step_names": [],
  "last_completed_step": "b",
  "execution_round": 3,
  "graph_execution_kind": "dag_workflow_graph_v1"
}

`execution_round` is the **scheduler dispatch wave ID**, not an attempt count:
- Round 1: dispatch entry nodes (e.g. A)
- Round 2: dispatch nodes whose predecessors completed in round 1 (e.g. B, C)
- Round 3: dispatch fan-in node whose all predecessors completed (e.g. D)

Each round represents one cycle of: compute ready set → dispatch → await completion → checkpoint.
```

### Backwards compatibility

Keep `pending_step` (singular) as a **derived field** for linear cases:
- If `pending_steps` has exactly one element → `pending_step = pending_steps[0]`
- If multiple → `pending_step = null` (frontend should read `pending_steps`)
- This preserves linear compatibility path

### routing_metadata extension

```json
{
  "routing_kind": "dag_workflow_graph_v1",
  "graph_authority_active": true,
  "graph_read_model_kind": "dag_workflow_graph_v1",
  "pending_step_determined_by": "dag_ready_set",
  "total_completed": 4,
  "total_graph_steps": 7,
  "remaining_graph_steps": 3,
  "parallel_state": {
    "active_branches": 2,
    "max_parallel": 3,
    "fan_in_waiting": ["node_D"],
    "fan_out_origin": "node_A"
  }
}
```

### retry_step in a DAG

Semantics:
1. Identify the failed step's **dependency cone**: the set of nodes reachable from the failed node via `successors`
2. Reset the failed step to `pending`
3. Reset all nodes in the dependency cone that were `blocked_by_failed_dependency` back to `pending`
4. Do NOT touch nodes outside the dependency cone (other branches, completed ancestors)
5. Recompute `ready_set` — the retried step should appear in it
6. Resume execution

This is **cone-scoped**, not branch-scoped or graph-scoped. It only affects the failed node and its downstream dependents.

### resume_run in a DAG

Semantics:
1. Read checkpoint to find `ready_steps` and `running_steps` (treat running as pending on resume)
2. Recompute ready set from `completed_step_names` + graph
3. Begin executing ready steps
4. **`blocked_by_failed_dependency` nodes stay blocked** — resume does NOT auto-unblock them
5. **`blocked_by_human_gate` nodes** may be unblocked if the human gate was approved
6. **`waiting_for_intervention` nodes** are reactivated (they were waiting for human action, which resume represents)

Hard rule: `resume_run` never converts a `blocked_by_failed_dependency` into a ready node. Only `retry_step` on the failed upstream can do that.

### restart_run

Reset all steps, clear completed/blocked/failed sets, start from entry nodes.

---

## 6. UI / Observability Minimum

### Frontend data requirements

The frontend needs to see, at minimum:

1. **Per-step state**: `running`, `completed`, `failed`, `blocked`, `pending`
   - Already exists in `RuntimeStepOperationStateRecord`
   - Blocked is new — needs to be an explicit operation state

2. **Active branches**: which steps are currently executing in parallel
   - Source: `running_steps` in checkpoint
   - Display: multiple step cards highlighted as "running" simultaneously

3. **Join waiting**: which fan-in nodes are waiting for upstream completion
   - Source: a fan-in node with some predecessors completed, some running/pending
   - Display: "waiting for N/M upstream branches"

4. **Branch failure**: which steps failed and what's blocked downstream
   - Source: `failed_step_names` + `blocked_step_names` in checkpoint
   - Display: failed step in red, blocked descendants in grey with "blocked by [step]" annotation

5. **Branch-level authority source**: each branch's execution order comes from the graph
   - Source: `routing_metadata.graph_authority_active` + step-level `scheduler_authority_source`

### Schema changes for frontend

Add to `RuntimeCheckpointRecord`:
```typescript
pending_steps?: string[];
ready_steps?: string[];
running_steps?: string[];
blocked_step_names?: string[];
failed_step_names?: string[];
execution_round?: number;
graph_execution_kind?: string;
```

Add `"blocked"` to `RuntimeOperationState` union type.

Add to `RuntimeRecoverySummaryRecord`:
```typescript
blocked_step_names?: string[];
retryable_step_names?: string[];
```

### Minimal visual changes

v1 does not require a graph canvas. The existing step-card list is sufficient:
- Steps render in topological order
- Running steps show green pulse (already exists)
- Blocked steps show grey with explanation
- Fan-in steps show "waiting for upstream" when predecessors are not all done
- Multiple steps can be "running" simultaneously

---

## Implementation Order

### Phase 1: Schema + DAG Read Model (no behavior change)

1. Add `_build_dag_read_model()` to `WorkflowGraphScheduler`
2. Add `DAGReadModel` dataclass
3. Extend `describe_support()` to report DAG capability
4. Add `"blocked"` to operation state enum
5. Extend checkpoint/routing_metadata schemas with optional DAG fields

### Phase 2: DAG Execution Core

1. Add `_run_steps_dag()` method (parallel-capable scheduler loop)
2. Implement ready-set computation from DAG + completed set
3. Implement `ThreadPoolExecutor` dispatch with scoped sessions
4. Implement strict all-success join barrier
5. Wire `WorkflowGraphScheduler.run()` to choose linear vs DAG path

### Phase 3: Failure + Checkpoint

1. Implement blocked-set computation from failed nodes
2. Extend `_build_checkpoint()` for DAG state (pending_steps, blocked, running)
3. Failure propagation policy (continue other branches)
4. `_apply_runtime_operation()` for DAG operation state

### Phase 4: Recovery

1. `_prepare_steps_for_requested_action()` for branch-scoped retry
2. `resume_run` reads DAG checkpoint and recomputes ready set
3. `restart_run` resets all node states

### Phase 5: UI

1. Frontend types extended with DAG checkpoint fields
2. Step cards show "blocked" state
3. Multiple simultaneous "running" steps displayed
4. Fan-in "waiting for upstream" indicator

---

## Migration Strategy

The existing `WorkflowGraphScheduler` will gain DAG capability as a superset:

- Linear graphs produce a DAG read model with max parallelism = 1
- All linear behavior is preserved
- `_run_steps_in_order()` remains as the linear fast path
- `_run_steps_dag()` is the new parallel path
- Scheduler selection: if DAG read model has fan-out/fan-in → DAG path, else → linear path

The `CompatibilityStepScheduler` remains as fallback for runs without workflow_graph.

No existing run data is broken. DAG fields in checkpoint/routing_metadata are optional and additive.
