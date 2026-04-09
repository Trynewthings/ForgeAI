# Template Registry Design Note

## Goal

Define the smallest template system that fits the current compiler architecture.

The design target is:

- deterministic-first
- family-scoped
- registry-based
- slot-only overlay
- observable in compiler trace

## Why This Fits The Current Compiler

The compiler already has a stable ordered flow:

1. normalize/analyze the requirement
2. recommend a legal blueprint family
3. build a deterministic skeleton
4. optionally fill descriptive slots with `llm_assisted`
5. validate/repair using the existing schema-driven pipeline

The template system fits between steps 2 and 3:

1. analyze requirement
2. recommend legal family
3. select template within that family
4. build deterministic skeleton
5. apply template overlay to existing slots
6. optionally pass template metadata to `llm_assisted` filler

This preserves:

- compiler layering
- family legality controls
- deterministic structural authority
- existing validation and compile history

## Registry Design

The registry is a static in-code catalog, not a new platform feature.

Each template definition contains:

- `template_id`
- `name`
- `family`
- `description`
- `tags`
- `match_rules`
- `slot_hints`

### Match rules

Match rules are deterministic and structured:

- `domains`
- `recurrence_modes`
- `required_tools_any`
- `keywords_any`
- `human_review_required`

These rules are deliberately simple so matching stays transparent and testable.

### Slot hints

Slot hints are intentionally narrow and map only onto existing deterministic payload slots:

- blueprint description
- known step descriptions/purpose text
- known artifact descriptions
- known variable hints/defaults
- template tags

If a hint does not map cleanly to an existing slot, it is dropped.

## Matcher Logic

The matcher accepts:

- normalized requirement
- selected family

The matcher behavior is:

- filter registry to templates in the selected family
- score candidates with transparent rule hits
- sort candidates by score
- return a structured `TemplateSelectionResult`

This result includes:

- selected template id/name/family
- candidate list
- match reasons
- selection mode
- confidence/score

The matcher does not:

- choose family
- generate new template definitions
- call the LLM

## Authority Boundaries

### Family authority

Family authority remains independent.

The family recommender stays authoritative because:

- it runs before template selection
- template matching only sees the final selected family
- the template registry is family-scoped

### Skeleton authority

Deterministic skeleton generation remains authoritative because:

- structure is generated before overlay
- overlay is limited to existing slots only
- no template path can add or reorder graph structure

### LLM filler authority

`llm_assisted` filler authority does not expand because:

- template data is added only as prompt input context
- prompt contracts and output schema remain unchanged
- the filler still applies only bounded slot fills

## Observable Surfaces

v3.0 intentionally reuses existing observability surfaces.

### Decision trace

Compiler trace now includes `template_selection` with:

- `selected_template_id`
- `selected_template_name`
- `selected_family`
- `matched_candidates`
- `match_reasons`
- `selection_mode`

### Blueprint tags

Compiled blueprint drafts use `template_tags_json` to expose:

- `template_id`
- `template_name`
- `template_family`
- `template_source=registry`

### Compile history

No new persistence table is introduced.

Compile history continues to store compiler result payloads, which now include the template trace.

## Non-Goals

This design explicitly does not include:

- template CRUD
- template database persistence
- template lifecycle/version governance
- template marketplaces or UI libraries
- LLM template generation
- template-first family selection
- cross-family promotion logic

## Fixed Review Checklist

Use this checklist when changing template behavior:

- `family authority independent`: template selection still happens only after legal family recommendation
- `overlay slot-only`: overlay only modifies pre-existing deterministic slots
- `llm filler authority unchanged`: template hints do not widen filler output scope
- `trace/tags/history complete`: template usage remains visible in result trace, blueprint tags, and compile history
