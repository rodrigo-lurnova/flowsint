"""run_flow derives the scan status from the aggregated flow outcome."""

import uuid
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from flowsint_core.core.enums import EventLevel
from flowsint_core.core.models import Scan
from flowsint_core.tasks import flow as flow_task

BRANCH = {
    "id": "b1",
    "name": "branch",
    "steps": [
        {
            "nodeId": "a-1",
            "type": "enricher",
            "inputs": {},
            "outputs": {},
            "status": "pending",
            "branchId": "b1",
            "depth": 0,
        }
    ],
}


def _results(outcome: str, sources) -> Dict[str, Any]:
    return {
        "initial_values": [],
        "branches": [],
        "results": {},
        "outcome": outcome,
        "sources": sources,
        "findings": [],
    }


@pytest.fixture
def run(monkeypatch):
    session = MagicMock()
    added = []
    session.add.side_effect = added.append
    monkeypatch.setattr(flow_task, "SessionLocal", lambda: session)

    def runner(results: Dict[str, Any]) -> Scan:
        class FakeOrchestrator:
            def __init__(self, **kwargs):
                pass

            def scan(self, values):
                return results

        monkeypatch.setattr(flow_task, "FlowOrchestrator", FakeOrchestrator)
        added.clear()
        flow_task.run_flow.apply(args=[[BRANCH], [], None], throw=True)
        return added[0]

    return runner


def test_run_flow_all_failed_marks_scan_failed(run):
    scan = run(
        _results(
            "failed",
            [{"source": "a", "status": "failed", "error": "timeout", "findings": 0}],
        )
    )
    assert scan.status == EventLevel.FAILED
    assert scan.details["outcome"] == "failed"
    assert "a: timeout" in scan.error
    assert scan.completed_at is not None


def test_run_flow_partial_is_completed_with_outcome(run):
    scan = run(
        _results(
            "partial",
            [
                {"source": "a", "status": "failed", "error": "timeout", "findings": 0},
                {"source": "b", "status": "ok", "error": None, "findings": 1},
            ],
        )
    )
    assert scan.status == EventLevel.COMPLETED
    assert scan.details["outcome"] == "partial"
    assert scan.error is None
    assert scan.completed_at is not None


def test_run_flow_complete_is_completed(run):
    scan = run(_results("complete", [{"source": "a", "status": "ok", "findings": 0}]))
    assert scan.status == EventLevel.COMPLETED


def test_run_flow_failed_without_source_errors_still_has_a_message(run):
    scan = run(_results("failed", []))
    assert scan.status == EventLevel.FAILED
    assert scan.error


def test_run_flow_exception_sets_completed_at_and_reraises(monkeypatch):
    session = MagicMock()
    scan = Scan(id=uuid.uuid4(), status=EventLevel.PENDING)
    session.query.return_value.filter.return_value.first.return_value = scan
    monkeypatch.setattr(flow_task, "SessionLocal", lambda: session)

    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(flow_task, "FlowOrchestrator", Boom)
    result = flow_task.run_flow.apply(args=[[BRANCH], [], None])
    assert result.failed()
    assert scan.status == EventLevel.FAILED
    assert "boom" in scan.error
    assert scan.completed_at is not None


def test_flow_tasks_are_not_acked_late():
    assert flow_task.celery.conf.task_acks_late is False


def test_run_flow_duplicate_scan_id_stops_and_keeps_existing_scan(monkeypatch):
    """Same task id twice (two launches, or a Celery redelivery): the second
    one must not run the flow nor touch the first one's scan."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from flowsint_core.core.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(flow_task, "SessionLocal", Session)

    ran = []

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        def scan(self, values):
            ran.append(1)
            return _results("success", [])

    monkeypatch.setattr(flow_task, "FlowOrchestrator", FakeOrchestrator)

    task_id = str(uuid.uuid4())
    flow_task.run_flow.apply(args=[[BRANCH], [], None], task_id=task_id, throw=True)
    assert ran == [1]

    check = Session()
    first = check.get(Scan, uuid.UUID(task_id))
    assert first.status == EventLevel.COMPLETED
    done_at = first.completed_at
    check.close()

    result = flow_task.run_flow.apply(
        args=[[BRANCH], [], None], task_id=task_id, throw=True
    )
    assert ran == [1]  # the duplicate ran nothing
    assert not isinstance(result.result, IntegrityError)

    check = Session()
    again = check.get(Scan, uuid.UUID(task_id))
    assert again.status == EventLevel.COMPLETED
    assert again.error is None
    assert again.completed_at == done_at
    check.close()
