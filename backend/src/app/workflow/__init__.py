"""Declarative Workflows subsystem (FEAT-0062, PRP-0118, UDR-0101).

A `kind: Workflow` declarative YAML is COMPILED to a Microsoft Agent Framework
``Workflow`` graph (CTR-0180) whose agent nodes are ChatWalaʻau-built declarative
``kind: Prompt`` agents (spec, not engine -- UDR-0101 D1/D4). Workflows are
discovered from the SAME ``DECLARATIVE_AGENTS_DIR`` tree as agents, dispatched by
``kind`` (UDR-0101 D2), and run in either lane (CTR-0181): interactively over the
AG-UI SSE transport (a per-conversation run-target, NOT a persona -- UDR-0101 D3/D5)
or asynchronously as a ``workflow`` Pipeline job type (UDR-0074 D7 extension point).

Public surface:
  * loader.load_workflow_inventory / resolve_workflow / compile_workflow  (CTR-0180)
  * runtime.stream_workflow / run_workflow_job / register_workflow_job_type (CTR-0181)
  * router.register_workflows                                             (CTR-0182/0183)
"""

from __future__ import annotations

from app.workflow.spec import WorkflowError, WorkflowSpec

__all__ = ["WorkflowError", "WorkflowSpec"]
