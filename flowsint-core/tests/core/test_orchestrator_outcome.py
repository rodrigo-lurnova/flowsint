"""Aggregated outcome of a flow: sources, findings and partial failures."""

import json
from typing import Any, Dict, List

import pytest

from flowsint_core.core import enricher_base as enricher_base_module
from flowsint_core.core import orchestrator as orch_module
from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.findings import (
    Finding,
    SourceRun,
    compute_outcome,
    dedupe_findings,
)
from flowsint_core.core.orchestrator import FlowOrchestrator
from flowsint_core.core.types import FlowBranch, FlowStep

NODE = {"label": "node"}


def _finding(confidence=0.5, **overrides) -> Finding:
    data: Dict[str, Any] = dict(
        subject="A",
        relation="administra",
        object="B",
        source="a",
        certainty="probable",
        confidence=confidence,
    )
    data.update(overrides)
    return Finding(**data)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["ok", "ok"], "complete"),
        (["ok", "failed"], "partial"),
        (["failed", "failed"], "failed"),
        (["failed", "skipped"], "failed"),
        (["ok", "skipped"], "complete"),
        (["skipped"], "failed"),
        ([], "failed"),
    ],
)
def test_compute_outcome(statuses, expected):
    runs = [SourceRun(source=f"s{i}", status=s) for i, s in enumerate(statuses)]
    assert compute_outcome(runs) == expected


def test_compute_outcome_failed_run_with_findings_is_partial():
    runs = [
        SourceRun(source="a", status="failed", error="timeout", findings=2),
        SourceRun(source="b", status="skipped"),
    ]
    assert compute_outcome(runs) == "partial"


def test_dedupe_findings_ignores_confidence_and_keeps_the_highest():
    low, high = _finding(confidence=0.2), _finding(confidence=0.9)
    other = _finding(object="C")
    result = dedupe_findings([low, other, high])
    assert result == [high, other]


def test_dedupe_findings_prefers_a_confidence_over_none():
    result = dedupe_findings([_finding(confidence=None), _finding(confidence=0.1)])
    assert [f.confidence for f in result] == [0.1]


# --- orchestrator -----------------------------------------------------------


class Script:
    """Per-enricher-name behaviours, one consumed per execute (last repeats)."""

    def __init__(self, behaviours: Dict[str, List[Any]]):
        self.behaviours = behaviours
        self.calls: Dict[str, int] = {}
        self.inputs: Dict[str, List[Any]] = {}

    def next(self, name: str) -> Any:
        self.calls[name] = self.calls.get(name, 0) + 1
        queue = self.behaviours.get(name, [[]])
        return queue[min(self.calls[name], len(queue)) - 1]


class FakeRegistry:
    def __init__(self, script: Script):
        self.script = script

    def enricher_exists(self, name: str) -> bool:
        return True

    def get_enricher(self, name, sketch_id, scan_id, vault=None, params=None):
        script = self.script

        class Fake(Enricher):
            @classmethod
            def name(cls):
                return name

            @classmethod
            def category(cls):
                return "Test"

            @classmethod
            def key(cls):
                return "value"

            async def scan(self, values):
                script.inputs.setdefault(name, []).append(values)
                behaviour = script.next(name)
                if isinstance(behaviour, Exception):
                    raise behaviour
                if isinstance(behaviour, dict):
                    for message in behaviour.get("failures", []):
                        self.add_failure(message)
                    for f in behaviour.get("findings", []):
                        self.add_finding(f)
                    return behaviour["results"]
                return behaviour

        return Fake(sketch_id, scan_id, params=params, graph_service=_NullGraph())


class _NullGraph:
    def flush(self):
        pass


def step(node_id: str) -> FlowStep:
    return FlowStep(
        nodeId=node_id,
        type="enricher",
        inputs={},
        outputs={},
        status="pending",
        branchId="b",
        depth=0,
    )


def branch(name: str, *node_ids: str) -> FlowBranch:
    return FlowBranch(id=name, name=name, steps=[step(n) for n in node_ids])


@pytest.fixture
def run_flow_with(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the orchestrator writes enricher_logs/ to the cwd
    monkeypatch.setattr(orch_module, "Logger", _RecordingLogger())
    monkeypatch.setattr(enricher_base_module, "Logger", _RecordingLogger())

    def runner(behaviours, branches, values=None, hook=None):
        script = Script(behaviours)
        monkeypatch.setattr(orch_module, "ENRICHER_REGISTRY", FakeRegistry(script))
        flow = FlowOrchestrator("sketch", "scan", branches)
        if hook:
            hook(flow)
        result = flow.scan(values if values is not None else [NODE])
        runner.script = script
        return result

    return runner


class _RecordingLogger:
    def __init__(self):
        self.completed_calls: List[Any] = []
        self.error_calls: List[Any] = []

    def __getattr__(self, attr):
        if attr == "completed":
            return lambda *a, **k: self.completed_calls.append(a)
        if attr == "error":
            return lambda *a, **k: self.error_calls.append(a)
        return lambda *a, **k: None


def statuses(result):
    return [s["status"] for s in result["sources"]]


def test_failing_step_does_not_abort_other_branches(run_flow_with):
    r = run_flow_with(
        {"a": [RuntimeError("caída")], "b": [[NODE]]},
        [branch("one", "a-1"), branch("two", "b-1")],
    )
    assert r["outcome"] == "partial"
    assert statuses(r) == ["failed", "ok"]
    assert [b["id"] for b in r["branches"]] == ["one", "two"]
    assert "caída" in r["sources"][0]["error"]


def test_step_without_inputs_is_skipped(run_flow_with):
    r = run_flow_with(
        {"a": [RuntimeError("caída")], "b": [[NODE]]},
        [branch("one", "a-1", "b-1")],
    )
    assert statuses(r) == ["failed", "skipped"]
    assert r["outcome"] == "failed"
    assert [s["source"] for s in r["sources"]] == ["a", "b"]


def test_step_after_a_legitimately_empty_predecessor_is_skipped(run_flow_with):
    r = run_flow_with({"a": [[]], "b": [[NODE]]}, [branch("one", "a-1", "b-1")])
    assert statuses(r) == ["ok", "skipped"]
    assert r["outcome"] == "complete"


def test_cached_enricher_counts_once(run_flow_with):
    f = _finding()
    r = run_flow_with(
        {"a": [{"results": [NODE], "findings": [f]}]},
        [branch("one", "a-1"), branch("two", "a-1")],
    )
    assert run_flow_with.script.calls["a"] == 1
    assert statuses(r) == ["ok"]
    assert len(r["findings"]) == 1
    assert r["sources"][0]["findings"] == 1


def test_one_instance_run_with_different_inputs_yields_one_run_each(run_flow_with):
    r = run_flow_with(
        {
            "a": [
                {"results": [NODE], "findings": [_finding(object="B")]},
                {"results": [NODE], "findings": [_finding(object="C")]},
            ],
            "b": [[{"label": "other"}]],
        },
        [branch("one", "a-1"), branch("two", "b-1", "a-1")],
    )
    assert run_flow_with.script.calls["a"] == 2
    assert [s["source"] for s in r["sources"]] == ["a", "b", "a"]
    assert sorted(f["object"] for f in r["findings"]) == ["B", "C"]


def test_shared_failed_predecessor_gives_a_single_skipped_run(run_flow_with):
    r = run_flow_with(
        {"a": [RuntimeError("caída")]},
        [branch("one", "a-1", "c-1"), branch("two", "a-1", "c-1")],
    )
    assert [s["source"] for s in r["sources"]] == ["a", "c"]
    assert statuses(r) == ["failed", "skipped"]


def test_cached_failure_keeps_failing_its_branch(run_flow_with):
    r = run_flow_with(
        {"a": [RuntimeError("caída")], "c": [[NODE]]},
        [branch("one", "a-1"), branch("two", "a-1", "c-1")],
    )
    assert run_flow_with.script.calls["a"] == 1
    assert "c" not in run_flow_with.script.calls
    assert statuses(r) == ["failed", "skipped"]


def test_skipped_in_one_branch_but_run_in_another_keeps_the_real_run(run_flow_with):
    r = run_flow_with(
        {"a": [RuntimeError("caída")], "c": [[NODE]]},
        [branch("one", "a-1", "c-1"), branch("two", "c-1")],
    )
    assert [s["source"] for s in r["sources"]] == ["a", "c"]
    assert statuses(r) == ["failed", "ok"]


def test_missing_enricher_node_is_a_failed_run_and_next_branch_continues(
    run_flow_with,
):
    r = run_flow_with(
        {"b": [[NODE]]},
        [branch("one", "a-1", "c-1"), branch("two", "b-1")],
        hook=lambda flow: flow.enrichers.pop("a-1"),
    )
    assert statuses(r) == ["failed", "skipped", "ok"]
    assert "a-1" in r["sources"][0]["error"]
    assert r["outcome"] == "partial"


def test_unexpected_orchestrator_error_is_a_failed_run(run_flow_with, monkeypatch):
    def boom(self, outputs, step_outputs, results_mapping):
        raise KeyError("boom")

    monkeypatch.setattr(FlowOrchestrator, "update_results_mapping", boom)
    r = run_flow_with(
        {"a": [[NODE]], "b": [[NODE]]},
        [branch("one", "a-1", "c-1"), branch("two", "b-1")],
    )
    assert statuses(r) == ["failed", "skipped", "failed"]
    assert r["outcome"] == "failed"


def test_findings_are_deduplicated_and_json_safe(run_flow_with):
    low, high = _finding(confidence=0.2), _finding(confidence=0.9)
    r = run_flow_with(
        {
            "a": [{"results": [NODE], "findings": [low]}],
            "b": [{"results": [NODE], "findings": [high]}],
        },
        [branch("one", "a-1"), branch("two", "b-1")],
    )
    assert [f["confidence"] for f in r["findings"]] == [0.9]
    json.dumps(r["sources"])
    json.dumps(r["findings"])
    assert r["initial_values"] and "results" in r and "reference_mapping" in r


def test_success_log_is_not_emitted_when_everything_failed(run_flow_with):
    logger = orch_module.Logger  # replaced by the fixture
    r = run_flow_with({"a": [RuntimeError("caída")]}, [branch("one", "a-1")])
    assert r["outcome"] == "failed"
    assert logger.completed_calls == []
    assert logger.error_calls


def test_success_log_is_emitted_when_complete(run_flow_with):
    logger = orch_module.Logger
    run_flow_with({"a": [[NODE]]}, [branch("one", "a-1")])
    assert logger.completed_calls


PARTIAL = {"results": [{"label": "partial"}], "failures": ["una fuente cayó"]}


def test_failed_run_with_outputs_does_not_kill_its_branch(run_flow_with):
    r = run_flow_with({"a": [PARTIAL], "c": [[NODE]]}, [branch("one", "a-1", "c-1")])
    assert statuses(r) == ["failed", "ok"]
    assert r["outcome"] == "partial"
    assert run_flow_with.script.inputs["c"] == [[{"label": "partial"}]]


def test_failed_run_with_outputs_continues_through_a_cache_hit(run_flow_with):
    r = run_flow_with(
        {"a": [PARTIAL], "c": [[NODE]]},
        [branch("one", "a-1"), branch("two", "a-1", "c-1")],
    )
    assert run_flow_with.script.calls["a"] == 1
    assert statuses(r) == ["failed", "ok"]
    assert r["outcome"] == "partial"
    assert run_flow_with.script.inputs["c"] == [[{"label": "partial"}]]


def test_failed_run_without_outputs_still_skips_the_rest(run_flow_with):
    r = run_flow_with(
        {"a": [{"results": [], "failures": ["caída"]}], "c": [[NODE]]},
        [branch("one", "a-1", "c-1")],
    )
    assert statuses(r) == ["failed", "skipped"]
    assert r["outcome"] == "failed"
