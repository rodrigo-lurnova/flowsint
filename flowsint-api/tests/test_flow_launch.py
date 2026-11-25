"""POST /api/flows/{id}/launch: client-chosen scan_id and queue expiry."""

import importlib
import uuid
from unittest.mock import MagicMock

import pytest

from app.api.routes import flows as flows_route
from flowsint_core.core.auth import create_access_token
from flowsint_core.core.models import (
    Flow,
    Investigation,
    InvestigationUserRole,
    Profile,
    Role,
    Scan,
    Sketch,
)


def _user(db, email):
    p = Profile(email=email, hashed_password="x")
    db.add(p)
    db.commit()
    return p, {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


@pytest.fixture
def world(db_session, monkeypatch):
    owner, headers = _user(db_session, "owner@example.com")
    stranger, stranger_h = _user(db_session, "stranger@example.com")
    inv = Investigation(name="inv", owner_id=owner.id)
    db_session.add(inv)
    db_session.commit()
    db_session.add(
        InvestigationUserRole(
            user_id=owner.id, investigation_id=inv.id, roles=[Role.OWNER]
        )
    )
    sketch = Sketch(title="s", investigation_id=inv.id, owner_id=owner.id)
    flow = Flow(name="f", flow_schema={"nodes": [], "edges": []}, owner_id=owner.id)
    db_session.add_all([sketch, flow])
    db_session.commit()

    graph = MagicMock()
    graph.get_nodes_by_ids_for_task.return_value = []
    monkeypatch.setattr(flows_route, "create_graph_service", lambda **kw: graph)
    monkeypatch.setattr(
        flows_route, "create_type_registry_service", lambda db: MagicMock()
    )
    calls = []

    def fake_send_task(name, args=None, kwargs=None, **options):
        calls.append({"name": name, "args": args, "options": options})
        return MagicMock(id=options.get("task_id", "generated-id"))

    monkeypatch.setattr(flows_route.celery, "send_task", fake_send_task)
    return {
        "db": db_session,
        "flow": flow,
        "sketch": sketch,
        "h": headers,
        "stranger_h": stranger_h,
        "calls": calls,
    }


def _launch(client, w, headers=None, **extra):
    body = {"node_ids": [], "sketch_id": str(w["sketch"].id), **extra}
    return client.post(
        f"/api/flows/{w['flow'].id}/launch", json=body, headers=headers or w["h"]
    )


def test_without_scan_id_nothing_changes(client, world):
    res = _launch(client, world)
    assert res.status_code == 200
    assert res.json() == {"id": "generated-id"}
    [call] = world["calls"]
    assert "task_id" not in call["options"]
    assert "expires" not in call["options"]


def test_scan_id_becomes_task_id_and_response_id(client, world):
    sid = uuid.uuid4()
    res = _launch(client, world, scan_id=str(sid).upper())
    assert res.status_code == 200
    assert res.json() == {"id": str(sid)}
    [call] = world["calls"]
    assert call["options"]["task_id"] == str(sid)


def test_non_uuid4_scan_id_is_rejected(client, world):
    assert _launch(client, world, scan_id="nope").status_code == 422
    assert _launch(client, world, scan_id=str(uuid.uuid1())).status_code == 422
    assert world["calls"] == []


def test_existing_scan_id_is_409_and_sends_nothing(client, world):
    # Owned by someone else's sketch: still the same opaque answer.
    other = Scan(sketch_id=None)
    world["db"].add(other)
    world["db"].commit()
    res = _launch(client, world, scan_id=str(other.id))
    assert res.status_code == 409
    assert res.json() == {"detail": "scan_id already used"}
    assert world["calls"] == []


def test_permission_checks_come_before_the_409(client, world):
    used = Scan(sketch_id=world["sketch"].id)
    world["db"].add(used)
    world["db"].commit()
    res = _launch(client, world, headers=world["stranger_h"], scan_id=str(used.id))
    assert res.status_code in (403, 404)
    assert world["calls"] == []


def test_expiry_is_passed_to_send_task(client, world, monkeypatch):
    monkeypatch.setattr(flows_route, "FLOW_EXPIRES_SECONDS", 300)
    assert _launch(client, world).status_code == 200
    assert world["calls"][0]["options"]["expires"] == 300


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_expiry_unset_means_none(raw):
    assert flows_route.parse_flow_expires(raw) is None


def test_expiry_positive_integer():
    assert flows_route.parse_flow_expires(" 90 ") == 90


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
def test_expiry_invalid_aborts_startup(raw, monkeypatch):
    monkeypatch.setenv("FLOWSINT_FLOW_EXPIRES_SECONDS", raw)
    try:
        with pytest.raises(RuntimeError, match="FLOWSINT_FLOW_EXPIRES_SECONDS"):
            importlib.reload(flows_route)
    finally:
        monkeypatch.delenv("FLOWSINT_FLOW_EXPIRES_SECONDS")
        importlib.reload(flows_route)
