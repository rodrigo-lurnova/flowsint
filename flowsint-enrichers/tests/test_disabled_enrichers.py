import pytest

import flowsint_enrichers.registry as registry
from flowsint_enrichers.registry import ENRICHER_REGISTRY, load_all_enrichers

FAKE = "es_org_to_sentencias_cendoj"


class _FakeEnricher:
    @classmethod
    def name(cls):
        return FAKE


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolated registry state, restored afterwards."""
    monkeypatch.delenv("FLOWSINT_DISABLED_ENRICHERS", raising=False)
    load_all_enrichers()
    saved = dict(ENRICHER_REGISTRY._enrichers)
    saved_disabled = set(ENRICHER_REGISTRY._disabled)
    saved_loaded = registry._enrichers_loaded
    ENRICHER_REGISTRY._enrichers[FAKE] = _FakeEnricher
    registry._enrichers_loaded = False
    yield
    ENRICHER_REGISTRY._enrichers.clear()
    ENRICHER_REGISTRY._enrichers.update(saved)
    ENRICHER_REGISTRY._disabled.clear()
    ENRICHER_REGISTRY._disabled.update(saved_disabled)
    registry._enrichers_loaded = saved_loaded


def test_disabled_enrichers_are_removed(clean_registry, monkeypatch):
    monkeypatch.setenv("FLOWSINT_DISABLED_ENRICHERS", f"domain_to_whois, {FAKE}")
    load_all_enrichers()
    assert ENRICHER_REGISTRY.enricher_exists("domain_to_whois") is False
    assert ENRICHER_REGISTRY.enricher_exists(FAKE) is False
    assert "domain_to_whois" not in [e["name"] for e in ENRICHER_REGISTRY.list()]
    with pytest.raises(Exception, match="not found"):
        ENRICHER_REGISTRY.get_enricher("domain_to_whois", "s", "c")


def test_empty_variable_keeps_everything(clean_registry, monkeypatch):
    monkeypatch.setenv("FLOWSINT_DISABLED_ENRICHERS", " , ")
    load_all_enrichers()
    assert ENRICHER_REGISTRY.enricher_exists("domain_to_whois") is True
    assert ENRICHER_REGISTRY.enricher_exists(FAKE) is True


def test_unknown_name_fails_closed(clean_registry, monkeypatch):
    monkeypatch.setenv("FLOWSINT_DISABLED_ENRICHERS", "domain_to_whois,nope_a, nope_b")
    for _ in range(2):
        with pytest.raises(ValueError) as exc:
            load_all_enrichers()
        assert "nope_a" in str(exc.value) and "nope_b" in str(exc.value)
        assert "domain_to_whois" not in str(exc.value)
    assert registry._enrichers_loaded is False
    assert ENRICHER_REGISTRY.enricher_exists("domain_to_whois") is True


def test_disabled_name_stays_out_on_late_register(clean_registry, monkeypatch):
    monkeypatch.setenv("FLOWSINT_DISABLED_ENRICHERS", FAKE)
    load_all_enrichers()
    ENRICHER_REGISTRY.register(_FakeEnricher)
    assert ENRICHER_REGISTRY.enricher_exists(FAKE) is False
