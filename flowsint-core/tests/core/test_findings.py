from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.findings import Finding

pytestmark = pytest.mark.asyncio

FINDING = Finding(
    subject="A",
    relation="administra",
    object="B",
    source="fake_enricher",
    certainty="probable",
    confidence=0.8,
    evidence=["doc 1"],
)


def make_enricher(
    scan_raises=None,
    scan_returns=None,
    scan_adds=(),
    inner_failure=None,
    params_schema=None,
    params=None,
    vault=None,
):
    class FakeEnricher(Enricher):
        @classmethod
        def name(cls):
            return "fake_enricher"

        @classmethod
        def category(cls):
            return "Test"

        @classmethod
        def key(cls):
            return "value"

        async def scan(self, values):
            for f in scan_adds:
                self.add_finding(f)
            if inner_failure:
                self.add_failure(inner_failure)
            if scan_raises:
                raise scan_raises
            return scan_returns if scan_returns is not None else []

    return FakeEnricher(
        graph_service=MagicMock(),
        params_schema=params_schema,
        params=params,
        vault=vault,
    )


async def test_scan_exception_is_recorded():
    e = make_enricher(scan_raises=RuntimeError("timeout"))
    assert await e.execute(["x"]) == []
    assert e.source_run().status == "failed"
    assert "timeout" in e.source_run().error


async def test_empty_result_is_ok():
    e = make_enricher(scan_returns=[])
    await e.execute(["x"])
    run = e.source_run()
    assert run.status == "ok" and run.findings == 0
    assert run.source == "fake_enricher" and run.error is None


async def test_inner_failure_with_findings_is_failed_and_keeps_findings():
    e = make_enricher(scan_adds=[FINDING], inner_failure="BDNS 503")
    await e.execute(["x"])
    assert e.source_run().status == "failed" and e.findings == [FINDING]
    assert e.source_run().findings == 1


async def test_lists_cleared_on_each_execute():
    e = make_enricher(scan_adds=[FINDING], inner_failure="boom")
    await e.execute(["x"])
    await e.execute(["x"])
    assert len(e.findings) == 1 and e.failures == ["boom"]


async def test_error_truncated_to_300_chars():
    e = make_enricher(scan_raises=RuntimeError("x" * 1000))
    await e.execute(["x"])
    assert len(e.source_run().error) == 300


async def test_coverage_is_exposed():
    e = make_enricher()
    await e.execute(["x"])
    e.coverage = "solo 2024"
    assert e.source_run().coverage == "solo 2024"


async def test_secret_values_redacted_from_error():
    vault = MagicMock()
    vault.get_secret.return_value = "s3cr3t-key"
    e = make_enricher(
        scan_raises=RuntimeError("GET https://api.test/?key=s3cr3t-key failed"),
        params_schema=[{"name": "API_KEY", "type": "vaultSecret", "required": True}],
        vault=vault,
    )
    await e.execute(["x"])
    run = e.source_run()
    assert run.status == "failed"
    assert "s3cr3t-key" not in run.error
    assert "failed" in run.error


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Finding(**{**FINDING.model_dump(), "confidence": 1.2})
