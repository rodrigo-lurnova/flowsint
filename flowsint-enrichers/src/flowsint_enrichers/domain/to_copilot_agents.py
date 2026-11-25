import re
from typing import Any, Dict, List, Optional

from tools.security.powerpwn import PowerpwnTool

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_core.core.vault import VaultProtocol
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types import AiAgent, Domain

_ENV_RE = re.compile(r"/environments/([^/]+)/", re.IGNORECASE)
_BOT_RE = re.compile(r"/bots/([^/?]+?)(?:/canvas|/|$)", re.IGNORECASE)


@flowsint_enricher
class DomainToCopilotAgentsEnricher(Enricher):
    """[Powerpwn] Discover exposed Microsoft Copilot Studio bots for a domain.

    Runs power-pwn's Copilot Studio Hunter ``deep-scan`` (Docker-in-Docker),
    which resolves the domain's tenant, fuzzes the Power Platform API and
    reports Copilot Studio bots whose web chat is openly reachable — a class of
    exposed AI asset no other enricher surfaces.

    Requires a powerpwn Docker image (no official one is published; build it from
    power-pwn's Dockerfile and set the ``POWERPWN_IMAGE`` env var). See
    tools/security/powerpwn.py for why it can't be a pip dependency.
    """

    InputType = Domain
    OutputType = AiAgent

    def __init__(
        self,
        sketch_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        vault: Optional[VaultProtocol] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            sketch_id=sketch_id,
            scan_id=scan_id,
            params_schema=self.get_params_schema(),
            vault=vault,
            params=params,
        )

    @classmethod
    def name(cls) -> str:
        return "domain_to_copilot_agents"

    @classmethod
    def category(cls) -> str:
        return "Domain"

    @classmethod
    def key(cls) -> str:
        return "domain"

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "rate",
                "type": "number",
                "description": "ffuf rate limit (requests/second). 0 = unlimited.",
                "required": False,
                "default": "0",
            },
            {
                "name": "threads",
                "type": "number",
                "description": "ffuf concurrent threads.",
                "required": False,
                "default": "10",
            },
            {
                "name": "proxy",
                "type": "string",
                "description": "Proxy URL for ffuf (e.g. http://127.0.0.1:8080).",
                "required": False,
            },
        ]

    @staticmethod
    def _parse_bot_urls(
        open_urls: List[str], existing_urls: Optional[List[str]] = None
    ) -> List[AiAgent]:
        """Build AiAgent entities from discovered Copilot Studio bot URLs.

        ``open_urls`` are bots with a live/accessible web chat (is_open=True);
        ``existing_urls`` are bots discovered but not confirmed open. Pure and
        side-effect free for unit testing. URLs carry the environment id and bot
        name, which are extracted here.
        """
        existing_urls = existing_urls or []
        seen: Dict[str, AiAgent] = {}

        def add(url: str, is_open: bool) -> None:
            url = url.strip()
            if not url or not url.lower().startswith("http"):
                return
            if url in seen:
                # An open hit upgrades a previously-seen existing bot.
                if is_open:
                    seen[url].is_open = True
                return
            env_match = _ENV_RE.search(url)
            bot_match = _BOT_RE.search(url)
            try:
                seen[url] = AiAgent(
                    url=url,
                    name=bot_match.group(1) if bot_match else None,
                    platform="Copilot Studio",
                    environment_id=env_match.group(1) if env_match else None,
                    is_open=is_open,
                    source="powerpwn",
                )
            except Exception:
                return

        for u in open_urls:
            add(u, True)
        for u in existing_urls:
            add(u, False)
        return list(seen.values())

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        rate = self.params.get("rate")
        threads = self.params.get("threads")
        proxy = self.params.get("proxy")

        def _to_int(v: Any) -> Optional[int]:
            try:
                return int(v) if v is not None and str(v) != "" else None
            except (TypeError, ValueError):
                return None

        results: List[OutputType] = []
        self._pairs: List[tuple] = []

        try:
            tool = PowerpwnTool()
        except Exception as e:
            Logger.error(
                self.sketch_id,
                {"message": f"(Powerpwn) Docker unavailable: {e}"},
            )
            return results

        for domain_obj in data:
            domain_value = domain_obj.domain
            if not domain_value:
                continue
            try:
                # The result file is copied out of the container over the Docker
                # API, so there is no host bind-mount to translate or clean up.
                open_urls = tool.copilot_deep_scan(
                    domain_value,
                    rate=_to_int(rate),
                    threads=_to_int(threads),
                    proxy=proxy or None,
                )
                agents = self._parse_bot_urls(open_urls)
                for agent in agents:
                    self._pairs.append((domain_obj, agent))
                results.extend(agents)
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Powerpwn) deep-scan failed for {domain_value}: {e}"},
                )
                continue
        return results

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType]
    ) -> List[OutputType]:
        if not self._graph_service:
            return results

        for source_domain, agent in getattr(self, "_pairs", []):
            try:
                self.create_node(source_domain)
                self.create_node(agent)
                self.create_relationship(source_domain, agent, "EXPOSES_AGENT")
                self.log_graph_message(
                    f"(Powerpwn) {source_domain.domain} -> Copilot bot {agent.name}"
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Powerpwn) failed to create graph nodes: {e}"},
                )
                continue
        return results


# Make types available at module level for easy access
InputType = DomainToCopilotAgentsEnricher.InputType
OutputType = DomainToCopilotAgentsEnricher.OutputType
