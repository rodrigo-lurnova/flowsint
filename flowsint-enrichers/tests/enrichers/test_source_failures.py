"""Las caídas de Gravatar e ip-api cuentan como fallo de la fuente."""

import asyncio
from unittest.mock import MagicMock

import pytest
import requests

from flowsint_enrichers.email.to_gravatar import EmailToGravatarEnricher
from flowsint_enrichers.ip.to_infos import IpToInfosEnricher


def response(status, data=None, broken=False):
    r = MagicMock(status_code=status)
    r.json.side_effect = ValueError("no es JSON") if broken else None
    r.json.return_value = data or {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return r


def run(enricher_class, values):
    e = enricher_class(sketch_id="s", graph_service=MagicMock())
    e.results = asyncio.run(e.execute(values))
    return e


def test_gravatar_connection_error_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        requests, "head", MagicMock(side_effect=requests.ConnectionError("caída"))
    )
    e = run(EmailToGravatarEnricher, ["ana@acme.example.com"])
    assert e.source_run().status == "failed"
    assert "ana@acme.example.com" in e.failures[0] and "caída" in e.failures[0]


@pytest.mark.parametrize("status, failures", [(404, 0), (429, 1), (503, 1)])
def test_gravatar_statuses(monkeypatch, status, failures):
    monkeypatch.setattr(requests, "head", lambda *a, **k: response(status))
    e = run(EmailToGravatarEnricher, ["ana@acme.example.com"])
    assert len(e.failures) == failures and e.results == []


def test_gravatar_unreadable_profile_is_a_failure(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda *a, **k: response(200))
    monkeypatch.setattr(requests, "get", lambda *a, **k: response(200, broken=True))
    assert len(run(EmailToGravatarEnricher, ["ana@acme.example.com"]).failures) == 1


@pytest.mark.parametrize(
    "answer, failures",
    [
        (response(200, {"status": "success", "city": "Madrid"}), 0),
        (response(200, {"status": "fail", "message": "quota"}), 1),
        (response(500), 1),
        (response(200, broken=True), 1),
    ],
)
def test_ip_infos_failures(monkeypatch, answer, failures):
    monkeypatch.setattr(requests, "get", lambda *a, **k: answer)
    e = run(IpToInfosEnricher, ["192.0.2.1"])
    assert len(e.failures) == failures
    assert all("192.0.2.1" in f for f in e.failures)


def test_ip_infos_unexpected_error_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        IpToInfosEnricher, "get_location_data", lambda self, a: [].pop()
    )
    assert len(run(IpToInfosEnricher, ["192.0.2.1"]).failures) == 1
