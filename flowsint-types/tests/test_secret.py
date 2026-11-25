from flowsint_types import Secret, get_model_for_type
from flowsint_types.registry import get_type


def test_secret_registered():
    assert get_type("Secret", case_sensitive=True) is Secret
    assert get_model_for_type("secret") is Secret


def test_secret_primary_field():
    assert Secret.model_fields["fingerprint"].json_schema_extra == {"primary": True}


def _finding(**overrides):
    base = dict(
        detector="AWS",
        verified=True,
        redacted="AKIA...XYZ",
        source_url="https://github.com/acme/app",
        commit="abc123",
        file="cfg.py",
        line=10,
    )
    base.update(overrides)
    return Secret(**base)


def test_fingerprint_is_computed_and_deterministic():
    a = _finding()
    b = _finding()
    assert a.fingerprint
    assert len(a.fingerprint) == 32
    assert a.fingerprint == b.fingerprint


def test_distinct_findings_get_distinct_fingerprints():
    a = _finding()
    b = _finding(line=99, redacted="AKIA...OTHER")
    assert a.fingerprint != b.fingerprint


def test_explicit_fingerprint_is_preserved():
    assert Secret(fingerprint="fixed-id", detector="X").fingerprint == "fixed-id"


def test_label_reflects_verification_state():
    assert _finding(verified=True).nodeLabel == "AWS (verified)"
    assert _finding(verified=False).nodeLabel == "AWS (unverified)"
    assert _finding(verified=None).nodeLabel == "AWS (unknown)"


def test_label_defaults_when_no_detector():
    s = Secret(verified=True, redacted="x")
    assert s.nodeLabel == "secret (verified)"
