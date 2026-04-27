# Typed-Node v0 交接文档

**分支**: `feat/typed-node-v0`
**最新提交**: `5c06f06` (docs(typed-nodes): add v0 handoff document)
**测试基线**: typed_nodes 子集 **627 passed, 3 warnings**（2026-04-27 验证）
**完成度**: Phase 0–6 全部完成；spare_parts truth 对比当前仍显示业务差异，后续是业务规则校准

---

## 1. 项目背景

### 1.1 触发问题

旧的 `coding_agent` 路径让 LLM 同时承担"决定做什么"+"决定怎么做"两件事，
形成了 spec → executor 的循环论证：当 spec 不完美时无法靠模型升级修复，
只能不断强化 spec，副作用越积越多 (e.g. spare_parts 案例：CSV 文件名却写
入 JSON 字节)。

### 1.2 设计选择

参考 n8n 思路，把执行层拆成"独立可配置可测试的 typed node"：

| 层 | 职责 | LLM 参与度 |
|---|---|---|
| Planner Pass 1 | 选择 node_type、画 DAG 形状 | 是（受验证器约束） |
| Planner Pass 2 | 为每个 step 合成 typed_config | 是（每步独立 prompt，schema 严格） |
| Executor | 执行确定性逻辑 | **零** |

`coding_agent` 降级为 escape hatch，只在 catalog 没有合适 typed node 时使用。

---

## 2. 整体路线图（6 阶段）

| 阶段 | 任务 | 状态 |
|---|---|---|
| Phase 0 — 基础 | 8.0.1 (包/registry/errors)、8.0.2 (base/registry)、8.0.3 (10 个 Pydantic config) | DONE |
| Phase 1 — Catalog | 8.0.4–8.0.9 共 10 个 typed-node executor | DONE |
| Phase 2 — Planner Pass 1 | 8.0.10 (schema/prompt/validator/service)、8.0.11 (retry/shadow) | DONE |
| Phase 3 — Planner Pass 2 | 8.0.12 (scaffolding)、8.0.13 (LLM prompt builder)、8.0.14 (4-layer validator)、8.0.15 (repair + escape hatch) | DONE |
| Phase 4 — Build/Run 集成 | 8.0.16 (dispatch)、8.0.17 (typed-node workload) | DONE |
| Phase 5 — Spare_parts 端到端 | 8.0.18 (e2e run)、8.0.19 (truth comparison) | DONE（comparison 当前 DIFF） |
| Phase 6 — 迁移与开关 | 8.0.20 (feature flag) | DONE |

---

## 3. 已完成工作详情

### 3.1 Phase 0 — 基础设施

**`app/services/typed_nodes/expr_engine.py`** (~320 LOC)
- 基于 `simpleeval==0.9.13` (在 `requirements.txt` 中固定版本) 构建的安全表达式引擎
- 用 `EvalWithCompoundTypes` 而非 `SimpleEval`，支持 `x in [1,2,3]` 字面量
- AST 白名单：拒绝 `Lambda`、`Attribute`、`Subscript`、所有 `*Comp`、含 `__` 的名字
- `DOMAIN_FUNCTIONS`：max / min / abs / round / ceil / floor / today /
  today_plus_days / parse_date / coalesce / str / format / len
- `NULL_AWARE_FUNCTIONS = frozenset({"coalesce"})` 跳过 null 传播
- 三种 `null_propagation` 模式：sql / zero / raise
- 每表达式 AST 缓存（热循环用）

**`app/services/typed_nodes/base.py`**
- 抽象基类 `TypedNodeExecutor`，要求子类声明 `node_type` + `config_class`
- 强制方法：`execute(config, inputs)` 与 `output_schema(config, *upstream_schemas)`
- `validate_config()` 把 Pydantic ValidationError 包装成 `SchemaValidationFailure`

**`app/services/typed_nodes/registry.py`**
- `register_typed_node()` 在模块导入期注册 executor
- `render_catalog_summary_for_prompt()` 输出 markdown 表格（Pass 1 prompt 直接用）
- 表格末尾固定附加 `coding_agent | escape hatch — only when no typed node fits`
- 测试用 `reset_registry_for_tests()`

**`app/services/typed_nodes/errors.py`**
- 17 个错误类，分两支：`ConfigError`（加载期）+ `ExecutionError`（运行期）
- 每个错误带 `details: dict` 用于结构化审计

**`app/services/typed_nodes/configs/`** — 10 个 Pydantic config（每个一个文件）
- 全部 `extra="forbid"`，必须显式声明所有字段
- 关键交叉字段验证（`model_validator`）：
  - `ClassifyStateSpaceConfig`: 每条 `rule.then ∈ allowed_values`，`default_value ∈ allowed_values`，`allowed_values` 无重复
  - `MergeConfig`: 必须设 `on` 或同时设 `left_on`+`right_on`
  - `FormulaEvalConfig`: assignments target 唯一
  - `FormatConvertConfig`: from/to 配对必须在 `_PAIRS_SUPPORTED` 中，目标是 csv/tsv 时必须设 `csv_columns_in_order`
  - `GroupByAggregateConfig`: aggregation target 不与 group_keys 冲突

### 3.2 Phase 1 — 10 个 Typed-Node Executor

| node_type | 关键设计点 |
|---|---|
| `read_csv` | pandas + `dtype=str` 起手然后逐列强制类型（避免 "0001" 被推断成 int）；`output_schema = config.dtype_overrides.keys()`（强制声明） |
| `write_csv` | stdlib `csv.DictWriter` + `extrasaction='ignore'`，`columns_in_order` 是权威输出契约 |
| `schema_validate` | 通过性（passthrough）；最多收集 `MAX_VIOLATIONS_REPORTED = 50` 条违规 |
| `merge` | pandas-backed；空 DataFrame 用 typed empty + join keys 重建；`require_no_nulls_after` 与 `require_one_to_one` 是关键守卫 |
| `filter` | SafeExprEngine 谓词；预飞行式拒绝不安全表达式（row_index = -1 哨兵） |
| `project` | `drop_unselected=True`（默认）vs False（passthrough 模式）；`fill_missing_with` 处理缺失列 |
| `formula_eval` | 电子表格风格顺序求值；`_try_numerify_value()` 把字符串强制成数；row_inputs_subset 只包含被引用的列 |
| `classify_state_space` | 决策列表首匹配语义；evidence 字段格式 `"rule[<idx>]: <note or when>"`；SQL null 传播让规则下落 |
| `groupby_aggregate` | `pd.NamedAgg` 命名聚合；算术函数防御性地走 `pd.to_numeric(errors='coerce')` |
| `format_convert` | 确定性文件转码器（spare_parts JSON-as-CSV bug 的对策）；目标 csv/tsv 时拒绝嵌套 dict/list 单元格 |

**所有 executor 共有的契约**：
- 输入数量校验（source 节点必须 `inputs == []`，单输入节点 `len(inputs) == 1`）
- 输入类型校验（必须是 `list[dict]`）
- 错误统一通过 `app.services.typed_nodes.errors` 抛出，不让 pandas/python 原生异常逃逸

### 3.3 Phase 2 — Planner Pass 1（DAG 形状）

**核心契约**: requirement spec → typed-node DAG 形状（无 config）

**`app/services/typed_nodes/planner_pass1/`** 共 5 个模块：

- `schema.py` — `PlannerPass1Input/Output`、`TypedNodeStepDraft`、
  `TypedPlannerUnresolvedItem`、`PlannerInputArtifact`、`PlannerOutputArtifact`
  - `step_id` 必须 snake_case + 字母开头（regex 强制）
  - `coding_agent` 必须带 `escape_hatch_reason`，非 coding_agent 不能带（互斥）
  - `upstream_step_ids` 是**唯一**的拓扑声明（没有独立的 `edges` 数组）
  - `extra="forbid"` 全开

- `prompt.py` — `build_pass1_messages(input) → (system, user, revision)`
  - `PROMPT_REVISION = "typed_planner_pass1_prompt_v0_1"`（测试 pin 住）
  - 8 条硬规则在 system message 里
  - user message 渲染 catalog 表 + 输入/输出 artifact 表 + 需求文本 + 约束

- `validator.py` — 图级别不变量
  - 拒绝：未知 node_type（带 Levenshtein 提示 `did you mean`）、悬空 upstream、
    重复 step_id、循环（迭代 DFS WHITE/GRAY/BLACK）、未知 artifact 引用、
    未生产的 declared output、超 max_node_count、空图
  - 修复（不是拒绝）：entry_step_ids/exit_step_ids 漂移（机械可推导）

- `service.py` — `PlannerPass1Service.plan(input, *, retry_policy=None)`
  - mock 模式：确定性 3 步图（read_csv → project → write_csv）
  - real 模式：带防御的 JSON 提取（剥离 markdown fence、剥离前置散文）
  - 8.0.11 加入 retry：多轮对话续接（保留失败响应在 context 里 + 结构化反馈）
  - 8.0.11 加入 shadow：`plan_with_shadow(primary_mode, shadow_mode)`

- `retry.py` + `shadow.py`（8.0.11）
  - `RetryPolicy(max_attempts=3, retryable_codes={...})`
  - 提供商传输错误**不**重试（不是 planner 的责任）
  - 反馈消息按 code 排序，确定性输出
  - `compare_pass1_outputs(primary, shadow) → ShadowComparison`：步数、node_type Jaccard、edge 集合、artifact 绑定、unresolved 数

### 3.4 Phase 3 — Planner Pass 2（8.0.12–8.0.15 完成）

**核心契约**: Pass 1 DAG 形状 → 每个 step 带 `typed_config`

**`app/services/typed_nodes/planner_pass2/`** 已完成核心模块：

- `schema.py` — `PlannerPass2Input/Output`、`ConfiguredStep`、
  `TypedConfigPayload`、`SchemaFlowSnapshot`
  - `typed_config` 设计为 dict 而非 discriminated union（避免下游导入所有 Config 类）
  - 跨字段验证：`typed_config.node_type == step.node_type`
  - 输出 `steps` 一定是拓扑序

- `schema_flow.py` — `SchemaFlowTracker`
  - 委托 `executor.output_schema(config, *upstream_schemas)`
  - `register_step()` 走标准路径；`register_escape_hatch()` 给 coding_agent
  - 防御：重复注册、悬空 upstream、output_schema 返回非 set

- `mock_synth.py` — 确定性配置工厂
  - 每个 node_type 一个 factory，输出字典必须能通过对应 Config 的 Pydantic 验证
  - 困难点处理：
    - `classify_state_space`: 1 条 placeholder rule 满足 `then ∈ allowed_values`
    - `format_convert`: from==to 时强制改 to 避免 noop validator
    - `groupby_aggregate`: group_key[0] 与 agg source 不冲突
    - `merge`: 取上游交集第一列做 join key
  - `**_unused: Any` 吸收 LLM-only 关键字（保持 mock 与 LLM synthesizer 签名兼容）

- `service.py` — `PlannerPass2Service` 主循环
  - Kahn 拓扑排序（按 step_id 字典序保证确定性）
  - 每步：synthesize → Pydantic validate → static config validate → register schema flow → append
  - coding_agent 跳过 Pydantic 验证；schema_flow 用上游 union 作为宽容基线
  - 失败时返回**部分输出**（已成功的 step 保留，blocked 状态 + failed_step_id）
  - synthesizer 是构造函数注入；`LLMSynthesizer`、repair 与 mock 都走同一协议

- `llm_prompt.py` / `llm_synth.py`（8.0.13）
  - `PROMPT_REVISION_V2 = "typed_planner_pass2_prompt_v0_1"`（测试 pin）
  - 每步 prompt 注入 step draft、upstream columns、input/output artifacts、requirement text、Config JSON Schema
  - `LLMSynthesizer` 支持 mock runtime 直走 `synthesize_mock_config`，real runtime 调 provider 并解析 JSON envelope
  - provider/parse 失败时降级到 schema-valid mock config，标记 `synthesized_by="repair"`

- `config_validator.py`（8.0.14）
  - Pydantic 后补静态列引用验证、表达式引用验证、artifact path/format 合理性检查
  - 覆盖 read/write CSV、schema_validate、merge、filter、project、formula_eval、classify、groupby、format_convert
  - 输出 `ValidationIssue` 列表，service 根据 blocking issue 决定 repair/block

- `repair.py` + `PlannerPass2Service(..., repair_policy=RepairPolicy(...))`（8.0.15）
  - repair feedback 复用同一个 synthesizer protocol，针对单 step 重新生成 config
  - repair 耗尽后可降级为同 `step_id` 的 `coding_agent` escape hatch
  - `repaired_step_ids`、`RepairAttempt` 进入输出 trace，便于审计

### 3.5 Phase 4 — Build/Run 集成（8.0.16–8.0.17 完成）

- `build_execution_service.py`
  - 识别 `PlannerPass2Output` payload（`plan_version` 以 `typed_planner_pass2` 开头）
  - `build_typed_node_execution_package()` 直接物化 `steps.json`、`runtime_entry.json`、manifest/package metadata/input/output schema
  - execution strategy 固定为 `typed_node_v0`，execution mode 固定为 `typed-node-v0`

- `executable_manifest/resolver.py` / `validator.py`
  - validator 接受 `typed_node_v0`
  - resolver 对 typed-node manifest 直接生成 `execution_kind="typed_node"` 的 resolved steps
  - typed node step definition 保留 `node_type`、`typed_config`、artifact binding 与 upstream id

- `case_workloads.py` / `run_execution_service.py`
  - `execute_typed_node_workload()` 根据 registry 找 executor，聚合 upstream `structured_output.result` 后零 LLM 执行
  - `_execute_step()` 支持 `step_type="typed_node"`，provider response 为 `None`
  - `write_csv` sink 会返回 `primary_output_path`

### 3.6 Phase 5 — Spare_parts E2E（8.0.18–8.0.19 完成，truth 仍有业务差异）

- `scenario_data/spare_parts/run_typed_node_e2e.py`
  - 构造 15-step `PlannerPass2Output`
  - 走 build materialization → manifest resolve → typed-node runtime workload
  - 默认输出 `scenario_data/spare_parts/output/replenishment_queue.csv`

- `scenario_data/spare_parts/compare_truth.py`
  - 比较 expected vs actual CSV：列对齐、按 `sku_id` 行对齐、空白规整、数值容差、可读 diff
  - CLI 成功时 exit 0；存在差异时 exit 1 并打印摘要
  - 当前比较结果：15/0/0 列 shared/missing/extra；8/2/1 行 compared/missing/extra；45 个 cell diff
  - 当前缺失 truth rows: `P-005`, `P-019`；当前多出 actual row: `P-006`

- 测试：
  - `apps/api/tests/typed_nodes/test_spare_parts_e2e.py`
  - `apps/api/tests/typed_nodes/test_spare_parts_truth_comparison.py`

### 3.7 Phase 6 — Feature Flag（8.0.20 完成）

- `app/core/config.py`
  - 新增 `typed_node_v0_enabled: bool = Field(default=True, alias="TYPED_NODE_V0_ENABLED")`
  - 默认开启，避免 8.0.16–8.0.19 的 typed-node 路径意外回退

- Build 入口
  - `build_execution_service._should_dispatch_typed_node_build()` 同时检查 flag 和 Pass2 payload shape
  - 关闭 flag 后，session-anchored build 不再接管 typed-node plan，回到既有 planner output build 路径

- Run 入口
  - `run_execution_service._execute_step()` 在 `step_type="typed_node"` 分支先检查 flag
  - 关闭 flag 后直接抛出明确错误：`typed-node-v0 execution is disabled by TYPED_NODE_V0_ENABLED`

- 测试：
  - `test_build_dispatch.py::test_typed_node_build_dispatch_respects_feature_flag`
  - `test_runtime_workload.py::test_run_execution_service_rejects_typed_node_when_feature_flag_disabled`

---

## 4. 关键契约（后续开发必须遵守）

### 4.1 TypedNodeExecutor 注册协议

```python
class MyExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "my_node"
    config_class: ClassVar[type] = MyNodeConfig

    def execute(self, config, inputs): ...
    @staticmethod
    def output_schema(config, *upstream_schemas) -> set[str]: ...

register_typed_node(MyExecutor, summary="...")
```

新增 typed node 时 7 件事不能漏：
1. config 文件 + Pydantic 验证 + 在 `configs/__init__.py` 导出
2. executor 文件 + 在 `executors/__init__.py` 显式 import 触发注册
3. `output_schema()` 必须**静态可计算**（仅依赖 config + upstream_schemas，不能查数据）
4. `execute()` 异常**必须**包成 `TypedNodeError` 子类
5. 加 unit test 覆盖正常路径 + 至少 3 个错误路径
6. mock_synth.py 添加该 node_type 的 factory（产物必须通过 Pydantic 验证）
7. registry catalog summary 自动包含新节点（无需改 registry.py）

### 4.2 Synthesizer 协议（Pass 2 关键）

```python
def synthesize(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
    upstream_columns: list[set[str]],
    requirement_text: str,
    repair_feedback: str | None = None,
    attempt_idx: int | None = None,
) -> tuple[
    dict[str, Any],   # config dict（必须能过 Pydantic）
    list[str],        # synthesis_notes
    Literal["mock", "llm", "repair"],  # synthesized_by
]
```

`LLMSynthesizer`、mock synthesizer 和 repair retry 都实现这个协议，注入
`PlannerPass2Service(synthesizer=..., repair_policy=...)`。

### 4.3 Validator 拒绝 vs 修复二分法

- **拒绝**: 改变 LLM 意图（cycle、unknown node_type、artifact 引用错）→ 重试或人工
- **修复**: 机械可推导（entry/exit 集合漂移）→ 自动改正 + warning

8.0.14 的 4-layer validator 沿用同一原则。

### 4.4 测试隔离

`test_base_registry_errors.py` 已经通过 snapshot-restore fixture 解决了
全局 registry 在测试间的污染问题。新加测试时如果要操作 registry，
**复用现有 fixture 模式**而不是再造一个。

---

## 5. 后续工作（详细）

### 5.1 Phase 5 后续校准

8.0.18/8.0.19 的工程链路已经打通，但 `compare_truth.py` 明确显示当前
spare_parts 业务输出与 truth fixture 不一致。后续若要让 truth comparison
成为硬门，需要单独校准业务公式/分类规则，而不是再改 build/run runtime。

当前已知差异（2026-04-27）：
- 列：shared/missing/extra = 15/0/0
- 行：compared/missing/extra = 8/2/1
- 缺失 truth rows: `P-005`, `P-019`
- 多出 actual row: `P-006`
- cell diffs: 45

---

## 6. 工作流程与命令

### 6.1 开发环境

```bash
PYTHONPATH=apps/api UV_CACHE_DIR=/tmp/uv-cache /Users/llk/.local/bin/uv run --with-requirements apps/api/requirements.txt pytest apps/api/tests/typed_nodes -q
PYTHONPATH=apps/api UV_CACHE_DIR=/tmp/uv-cache /Users/llk/.local/bin/uv run --with-requirements apps/api/requirements.txt python scenario_data/spare_parts/run_typed_node_e2e.py
PYTHONPATH=apps/api UV_CACHE_DIR=/tmp/uv-cache /Users/llk/.local/bin/uv run --with-requirements apps/api/requirements.txt python scenario_data/spare_parts/compare_truth.py
```

注意：命令从仓库根目录 `/Users/llk/agentsFactory` 运行；`scenario_data/*/output/`
是生成产物，不要加入 git。

### 6.2 Git 工作流

- 分支: `feat/typed-node-v0`，已 push 到 origin
- 已知忽略事项（已经在 .gitignore 中）:
  - `scenario_data/*/output/` 整个目录（生成产物）
  - `scenario_data/*/coding_agent_step_*.out`、`script.py`、`script.sh`、`run_richer_e2e.py`
  - `scenario_data/*_run_trace/`

### 6.3 Smoke Test 命令

每完成一段重要功能时跑一次：
```bash
PYTHONPATH=. .venv/bin/python -c "
import app.services.typed_nodes.executors  # register
from app.services.typed_nodes.registry import render_catalog_summary_for_prompt
print(render_catalog_summary_for_prompt())
"
```

---

## 7. 测试基线（不可回退）

- typed_nodes 子集: **627 passed, 3 warnings**（2026-04-27）
- Phase 5 窄测试: **3 passed, 3 warnings**
- 预先存在的失败（不是本工作引入的）:
  - `tests/test_worker_task_failure_serialization.py` collection error（worker_app 模块缺失）
  - 旧 baseline 的 2 个 pre-existing failures（session message analysis + materialization runtime hang）

新增功能时**不能让上述基线下降**。每个 commit 至少跑 typed_nodes 子集；涉及
spare_parts 时同时跑 E2E runner 与 `compare_truth.py`。

---

## 8. 上手建议（给下一位）

### 8.1 应该先读什么

按顺序：
1. 本文档（顶层概览）
2. `apps/api/app/services/typed_nodes/__init__.py`（包总览）
3. `apps/api/app/services/typed_nodes/base.py`（执行器协议）
4. `apps/api/app/services/typed_nodes/registry.py`（注册机制）
5. **任意一个 executor 完整看一遍**（推荐 `executors/merge.py`，覆盖最多 corner case）
6. `apps/api/app/services/typed_nodes/planner_pass1/schema.py`（Pass 1 契约）
7. `apps/api/app/services/typed_nodes/planner_pass2/service.py`（Pass 2 主循环）

### 8.2 不应该做什么

- **不要**改动 Pass 1 的 `upstream_step_ids` 单一拓扑源契约。改成双源（edges + upstream）会立刻引入 LLM 漂移类问题。
- **不要**在 executor 里调用 LLM。executor 路径必须保持零 LLM。
- **不要**让 `output_schema` 依赖运行时数据。它必须只依赖 config + upstream_schemas。
- **不要**用 git rebase --interactive 修改已 push 的 commit。

### 8.3 用户偏好

用户特点（之前对话中表达过的）：
- "直接开干才能遇到问题" —— 喜欢快速迭代而非完整设计
- "不要无限细化了" —— 拒绝过度抽象
- 喜欢小步提交、每步可验证、每步带测试
- 看重"端到端能跑通"超过"理论完美"
- Spare_parts 是验证场景的金标准

### 8.4 可能的隐藏陷阱

1. **simpleeval 版本固定** (`requirements.txt` `simpleeval==0.9.13`)。升级会破坏
   AST 白名单（不同版本的 AST 节点集合略有差异）。改之前先确认所有
   `expr_engine` 测试还能过。

2. **pandas dtype 默认推断会偷偷改字符串**: read_csv 必须先 `dtype=str` 然后逐列强制类型。
   修改 `read_csv.py` 时容易破坏这个保证。

3. **Pydantic v2 行为**: 项目用 pydantic v2。某些 `model_validator(mode="after")` 在
   v1 里写法不同；如果引用旧示例代码可能踩坑。

4. **测试全局 registry 状态**: `test_base_registry_errors.py` 的 fixture 是
   snapshot-restore 模式。如果新加测试要操作 registry，复用这个模式而不是
   写 `registry._REGISTRY.clear()` —— 后者会破坏其他测试文件。

5. **scenario_data 的 input/expected 已 track，output 已 ignore**。不要把
   生成的 output 加入 git。

---

## 9. 联络与状态

- 分支 PR URL（待创建）: https://github.com/Trynewthings/ForgeAI/pull/new/feat/typed-node-v0
- 当前状态: 8.0.20 已落地；typed-node v0 路线图工程阶段完成
- 下一项: spare_parts 业务规则/fixture truth 校准
- 业务校准: `compare_truth.py` 当前仍 DIFF，说明 spare_parts 规则与 truth fixture 尚未对齐

后续 commits 建议保持每个 8.0.X 任务一个 commit 的节奏，commit message
首行使用 `feat(typed-nodes): 8.0.X — <短标题>`。
