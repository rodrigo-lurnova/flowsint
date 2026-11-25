import hashlib
from typing import Optional, Self

from pydantic import Field, model_validator

from .flowsint_base import FlowsintType
from .registry import flowsint_type


@flowsint_type
class Secret(FlowsintType):
    """Represents a secret/credential leaked in source code or other artifacts.

    Produced by secret scanners (e.g. trufflehog). The ``fingerprint`` is a
    stable hash derived from the detector plus the leak location so the same
    finding merges to one node and distinct findings stay separate. The plain
    secret is intentionally stored ``redacted``; ``raw`` is optional and only
    populated when explicitly captured.
    """

    fingerprint: Optional[str] = Field(
        None,
        description="Stable unique identifier for this finding",
        title="Fingerprint",
        json_schema_extra={"primary": True},
    )
    detector: Optional[str] = Field(
        None,
        description="Detector/type of the secret (e.g. AWS, GitHub, Slack)",
        title="Detector",
    )
    verified: Optional[bool] = Field(
        None,
        description="Whether the secret was actively verified as live",
        title="Verified",
    )
    redacted: Optional[str] = Field(
        None, description="Masked/redacted form of the secret", title="Redacted"
    )
    raw: Optional[str] = Field(
        None,
        description="Raw secret value (only if explicitly captured)",
        title="Raw",
    )
    source_url: Optional[str] = Field(
        None, description="Repository or source where the secret was found", title="Source URL"
    )
    commit: Optional[str] = Field(
        None, description="Commit hash where the secret appears", title="Commit"
    )
    file: Optional[str] = Field(
        None, description="File path within the source", title="File"
    )
    line: Optional[int] = Field(
        None, description="Line number of the finding", title="Line"
    )
    email: Optional[str] = Field(
        None, description="Committer email associated with the finding", title="Email"
    )
    timestamp: Optional[str] = Field(
        None, description="Commit timestamp of the finding", title="Timestamp"
    )
    source: Optional[str] = Field(
        None, description="Tool that produced the finding (e.g. trufflehog)", title="Source"
    )

    @model_validator(mode="after")
    def compute_fingerprint_and_label(self) -> Self:
        if not self.fingerprint:
            basis = "|".join(
                str(part)
                for part in (
                    self.detector,
                    self.redacted or self.raw,
                    self.source_url,
                    self.commit,
                    self.file,
                    self.line,
                )
            )
            self.fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

        if not self.nodeLabel:
            state = (
                "verified"
                if self.verified
                else "unverified"
                if self.verified is False
                else "unknown"
            )
            label = self.detector or "secret"
            self.nodeLabel = f"{label} ({state})"
        return self

    @classmethod
    def detect(cls, line: str) -> bool:
        """Secret cannot be reliably detected from a single line of text."""
        return False
