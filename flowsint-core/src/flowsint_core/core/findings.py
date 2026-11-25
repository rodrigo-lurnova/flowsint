from typing import List, Literal, Optional

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
