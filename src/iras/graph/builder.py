"""LangGraph state machine builder for IRAS.

Wires together all 9 nodes with their conditional edges and compiles the
graph with the provided checkpointer.

Graph topology:
    START
      │
    ingestion
      │
    triage
      │
    context_gathering  ◄──────────────────────────────┐
      │                                                │
    rca ── confidence < threshold, attempts < max ────┘
      │
      ├── confidence < threshold, attempts >= max → escalation
      │
      └── confidence >= threshold → generate_plan
                                        │
                                    approval  (interrupt)
                                        │
                           ┌─── approved ──────────────┐
                           │                            │
                   apply_remediation              escalation
                           │                            │
                           └──────── postmortem ────────┘
                                          │
                                         END
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from iras.graph.nodes.apply_remediation import apply_remediation_node
from iras.graph.nodes.approval import approval_node, route_after_approval
from iras.graph.nodes.context_gathering import context_gathering_node
from iras.graph.nodes.escalation import escalation_node
from iras.graph.nodes.generate_plan import generate_plan_node
from iras.graph.nodes.ingestion import ingestion_node
from iras.graph.nodes.postmortem import postmortem_node
from iras.graph.nodes.rca import rca_node, route_after_rca
from iras.graph.nodes.triage import triage_node
from iras.graph.state import IncidentState


def build_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    """Construct and compile the IRAS incident response graph.

    Args:
        checkpointer: LangGraph checkpoint saver.  Pass None for in-memory
            (no persistence) — useful in unit tests.

    Returns:
        A compiled CompiledStateGraph ready to be invoked.
    """
    workflow = StateGraph(IncidentState)

    # ── Register nodes ────────────────────────────────────────────────────────
    workflow.add_node("ingestion", ingestion_node)
    workflow.add_node("triage", triage_node)
    workflow.add_node("context_gathering", context_gathering_node)
    workflow.add_node("rca", rca_node)
    workflow.add_node("generate_plan", generate_plan_node)
    workflow.add_node("approval", approval_node)
    workflow.add_node("apply_remediation", apply_remediation_node)
    workflow.add_node("escalation", escalation_node)
    workflow.add_node("postmortem", postmortem_node)

    # ── Register edges ────────────────────────────────────────────────────────
    workflow.add_edge(START, "ingestion")
    workflow.add_edge("ingestion", "triage")
    workflow.add_edge("triage", "context_gathering")
    workflow.add_edge("context_gathering", "rca")

    # RCA → conditional (retry, generate_plan, or escalate)
    workflow.add_conditional_edges(
        "rca",
        route_after_rca,
        {
            "context_gathering": "context_gathering",
            "generate_plan": "generate_plan",
            "escalation": "escalation",
        },
    )

    workflow.add_edge("generate_plan", "approval")

    # Approval → conditional (apply or escalate)
    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "apply_remediation": "apply_remediation",
            "escalation": "escalation",
        },
    )

    workflow.add_edge("apply_remediation", "postmortem")
    workflow.add_edge("escalation", "postmortem")
    workflow.add_edge("postmortem", END)

    # ── Compile ───────────────────────────────────────────────────────────────
    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    return workflow.compile(**compile_kwargs)
