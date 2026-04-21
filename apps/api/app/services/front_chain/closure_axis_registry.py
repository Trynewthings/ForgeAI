"""Closure axis registry (Phase 1C).

Single source of truth for all standard closure axes. This module defines:
    * axis metadata (id, layer, severity, priority)
    * human-readable question templates
    * structured option sets with deterministic resolves mappings
    * the closed set of axis ids the interpreter is allowed to produce

Rules (Phase 1C R-list):
    R1: LLM must not invent axes outside this registry.
    R2: closure questions always carry suggested_options from here.
    R3: option selection → deterministic resolve → no LLM.
    R4: clarification_interpreter_llm is a fallback for free-text only.
    R5: specification-level concerns do NOT enter blocking/review axes.

To add a new closure axis: add an entry to ``AXIS_REGISTRY`` below. The
interpreter prompt, policy engine, and update engine all read from this
registry at runtime — no other file needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

FrontChainLayerName = Literal["problem_formulation", "input_closure", "output_closure"]


@dataclass(frozen=True)
class ClarificationOptionDef:
    """A single option the user can pick for a given axis."""

    option_id: str
    label: str
    value: str
    resolves: dict[str, str]
    is_default: bool = False
    requires_free_text_suffix: bool = False


@dataclass(frozen=True)
class ClosureAxisDef:
    """Metadata for one standard closure axis."""

    axis_id: str
    layer: FrontChainLayerName
    severity: Literal["blocking", "review"] = "blocking"
    priority: int = 0
    question_text: str = ""
    context_hint: str = ""
    options: tuple[ClarificationOptionDef, ...] = ()

    @property
    def option_ids(self) -> set[str]:
        return {opt.option_id for opt in self.options}

    def get_option(self, option_id: str) -> Optional[ClarificationOptionDef]:
        return next((o for o in self.options if o.option_id == option_id), None)


# ---------------------------------------------------------------------------
# The registry — add new axes here
# ---------------------------------------------------------------------------

AXIS_REGISTRY: dict[str, ClosureAxisDef] = {}


def _reg(axis: ClosureAxisDef) -> None:
    AXIS_REGISTRY[axis.axis_id] = axis


# === Problem Formulation ===

_reg(ClosureAxisDef(
    axis_id="target_object",
    layer="problem_formulation",
    severity="blocking",
    priority=0,
    question_text="这个任务操作的主要对象是什么？",
    context_hint="需要明确任务的操作对象才能进入规划。",
    options=(
        ClarificationOptionDef("to_repo", "代码仓库", "repository", {"target_object": "repository"}),
        ClarificationOptionDef("to_web", "网页 / Web API", "web_resource", {"target_object": "web_resource"}),
        ClarificationOptionDef("to_dataset", "数据集 / CSV / 表格", "dataset", {"target_object": "dataset"}),
        ClarificationOptionDef("to_workspace", "本地工作区文件", "local_workspace", {"target_object": "local_workspace"}),
        ClarificationOptionDef("to_other", "其他", "generic_request", {"target_object": "generic_request"}, requires_free_text_suffix=True),
    ),
))

_reg(ClosureAxisDef(
    axis_id="problem_kind",
    layer="problem_formulation",
    severity="blocking",
    priority=1,
    question_text="这个任务的核心目标是什么？",
    context_hint="需要明确任务是分析、研究、数据处理还是其它类型。",
    options=(
        ClarificationOptionDef("pk_analysis", "分析 / 评估", "analysis", {"problem_kind": "analysis"}),
        ClarificationOptionDef("pk_research", "搜索 / 研究", "research", {"problem_kind": "research"}),
        ClarificationOptionDef("pk_data", "数据处理 / 汇总", "data_processing", {"problem_kind": "data_processing"}),
        ClarificationOptionDef("pk_synth", "综合 / 生成", "synthesis", {"problem_kind": "synthesis"}),
        ClarificationOptionDef("pk_other", "其他", "generic", {"problem_kind": "generic"}, requires_free_text_suffix=True),
    ),
))

# === Input Closure ===

_reg(ClosureAxisDef(
    axis_id="source_locator",
    layer="input_closure",
    severity="blocking",
    priority=2,
    question_text="主要输入数据在哪里？请提供 URL、文件路径或 API 地址。",
    context_hint="需要确定输入来源才能规划获取策略。",
    options=(
        ClarificationOptionDef("sl_other", "手动输入", "", {}, requires_free_text_suffix=True),
    ),
))

_reg(ClosureAxisDef(
    axis_id="source_modality",
    layer="input_closure",
    severity="blocking",
    priority=3,
    question_text="输入数据的获取方式是什么？",
    context_hint="获取方式决定了数据采集的策略和权限要求。",
    options=(
        ClarificationOptionDef("sm_local", "本地文件 / 路径", "local_path", {"source_modality": "local_path"}),
        ClarificationOptionDef("sm_api", "远程 API", "remote_api", {"source_modality": "remote_api"}),
        ClarificationOptionDef("sm_web", "网页抓取", "web_scrape", {"source_modality": "web_scrape"}),
        ClarificationOptionDef("sm_repo", "Git 仓库 checkout", "remote_repo", {"source_modality": "remote_repo"}),
        ClarificationOptionDef("sm_db", "数据库查询", "database", {"source_modality": "database"}),
        ClarificationOptionDef("sm_other", "其他", "unknown", {"source_modality": "unknown"}, requires_free_text_suffix=True),
    ),
))

_reg(ClosureAxisDef(
    axis_id="acquisition_path",
    layer="input_closure",
    severity="review",
    priority=4,
    question_text="数据获取的具体路径或协议是什么？",
    context_hint="明确采集路径有助于判断可行性。",
    options=(
        ClarificationOptionDef("ap_direct", "直接读取", "direct_read", {"acquisition_path": "direct_read"}),
        ClarificationOptionDef("ap_checkout", "Checkout 后分析", "checkout_then_inventory", {"acquisition_path": "checkout_then_inventory"}),
        ClarificationOptionDef("ap_fetch", "下载后处理", "fetch_then_ingest", {"acquisition_path": "fetch_then_ingest"}),
        ClarificationOptionDef("ap_search", "搜索后采集", "search_then_collect", {"acquisition_path": "search_then_collect"}),
    ),
))

# === Output Closure ===

_reg(ClosureAxisDef(
    axis_id="deliverable_form",
    layer="output_closure",
    severity="blocking",
    priority=5,
    question_text="最终产出物的类型是什么？",
    context_hint="确定交付物形式是规划的前提。",
    options=(
        ClarificationOptionDef("df_report_md", "Markdown 报告", "report", {"deliverable_form": "report", "output_modality": "markdown"}, is_default=True),
        ClarificationOptionDef("df_report_pdf", "PDF 报告", "report", {"deliverable_form": "report", "output_modality": "pdf"}),
        ClarificationOptionDef("df_json", "JSON 结构化数据", "structured_artifact", {"deliverable_form": "structured_artifact", "output_modality": "json"}),
        ClarificationOptionDef("df_csv", "CSV 数据", "structured_artifact", {"deliverable_form": "structured_artifact", "output_modality": "csv"}),
        ClarificationOptionDef("df_message", "消息 / 通知", "message", {"deliverable_form": "message"}),
        ClarificationOptionDef("df_other", "其他", "generic_output", {"deliverable_form": "generic_output"}, requires_free_text_suffix=True),
    ),
))

_reg(ClosureAxisDef(
    axis_id="delivery_target",
    layer="output_closure",
    severity="blocking",
    priority=6,
    question_text="最终产出物交付到哪里？",
    context_hint="明确交付目标以便规划输出路径。",
    options=(
        ClarificationOptionDef("dt_local", "本地文件系统", "local_filesystem", {"delivery_target": "local_filesystem"}, is_default=True),
        ClarificationOptionDef("dt_user", "直接返回给用户", "user_direct", {"delivery_target": "user_direct"}),
        ClarificationOptionDef("dt_cloud", "云存储", "cloud_storage", {"delivery_target": "cloud_storage"}),
        ClarificationOptionDef("dt_email", "邮件发送", "email", {"delivery_target": "email"}),
        ClarificationOptionDef("dt_other", "其他", "unspecified", {"delivery_target": "unspecified"}, requires_free_text_suffix=True),
    ),
))

_reg(ClosureAxisDef(
    axis_id="output_modality",
    layer="output_closure",
    severity="review",
    priority=7,
    question_text="产出物的格式是什么？",
    context_hint="明确格式以便规划渲染方式。",
    options=(
        ClarificationOptionDef("om_md", "Markdown", "markdown", {"output_modality": "markdown"}, is_default=True),
        ClarificationOptionDef("om_json", "JSON", "json", {"output_modality": "json"}),
        ClarificationOptionDef("om_csv", "CSV", "csv", {"output_modality": "csv"}),
        ClarificationOptionDef("om_pdf", "PDF", "pdf", {"output_modality": "pdf"}),
        ClarificationOptionDef("om_html", "HTML", "html", {"output_modality": "html"}),
    ),
))


# ---------------------------------------------------------------------------
# Semantic side-effect helpers
# ---------------------------------------------------------------------------

# problem_kind values that describe pure synthesis / generation — the task
# produces an output from internal capability, not by operating on external
# input data. When the resolved problem_kind lands in this set, the input
# closure layer is considered ``not_applicable`` and the deterministic layer
# auto-closes it. This keeps the user from being asked "where is the data?"
# for a task like "generate a picture of a red dragon".
#
# The set is deliberately narrow. Anything that downstream still needs to
# retrieve or transform data (research, analysis, data_processing, synthesis
# *of a research result*) stays outside this set because the input closure
# is still meaningful. Add values here only when the semantics are "no
# external input is part of the task".
SYNTHESIS_PROBLEM_KINDS: frozenset[str] = frozenset({
    "synthesis",            # registry option pk_synth → "综合 / 生成"
    "generation",
    "generate",
    "content_generation",
    "creative_generation",
    "creation",
    "image_generation",
    "text_generation",
})


def is_synthesis_problem_kind(problem_kind: Optional[str]) -> bool:
    """Return True when the resolved problem_kind makes input closure
    semantically not applicable.
    """
    if not problem_kind:
        return False
    return problem_kind.lower() in SYNTHESIS_PROBLEM_KINDS


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_registered_axis_ids() -> set[str]:
    """Return all registered axis ids as a frozen set."""
    return set(AXIS_REGISTRY.keys())


def get_axis(axis_id: str) -> Optional[ClosureAxisDef]:
    return AXIS_REGISTRY.get(axis_id)


def get_axes_for_layer(layer: FrontChainLayerName) -> list[ClosureAxisDef]:
    return [a for a in AXIS_REGISTRY.values() if a.layer == layer]


def get_prompt_axis_list() -> str:
    """Build a compact axis list for inclusion in the interpreter prompt.

    Example output::

        problem_formulation: target_object, problem_kind
        input_closure: source_locator, source_modality, acquisition_path
        output_closure: deliverable_form, delivery_target, output_modality
    """
    by_layer: dict[str, list[str]] = {}
    for axis in AXIS_REGISTRY.values():
        by_layer.setdefault(axis.layer, []).append(axis.axis_id)
    return "\n".join(
        f"  {layer}: {', '.join(ids)}"
        for layer, ids in by_layer.items()
    )
