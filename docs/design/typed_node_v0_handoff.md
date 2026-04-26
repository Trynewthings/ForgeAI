# Typed-Node v0 交接文档

**分支**: `feat/typed-node-v0`
**最新提交**: `caa1a82` (2026-04-26)
**测试基线**: 1233 passed + 4 skipped (`tests/test_worker_task_failure_serialization.py` 因预先存在的 `worker_app` 导入错误暂时 ignore)
**完成度**: Phase 0 全部 + Phase 1 全部 + Phase 2 全部 + Phase 3 (1/4)

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
| Phase 3 — Planner Pass 2 | 8.0.12 (scaffolding) | 1/4 |
|  | 8.0.13 (LLM prompt builder) | TODO |
|  | 8.0.14 (4-layer validator) | TODO |
|  | 8.0.15 (repair + escape hatch) | TODO |
| Phase 4 — Build/Run 集成 | 8.0.16 (dispatch)、8.0.17 (typed-node workload) | TODO |
| Phase 5 — Spare_parts 端到端 | 8.0.18 (e2e run)、8.0.19 (truth comparison) | TODO |
| Phase 6 — 迁移与开关 | 8.0.20 (feature flag) | TODO |

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

### 3.4 Phase 3 — Planner Pass 2（已完成 1/4：8.0.12）

**核心契约**: Pass 1 DAG 形状 → 每个 step 带 `typed_config`

**`app/services/typed_nodes/planner_pass2/`** 已完成 5 个模块：

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
  - 每步：synthesize → Pydantic validate → register schema flow → append
  - coding_agent 跳过 Pydantic 验证；schema_flow 用上游 union 作为宽容基线
  - 失败时返回**部分输出**（已成功的 step 保留，blocked 状态 + failed_step_id）
  - **synthesizer 是构造函数注入** —— 8.0.13 LLM 版本通过同一个协议替换

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
) -> tuple[
    dict[str, Any],   # config dict（必须能过 Pydantic）
    list[str],        # synthesis_notes
    Literal["mock", "llm", "repair"],  # synthesized_by
]
```

8.0.13 写 LLM 版本时直接实现这个签名，注入 `PlannerPass2Service(synthesizer=...)`。

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

### 5.1 8.0.13 — Pass 2 LLM Prompt Builder（下一步）

**目标**: 实现 `LLMSynthesizer`，符合 4.2 节 synthesizer 协议。

**建议结构**:
```
app/services/typed_nodes/planner_pass2/
├── llm_prompt.py    # 新文件：per-step prompt 构建
├── llm_synth.py     # 新文件：LLM-driven synthesizer 实现
└── service.py       # 已存在：传入 llm_synth.synthesize 即可
```

**llm_prompt.py 应包含**:
- 每个 node_type 一个 prompt template（或一个通用模板 + 注入 Config 的 JSON Schema）
- 上下文注入：step.intent、upstream_columns、相关 artifacts、requirement_text 中的相关业务规则
- `PROMPT_REVISION_V2` 常量（测试 pin）
- system message：解释这是 Pass 2、强调 schema 严格、要求 JSON-only 响应

**llm_synth.py 应包含**:
- `LLMSynthesizer(adapter_factory)` 类
- `__call__` 实现 synthesizer 协议
- mock vs real 分支（mock 直接调用 `synthesize_mock_config`，real 调 LLM）
- JSON 解析 + 错误处理（参考 Pass 1 service.py 的 `_extract_json_payload`）
- 失败时返回什么？建议：返回 mock fallback + 标记 `synthesized_by="repair"` + 加 synthesis_notes

**测试要点**:
- 每个 node_type 至少 1 个 prompt 渲染测试
- LLM 响应解析失败时的 fallback
- 上下文注入正确性（upstream_columns 应该体现在 prompt 里）

**预估工作量**: 1.5–2 天

### 5.2 8.0.14 — Pass 2 4-Layer Validator

**4 层验证**（按顺序，前一层失败则后面跳过）:

1. **Pydantic 验证** —— 已在 8.0.12 service.py 里通过 `executor.validate_config()` 实现
2. **静态列引用验证** —— config 引用的列必须在 upstream_columns 里
   - 例: `formula_eval.assignments[i].expression` 引用的列名
   - 例: `merge.on` 必须在两个 upstream 都存在
   - 例: `project.columns[i].source` 必须在 upstream 里
3. **跨字段一致性** —— 已在 Pydantic model_validator 里实现，无需重复
4. **业务级合理性**（轻验证）—— e.g. `read_csv.path` 后缀与 artifact.format 一致

**建议结构**:
```
app/services/typed_nodes/planner_pass2/config_validator.py
```

接口设计:
```python
def validate_step_config(
    step: TypedNodeStepDraft,
    config: dict,
    *,
    upstream_columns: list[set[str]],
    artifacts: ...,
) -> list[ValidationIssue]:
```

**预估工作量**: 1.5 天

### 5.3 8.0.15 — Repair + Escape Hatch

**Repair 流程**: 当某层验证器失败时:
1. 收集失败的 issue list
2. 构造 repair prompt（沿用 Pass 1 retry 的多轮思路）
3. 用同一个 LLM 重新合成那一步的 config
4. 最多重试 N 次后降级为 escape hatch（把这一步改成 coding_agent）

**Escape Hatch 降级**: 
- 把 `step.node_type` 改成 `coding_agent`
- `escape_hatch_reason = f"Pass 2 repair exhausted after {N} attempts: {last_issues}"`
- 调用方决定是阻断（require human review）还是继续

**关键决策**:
- repair 是在 service 主循环里做还是在 synthesizer 里做？建议**在主循环里**（保持 synthesizer 单纯）
- escape hatch 一旦发生，schema_flow 用什么？建议保留升级前对该 node_type 的 `output_schema` 推断结果

**预估工作量**: 1 天

### 5.4 Phase 4 — Build/Run 集成（8.0.16 + 8.0.17）

**8.0.16 Build dispatch**:
- 现有 `app/services/build/build_execution_service.py` 需要识别 typed-node 计划
- 当 `executable_manifest` 含 typed-node 标记时，跳过现有的 capability binding 路径
- 直接把 `PlannerPass2Output.steps` 物化成 build 对象
- 关键文件: `apps/api/app/services/build/build_execution_service.py`、
  `apps/api/app/services/executable_manifest/resolver.py`

**8.0.17 Typed-node workload**:
- 现有 `apps/api/app/services/run/case_workloads.py` 是 capability-driven
- 新增 `typed_node_workload`，输入 `(step, inputs) → output` 经过 `executor.execute()`
- 调度器 (`scheduler.py`) 处理拓扑顺序的部分已经存在（DAG runtime v1）
- 关键改动点：`run_execution_service.py` 的 step 派发逻辑

**预估工作量**: 3 天（涉及现有系统的修改，需要小心）

### 5.5 Phase 5 — Spare_parts E2E（8.0.18 + 8.0.19）

**8.0.18**: 在 `apps/api/scenario_data/spare_parts/` 跑端到端
- 输入: `input/demand.csv`、`input/inventory_rt.csv`、`input/safety_stock.csv`
- 期待输出: `output/replenishment_queue.csv`（与 `expected/` 对比）

**8.0.19**: Truth comparison
- 现有 expected fixtures 是参考真值
- 写一个比较脚本：列对齐 + 行对齐（按 sku_id）+ 容差 + 空格/类型规整化
- 失败时输出 diff 表格

**预估工作量**: 2 天

### 5.6 Phase 6 — Feature Flag (8.0.20)

- 在 `app/core/config.py` 加 `typed_node_v0_enabled: bool` 设置
- 在 build/run 入口检查该 flag，关闭时走旧路径
- 加端到端测试覆盖两个分支

**预估工作量**: 0.5 天

---

## 6. 工作流程与命令

### 6.1 开发环境

```bash
cd apps/api
PYTHONPATH=. .venv/bin/pytest tests/typed_nodes/ -q   # 仅运行 typed_nodes 测试
PYTHONPATH=. .venv/bin/pytest -q --ignore=tests/test_worker_task_failure_serialization.py
```

注意：仓库根目录是 `/Users/llk/agentsFactory`，但所有命令应在 `apps/api/` 下运行。

### 6.2 Git 工作流

- 分支: `feat/typed-node-v0`，已 push 到 origin
- 每个 8.0.X 任务一个 commit，commit message 末尾固定带
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
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

- 总用例: **1233 passed + 4 skipped**
- typed_nodes 子集: **579 passed**（407 executor/config/expr + 106 planner_pass1 + 66 planner_pass2）
- 预先存在的失败（不是本工作引入的）:
  - `tests/test_worker_task_failure_serialization.py` collection error（worker_app 模块缺失）
  - 旧 baseline 的 2 个 pre-existing failures（session message analysis + materialization runtime hang）

新增功能时**不能让上述基线下降**。每个 commit 跑一次完整 suite。

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
- 当前状态: 8.0.12 已 merge 到分支并 push origin
- 下一项: 8.0.13 LLM Prompt Builder

后续 commits 建议保持每个 8.0.X 任务一个 commit 的节奏，commit message
首行使用 `feat(typed-nodes): 8.0.X — <短标题>`。
