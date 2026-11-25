import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_core.core.vault import VaultProtocol
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types import Domain, Email

# Sources that work without an API key (not listed in theHarvester's
# data/api-keys.yaml for the 4.11.1 release). Kept conservative to avoid
# hammering sources that rate-limit hard.
DEFAULT_SOURCES = (
    "crtsh,certspotter,duckduckgo,otx,rapiddns,"
    "urlscan,hackertarget,threatcrowd,subdomaincenter"
)


@flowsint_enricher
class DomainToHarvesterEnricher(Enricher):
    """[theHarvester] Harvest email addresses for a domain from public sources.

    Existing domain enrichers resolve infrastructure (subdomains, IPs, WHOIS);
    none turn a domain into the *email addresses* exposed across search engines,
    certificate transparency and threat-intel feeds. theHarvester fills that gap.
    Subdomains it also finds are intentionally ignored here to avoid duplicating
    the subfinder-based ``domain_to_subdomains`` enricher.
    """

    InputType = Domain
    OutputType = Email

    _TIMEOUT = 300

    def __init__(
        self,
        sketch_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        vault: Optional[VaultProtocol] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        # get_enricher() does not pass params_schema, so wire it here (same as
        # the other parameterised enrichers) or a user-supplied `sources` value
        # would be rejected/dropped during param validation.
        super().__init__(
            sketch_id=sketch_id,
            scan_id=scan_id,
            params_schema=self.get_params_schema(),
            vault=vault,
            params=params,
        )

    @classmethod
    def name(cls) -> str:
        return "domain_to_harvester"

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
                "name": "sources",
                "type": "string",
                "description": (
                    "Comma-separated theHarvester sources to query "
                    "(-b). Default uses only keyless/free sources."
                ),
                "required": False,
                "default": DEFAULT_SOURCES,
            }
        ]

    def run_harvester(self, domain: str, sources: str) -> Path:
        """Run the theHarvester CLI and return the path to its JSON report."""
        tmp_dir = Path(tempfile.gettempdir())
        # theHarvester appends .json/.xml to the -f prefix.
        out_prefix = tmp_dir / f"theharvester_{domain}"
        json_path = out_prefix.with_suffix(".json")

        # Remove a stale report so a failed run can't be parsed as success.
        try:
            json_path.unlink(missing_ok=True)
        except Exception:
            pass

        try:
            subprocess.run(
                [
                    "theHarvester",
                    "-d",
                    domain,
                    "-b",
                    sources,
                    "-f",
                    str(out_prefix),
                ],
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT,
            )
        except Exception as e:
            Logger.error(
                self.sketch_id,
                {"message": f"(theHarvester) execution failed for {domain}: {e}"},
            )
        return json_path

    @staticmethod
    def _parse_harvester_json(raw: str) -> List[Email]:
        """Extract valid Email entities from a theHarvester JSON report string.

        Pure and side-effect free for unit testing. Malformed addresses that
        fail Email validation are silently skipped rather than aborting.
        """
        results: List[Email] = []
        try:
            data = json.loads(raw)
        except Exception:
            return results

        seen = set()
        for raw_email in data.get("emails", []) or []:
            if not isinstance(raw_email, str):
                continue
            value = raw_email.strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            try:
                results.append(Email(email=value))
            except Exception:
                # theHarvester occasionally emits truncated / non-RFC strings.
                continue
        return results

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        sources = self.params.get("sources") or DEFAULT_SOURCES
        results: List[OutputType] = []
        # Keep (source domain, email) pairs for postprocess linkage.
        self._pairs: List[tuple] = []

        for domain_obj in data:
            domain_value = domain_obj.domain
            if not domain_value:
                continue
            try:
                json_path = self.run_harvester(domain_value, sources)
                if not json_path.exists():
                    Logger.warn(
                        self.sketch_id,
                        {
                            "message": f"(theHarvester) no report produced for "
                            f"{domain_value}"
                        },
                    )
                    continue
                raw = json_path.read_text(encoding="utf-8", errors="ignore")
                emails = self._parse_harvester_json(raw)
                for email_obj in emails:
                    self._pairs.append((domain_obj, email_obj))
                results.extend(emails)
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {
                        "message": f"(theHarvester) failed to process "
                        f"{domain_value}: {e}"
                    },
                )
                continue
        return results

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType]
    ) -> List[OutputType]:
        if not self._graph_service:
            return results

        for source_domain, email_obj in getattr(self, "_pairs", []):
            try:
                self.create_node(source_domain)
                self.create_node(email_obj)
                self.create_relationship(source_domain, email_obj, "HAS_EMAIL")
                self.log_graph_message(
                    f"(theHarvester) {source_domain.domain} -> {email_obj.email}"
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(theHarvester) failed to create graph nodes: {e}"},
                )
                continue
        return results


# Make types available at module level for easy access
InputType = DomainToHarvesterEnricher.InputType
OutputType = DomainToHarvesterEnricher.OutputType
