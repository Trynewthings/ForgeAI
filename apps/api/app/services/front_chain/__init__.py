"""Front-chain authority island (Phase 1A).

This package is the new authoritative pipeline for front-chain understanding.
It is intentionally decoupled from:
    * ``app.services.requirement.problem_formulation_service``
    * ``app.services.requirement.solvability_service``
    * ``app.services.requirement.requirement_compiler_service``
    * ``app.services.requirement.compiler`` (entire subpackage)
    * ``app.services.requirement.requirement_clarification_service``
    * ``app.services.requirement_clarifier_service``
    * ``app.services.requirement_session_service``
    * ``app.services.requirement.problem_formulation_llm_service``

Modules in this package are allowed to import:
    * Shared contract schemas from ``app.schemas.problem_formulation``
    * Shared substrate type from ``app.schemas.semantic_core`` (read-only
      substrate hint; must not be used as a fallback authority source)
    * Provider runtime helpers from ``app.services.provider_runtime``
    * Capability registry for capability signatures (Phase 1B/2 concerns)

A static AST test (``tests/test_front_chain_phase1.py::
test_new_front_chain_package_does_not_import_old_services``) enforces the
forbidden-import list.
"""
