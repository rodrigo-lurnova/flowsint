from flowsint_enrichers import ENRICHER_REGISTRY
from flowsint_enrichers.repository.to_secrets import RepositoryToSecretsEnricher
from flowsint_types import Secret

# Verbatim JSONL line from the trufflehog README (`git ... --json` over test_keys).
REAL_FINDING = (
    '{"SourceMetadata":{"Data":{"Git":{"commit":"fbc14303ffbf8fb1c2c1914e8dda7d0121633aca",'
    '"file":"keys","email":"counter <counter@counters-MacBook-Air.local>",'
    '"repository":"https://github.com/trufflesecurity/test_keys",'
    '"timestamp":"2022-06-16 10:17:40 -0700 PDT","line":4}}},'
    '"SourceID":0,"SourceType":16,"SourceName":"trufflehog - git",'
    '"DetectorType":2,"DetectorName":"AWS","DecoderName":"PLAIN","Verified":true,'
    '"Raw":"AKIAYVP4CIPPERUVIFXG","Redacted":"AKIAYVP4CIPPERUVIFXG",'
    '"ExtraData":{"account":"595918472158"},"StructuredData":null}'
)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
def test_repository_to_secrets_is_registered():
    enricher = ENRICHER_REGISTRY.get_enricher("repository_to_secrets", "123", "123")
    assert enricher.name() == "repository_to_secrets"


def test_repository_to_secrets_metadata():
    assert RepositoryToSecretsEnricher.category() == "Repository"
    assert RepositoryToSecretsEnricher.key() == "repository"
    assert RepositoryToSecretsEnricher.input_schema()["type"] == "Repository"
    assert RepositoryToSecretsEnricher.output_schema()["type"] == "Secret"


def test_params_schema_exposes_toggles():
    names = {p["name"] for p in RepositoryToSecretsEnricher.get_params_schema()}
    assert {"only_verified", "capture_raw"} <= names


# ---------------------------------------------------------------------------
# _parse_trufflehog_output (no Docker)
# ---------------------------------------------------------------------------
def test_parse_real_finding():
    out = "\n".join(
        [
            "🐷🔑🐷  TruffleHog. Unearth your secrets. 🐷🔑🐷",  # stderr banner
            REAL_FINDING,
            "2024-01-01 info scanning complete",  # non-json log line
        ]
    )
    secrets = RepositoryToSecretsEnricher._parse_trufflehog_output(
        out, "https://github.com/trufflesecurity/test_keys"
    )
    assert len(secrets) == 1
    s = secrets[0]
    assert isinstance(s, Secret)
    assert s.detector == "AWS"
    assert s.verified is True
    assert s.redacted == "AKIAYVP4CIPPERUVIFXG"
    assert s.source_url == "https://github.com/trufflesecurity/test_keys"
    assert s.commit == "fbc14303ffbf8fb1c2c1914e8dda7d0121633aca"
    assert s.file == "keys"
    assert s.line == 4
    assert s.source == "trufflehog"
    assert s.fingerprint  # computed


def test_raw_not_stored_by_default():
    (s,) = RepositoryToSecretsEnricher._parse_trufflehog_output(REAL_FINDING, "x")
    assert s.raw is None


def test_raw_captured_when_opted_in():
    (s,) = RepositoryToSecretsEnricher._parse_trufflehog_output(
        REAL_FINDING, "x", capture_raw=True
    )
    assert s.raw == "AKIAYVP4CIPPERUVIFXG"


def test_redacted_falls_back_to_masked_raw():
    line = '{"DetectorName":"Generic","Verified":false,"Raw":"supersecretvalue12345"}'
    (s,) = RepositoryToSecretsEnricher._parse_trufflehog_output(line, "x")
    assert s.redacted == "supe******2345"
    assert s.raw is None


def test_parse_skips_noise_and_bad_json():
    out = "\n".join(["", "not json", "{bad", '{"foo":"bar"}'])
    # the only dict has neither detector nor raw -> dropped
    assert RepositoryToSecretsEnricher._parse_trufflehog_output(out, "x") == []
