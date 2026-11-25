from flowsint_enrichers import ENRICHER_REGISTRY
from flowsint_enrichers.domain.to_copilot_agents import DomainToCopilotAgentsEnricher
from flowsint_types import AiAgent

OPEN = (
    "https://copilotstudio.microsoft.com/environments/"
    "Default-11111111-2222-3333-4444-555555555555/bots/cr1a2_hrAssistant/canvas?__version__=2"
)
EXISTING = (
    "https://copilotstudio.microsoft.com/environments/"
    "Default-11111111-2222-3333-4444-555555555555/bots/cr1a2_internalBot/canvas?__version__=2"
)


def test_domain_to_copilot_agents_is_registered():
    enricher = ENRICHER_REGISTRY.get_enricher("domain_to_copilot_agents", "1", "1")
    assert enricher.name() == "domain_to_copilot_agents"


def test_metadata():
    assert DomainToCopilotAgentsEnricher.category() == "Domain"
    assert DomainToCopilotAgentsEnricher.key() == "domain"
    assert DomainToCopilotAgentsEnricher.input_schema()["type"] == "Domain"
    assert DomainToCopilotAgentsEnricher.output_schema()["type"] == "AiAgent"


def test_parse_extracts_env_and_name_and_marks_open():
    (agent,) = DomainToCopilotAgentsEnricher._parse_bot_urls([OPEN])
    assert isinstance(agent, AiAgent)
    assert agent.is_open is True
    assert agent.platform == "Copilot Studio"
    assert agent.environment_id == "Default-11111111-2222-3333-4444-555555555555"
    assert agent.name == "cr1a2_hrAssistant"
    assert agent.source == "powerpwn"


def test_existing_only_is_not_open():
    agents = DomainToCopilotAgentsEnricher._parse_bot_urls([], [EXISTING])
    assert len(agents) == 1
    assert agents[0].is_open is False


def test_open_hit_upgrades_existing_duplicate():
    agents = DomainToCopilotAgentsEnricher._parse_bot_urls([OPEN], [OPEN, EXISTING])
    by_url = {a.url: a for a in agents}
    assert len(by_url) == 2
    assert by_url[OPEN].is_open is True


def test_parse_skips_blank_and_nonhttp():
    assert DomainToCopilotAgentsEnricher._parse_bot_urls(["", "   ", "ftp://x/bot"]) == []


def test_params_schema_names():
    names = {p["name"] for p in DomainToCopilotAgentsEnricher.get_params_schema()}
    assert {"rate", "threads", "proxy"} <= names
