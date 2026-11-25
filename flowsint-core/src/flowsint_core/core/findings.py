from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Certainty = Literal["confirmada", "probable", "posible"]


class Finding(BaseModel):
    """A relation between two subjects found by a source, with its certainty."""

    subject: str
    relation: str
    object: str
    source: str
    certainty: Certainty
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    evidence: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class SourceRun(BaseModel):
    """Outcome of one source lookup (one enricher execution)."""

    source: str
    status: Literal["ok", "failed", "skipped"]
    error: Optional[str] = None
    coverage: Optional[str] = None
    findings: int = 0


Outcome = Literal["complete", "partial", "failed"]


def compute_outcome(runs: List[SourceRun]) -> Outcome:
    """Aggregate source runs. Skipped runs never count as failures."""
    ok = [r for r in runs if r.status == "ok"]
    failed = [r for r in runs if r.status == "failed"]
    if ok:
        return "partial" if failed else "complete"
    if any(r.findings > 0 for r in failed):
        return "partial"
    return "failed"


def dedupe_findings(findings: List[Finding]) -> List[Finding]:
    """Drop duplicates (all fields but confidence), keeping the highest confidence."""
    best: Dict[str, Finding] = {}
    for f in findings:
        key = f.model_dump_json(exclude={"confidence"})
        current = best.get(key)
        if current is None:
            best[key] = f
        elif (f.confidence if f.confidence is not None else -1) > (
            current.confidence if current.confidence is not None else -1
        ):
            best[key] = f
    return list(best.values())
