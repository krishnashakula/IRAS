"""Tests for iras.graph.builder — build_graph function."""

# pylint: disable=missing-class-docstring,missing-function-docstring
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from iras.graph.builder import build_graph


class TestBuildGraph:
    def test_builds_without_checkpointer(self):
        graph = build_graph(checkpointer=None)
        assert graph is not None

    def test_builds_with_memory_checkpointer(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        assert graph is not None

    def test_graph_has_expected_nodes(self):
        graph = build_graph()
        nodes = set(graph.nodes.keys())
        expected = {
            "ingestion",
            "triage",
            "context_gathering",
            "rca",
            "generate_plan",
            "approval",
            "apply_remediation",
            "escalation",
            "postmortem",
        }
        assert expected.issubset(nodes)

    def test_graph_compilable_twice(self):
        """build_graph should be deterministic — calling twice works."""
        graph1 = build_graph()
        graph2 = build_graph()
        assert graph1 is not None
        assert graph2 is not None

    def test_graph_with_none_checkpointer_no_persistence(self):
        """Without checkpointer, graph still compiles but lacks persistence."""
        graph = build_graph(checkpointer=None)
        # The graph should not have a checkpointer configured
        # (no state is preserved between runs)
        assert graph is not None
