import re
from typing import Optional, Self

from pydantic import Field, model_validator

from .flowsint_base import FlowsintType
from .registry import flowsint_type


@flowsint_type
class Repository(FlowsintType):
    """Represents a source-code repository (git) that can be scanned.

    Used as the input entity for secret-scanning enrichers. The clone/web URL
    uniquely identifies the repository.
    """

    url: str = Field(
        ...,
        description="Repository URL (clone or web URL)",
        title="Repository URL",
        json_schema_extra={"primary": True},
    )
    name: Optional[str] = Field(
        None, description="Repository name (owner/repo)", title="Name"
    )
    platform: Optional[str] = Field(
        None,
        description="Hosting platform (e.g. github, gitlab, bitbucket)",
        title="Platform",
    )
    visibility: Optional[str] = Field(
        None, description="public or private", title="Visibility"
    )
    default_branch: Optional[str] = Field(
        None, description="Default branch name", title="Default Branch"
    )
    source: Optional[str] = Field(
        None, description="Tool or method that discovered the repository", title="Source"
    )

    @model_validator(mode="after")
    def compute_label(self) -> Self:
        self.nodeLabel = self.name or self.url
        return self

    @classmethod
    def from_string(cls, line: str):
        """Parse a repository from a raw URL string."""
        return cls(url=line.strip())

    @classmethod
    def detect(cls, line: str) -> bool:
        """Detect common git hosting URLs."""
        line = line.strip()
        if not line:
            return False
        return bool(
            re.match(
                r"^(https?://|git@)[^\s]*(github\.com|gitlab\.com|bitbucket\.org)[^\s]+",
                line,
                re.IGNORECASE,
            )
            or line.endswith(".git")
        )
