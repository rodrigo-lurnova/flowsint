"""Contract of GET /api/scans/{id} (full result) and /api/scans/sketch/{id} (light)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from flowsint_core.core.auth import create_access_token
from flowsint_core.core.models import (
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
def world(db_session):
    owner, owner_h = _user(db_session, "owner@example.com")
    _, other_h = _user(db_session, "other@example.com")
    inv = Investigation(name="inv", owner_id=owner.id)
    db_session.add(inv)
    db_session.commit()
    db_session.add(
        InvestigationUserRole(
            user_id=owner.id, investigation_id=inv.id, roles=[Role.OWNER]
        )
    )
    sketch = Sketch(title="s", investigation_id=inv.id, owner_id=owner.id)
    db_session.add(sketch)
    db_session.commit()
    return {"db": db_session, "sketch": sketch, "h": owner_h, "other_h": other_h}


def _scan(w, **kw):
    s = Scan(sketch_id=w["sketch"].id, **kw)
    w["db"].add(s)
    w["db"].commit()
    if "started_at" in kw and kw["started_at"] is None:
        s.started_at = None  # column default would otherwise fill it in
        w["db"].commit()
    return s


def test_single_scan_has_result_fields(client, world):
    scan = _scan(
        world,
        details={"results": [1]},
        error="boom",
        started_at=datetime(2026, 1, 2, 3, 4, 5),
        completed_at=datetime(2026, 1, 2, 3, 5, 5),
    )
    body = client.get(f"/api/scans/{scan.id}", headers=world["h"]).json()
    assert body["id"] == str(scan.id)
    assert body["sketch_id"] == str(world["sketch"].id)
    assert body["status"] == "PENDING"
    assert body["details"] == {"results": [1]}
    assert body["error"] == "boom"


def test_naive_datetimes_serialised_as_utc(client, world):
    scan = _scan(
        world,
        started_at=datetime(2026, 1, 2, 3, 4, 5),
        completed_at=datetime(2026, 1, 2, 3, 5, 5),
    )
    body = client.get(f"/api/scans/{scan.id}", headers=world["h"]).json()
    assert datetime.fromisoformat(body["started_at"]) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )
    assert body["started_at"].endswith("+00:00") or body["started_at"].endswith("Z")
    assert body["completed_at"].endswith("+00:00") or body["completed_at"].endswith("Z")


def test_aware_datetime_keeps_its_instant(client, world):
    from app.api.schemas.scan import ScanDetail

    tz = timezone(timedelta(hours=2))
    out = ScanDetail(
        id=uuid.uuid4(),
        sketch_id=None,
        status="x",
        started_at=datetime(2026, 1, 2, 5, 0, tzinfo=tz),
    ).model_dump(mode="json")
    assert datetime.fromisoformat(out["started_at"]) == datetime(
        2026, 1, 2, 3, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("details", [None, ["legacy", 1], "text", 3, {"a": {"b": 1}}])
def test_any_details_shape_and_null_dates(client, world, details):
    scan = _scan(world, details=details, started_at=None)
    res = client.get(f"/api/scans/{scan.id}", headers=world["h"])
    assert res.status_code == 200
    body = res.json()
    assert body["details"] == details
    assert body["started_at"] is None
    assert body["completed_at"] is None
    assert body["error"] is None


def test_permission_denied_without_details(client, world):
    scan = _scan(world, details={"secret": 1})
    res = client.get(f"/api/scans/{scan.id}", headers=world["other_h"])
    assert res.status_code == 403
    assert "secret" not in res.text
    assert (
        client.get(f"/api/scans/{uuid.uuid4()}", headers=world["h"]).status_code == 404
    )


def test_sketch_list_is_light_and_ordered(client, world):
    late = _scan(world, details={"x": 1}, started_at=datetime(2026, 1, 3))
    early = _scan(world, details={"x": 2}, started_at=datetime(2026, 1, 1))
    none_a = _scan(world, started_at=None)
    none_b = _scan(world, started_at=None)
    # equal timestamps are ordered by id
    t1 = _scan(world, started_at=datetime(2026, 1, 2))
    t2 = _scan(world, started_at=datetime(2026, 1, 2))
    res = client.get(f"/api/scans/sketch/{world['sketch'].id}", headers=world["h"])
    assert res.status_code == 200
    rows = res.json()
    for r in rows:
        assert set(r) == {"id", "sketch_id", "status", "started_at", "completed_at"}
    ids = [r["id"] for r in rows]
    tied = sorted([str(t1.id), str(t2.id)], key=lambda s: uuid.UUID(s).bytes)
    nones = sorted([str(none_a.id), str(none_b.id)], key=lambda s: uuid.UUID(s).bytes)
    assert ids == [str(early.id), *tied, str(late.id), *nones]
    assert rows[0]["started_at"].endswith(("+00:00", "Z"))
