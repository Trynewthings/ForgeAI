# Golden Compiler Reliability Report

- total runs: 16
- mocked llm runs: 8
- analyzer validation success: 16/16 (100.00%)
- family recommendation success: 15/16 (93.75%)
- filler validation success: 16/16 (100.00%)
- repair attempts: 2
- repair successes: 1 (50.00%)
- fallback events: 1 (6.25% per run)
- fallback runs: 1 (6.25% of runs)

## Per-Case Summary

### Markdown Blog Post Polisher

- `deterministic` `compile`: family=`single_service`, completion=`ready_to_compile`, draft_readiness=`ready_for_build`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0
- `llm_assisted` `compile`: family=`single_service`, completion=`ready_to_compile`, draft_readiness=`ready_for_build`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0

### CSV Sales Report Generator

- `deterministic` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `llm_assisted` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0

### Weekly AI Research Digest

- `deterministic` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `llm_assisted` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `deterministic` `recompile`: family=`workflow`, completion=`ready_to_compile`, draft_readiness=`ready_for_build`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0
- `llm_assisted` `recompile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0

### Company News Monitor

- `deterministic` `compile`: family=`single_service`, completion=`clarification_required`, draft_readiness=`blocked_by_clarification`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `llm_assisted` `compile`: family=`single_service`, completion=`clarification_required`, draft_readiness=`blocked_by_clarification`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=1
- `deterministic` `recompile`: family=`workflow`, completion=`ready_to_compile`, draft_readiness=`ready_for_build`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0
- `llm_assisted` `recompile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`false`, fallback_count=0

### Customer Support Response Draft Generator

- `deterministic` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `llm_assisted` `compile`: family=`workflow`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0

### Startup Pitch Deck Reviewer

- `deterministic` `compile`: family=`multi_agent`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0
- `llm_assisted` `compile`: family=`multi_agent`, completion=`compilable_with_warnings`, draft_readiness=`compilable_with_warnings`, validation_valid=`true`, clarification_triggered=`true`, fallback_count=0

## Failure Reasons

- stage=`family_recommendation` reason=`repair_invalid` count=1 runs=company_news_monitor:llm_assisted:compile
