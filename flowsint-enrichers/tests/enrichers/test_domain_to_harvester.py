import json

from flowsint_enrichers import ENRICHER_REGISTRY
from flowsint_enrichers.domain.to_harvester import DomainToHarvesterEnricher
from flowsint_types import Email


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
def test_domain_to_harvester_is_registered():
    enricher = ENRICHER_REGISTRY.get_enricher("domain_to_harvester", "123", "123")
    assert enricher.name() == "domain_to_harvester"


def test_domain_to_harvester_metadata():
    assert DomainToHarvesterEnricher.category() == "Domain"
    assert DomainToHarvesterEnricher.key() == "domain"
    assert DomainToHarvesterEnricher.input_schema()["type"] == "Domain"
    assert DomainToHarvesterEnricher.output_schema()["type"] == "Email"


def test_domain_to_harvester_exposes_sources_param():
    schema = DomainToHarvesterEnricher.get_params_schema()
    names = {p["name"] for p in schema}
    assert "sources" in names


# ---------------------------------------------------------------------------
# _parse_harvester_json (no network / no subprocess)
# ---------------------------------------------------------------------------
def test_parse_extracts_emails():
    raw = json.dumps(
        {
            "emails": ["john@example.com", "jane@example.com"],
            "hosts": ["www.example.com"],
            "ips": ["93.184.216.34"],
        }
    )
    emails = DomainToHarvesterEnricher._parse_harvester_json(raw)
    assert sorted(e.email for e in emails) == ["jane@example.com", "john@example.com"]
    assert all(isinstance(e, Email) for e in emails)


def test_parse_dedupes_case_insensitively():
    raw = json.dumps({"emails": ["John@Example.com", "john@example.com"]})
    emails = DomainToHarvesterEnricher._parse_harvester_json(raw)
    assert [e.email for e in emails] == ["john@example.com"]


def test_parse_skips_malformed_addresses():
    raw = json.dumps({"emails": ["valid@example.com", "not-an-email", ""]})
    emails = DomainToHarvesterEnricher._parse_harvester_json(raw)
    assert [e.email for e in emails] == ["valid@example.com"]


def test_parse_handles_missing_emails_key_and_bad_json():
    assert DomainToHarvesterEnricher._parse_harvester_json("{}") == []
    assert DomainToHarvesterEnricher._parse_harvester_json("not json") == []
    assert DomainToHarvesterEnricher._parse_harvester_json(json.dumps({"hosts": []})) == []
