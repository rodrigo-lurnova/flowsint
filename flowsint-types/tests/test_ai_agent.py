import pytest
from pydantic import ValidationError

from flowsint_types import AiAgent, get_model_for_type
from flowsint_types.registry import get_type


def test_ai_agent_registered():
    assert get_type("AiAgent", case_sensitive=True) is AiAgent
    assert get_model_for_type("aiagent") is AiAgent


def test_ai_agent_primary_field():
    assert AiAgent.model_fields["url"].json_schema_extra == {"primary": True}


def test_ai_agent_requires_url():
    with pytest.raises(ValidationError):
        AiAgent()


def test_ai_agent_label_prefers_name():
    agent = AiAgent(url="https://host/bot", name="HR Bot", platform="Copilot Studio")
    assert agent.nodeLabel == "HR Bot"


def test_ai_agent_label_falls_back_to_url():
    agent = AiAgent(url="https://host/bot")
    assert agent.nodeLabel == "https://host/bot"


def test_ai_agent_optional_fields_default_none():
    agent = AiAgent(url="https://host/bot")
    assert agent.tenant_id is None
    assert agent.knowledge_sources is None
    assert agent.is_open is None
