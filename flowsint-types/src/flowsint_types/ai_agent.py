from typing import List, Optional, Self

from pydantic import Field, model_validator

from .flowsint_base import FlowsintType
from .registry import flowsint_type


@flowsint_type
class AiAgent(FlowsintType):
    """Represents an exposed/discoverable AI agent or chatbot.

    Covers Microsoft Copilot Studio bots, OpenAI Agent Builder deployments,
    custom GPTs and similar hosted conversational agents found during recon.
    The ``url`` is the externally reachable endpoint and acts as the unique key.
    """

    url: str = Field(
        ...,
        description="Externally reachable URL/endpoint of the agent",
        title="Agent URL",
        json_schema_extra={"primary": True},
    )
    name: Optional[str] = Field(
        None, description="Agent/bot display name", title="Name"
    )
    platform: Optional[str] = Field(
        None,
        description="Hosting platform (e.g. Copilot Studio, Agent Builder, Custom GPT)",
        title="Platform",
    )
    tenant_id: Optional[str] = Field(
        None, description="Power Platform / tenant identifier", title="Tenant ID"
    )
    environment_id: Optional[str] = Field(
        None, description="Power Platform environment identifier", title="Environment ID"
    )
    is_open: Optional[bool] = Field(
        None,
        description="Whether the agent is reachable without authentication",
        title="Is Open",
    )
    knowledge_sources: Optional[List[str]] = Field(
        None,
        description="Knowledge sources attached to the agent",
        title="Knowledge Sources",
    )
    tools: Optional[List[str]] = Field(
        None,
        description="Tools / actions / integrations connected to the agent",
        title="Tools",
    )
    source: Optional[str] = Field(
        None,
        description="Tool or method that discovered the agent (e.g. powerpwn)",
        title="Source",
    )

    @model_validator(mode="after")
    def compute_label(self) -> Self:
        self.nodeLabel = self.name or self.url
        return self

    @classmethod
    def from_string(cls, line: str):
        """Parse an AI agent from a raw URL string."""
        return cls(url=line.strip())

    @classmethod
    def detect(cls, line: str) -> bool:
        """AiAgent cannot be reliably detected from a single line of text."""
        return False
