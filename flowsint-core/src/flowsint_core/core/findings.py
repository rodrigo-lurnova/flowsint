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


MAX_MESSAGE = 300
MAX_MESSAGES = 50


def bounded(messages: List[str]) -> List[str]:
    """Cap each message, and keep the first ones plus a final "y N más"."""
    kept = [m[:MAX_MESSAGE] for m in messages[:MAX_MESSAGES]]
    rest = len(messages) - len(kept)
    return [*kept, f"y {rest} más"] if rest > 0 else kept


class SourceRun(BaseModel):
    """Outcome of one source lookup (one enricher execution)."""

    source: str
    status: Literal["ok", "failed", "skipped"]
    error: Optional[str] = None
    # Every failure, and every notice that does not make the run fail.
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    coverage: Optional[str] = None
    findings: int = 0

    def with_failure(self, message: str) -> "SourceRun":
        """Copy of this run, marked as failed, with one more error."""
        return self.model_copy(
            update={
                "status": "failed",
                "error": "; ".join(x for x in (self.error, message) if x)[:MAX_MESSAGE],
                "errors": bounded([*self.errors, message]),
            }
        )


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
