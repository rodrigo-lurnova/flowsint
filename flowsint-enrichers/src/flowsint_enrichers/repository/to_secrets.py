import json
from typing import Any, Dict, List, Optional

from tools.security.trufflehog import TrufflehogTool

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_core.core.vault import VaultProtocol
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types import Repository, Secret


def _mask(value: Optional[str]) -> Optional[str]:
    """Mask a raw secret so only its shape leaks into the graph."""
    if not value:
        return None
    value = value.strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


@flowsint_enricher
class RepositoryToSecretsEnricher(Enricher):
    """[Trufflehog] Scan a git repository for leaked, optionally verified secrets.

    Runs trufflehog (official Docker image, Docker-in-Docker) against a repo URL
    and turns each finding into a Secret node. Verified findings are credentials
    trufflehog actively validated against the live service — information no other
    enricher surfaces. Raw secret values are NOT stored by default; set
    ``capture_raw`` to opt in.
    """

    InputType = Repository
    OutputType = Secret

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
        return "repository_to_secrets"

    @classmethod
    def category(cls) -> str:
        return "Repository"

    @classmethod
    def key(cls) -> str:
        return "repository"

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "only_verified",
                "type": "select",
                "description": "Only keep secrets trufflehog actively verified as live.",
                "required": False,
                "default": "false",
                "options": [
                    {"label": "All findings", "value": "false"},
                    {"label": "Verified only", "value": "true"},
                ],
            },
            {
                "name": "capture_raw",
                "type": "select",
                "description": "Store the raw secret value (sensitive). Off by default.",
                "required": False,
                "default": "false",
                "options": [
                    {"label": "Redacted only", "value": "false"},
                    {"label": "Capture raw", "value": "true"},
                ],
            },
        ]

    @staticmethod
    def _extract_git_meta(finding: Dict[str, Any]) -> Dict[str, Any]:
        """Pull location fields out of SourceMetadata regardless of source kind.

        trufflehog nests location under SourceMetadata.Data.<Source> (e.g. Git,
        Github, Filesystem). We take the first sub-dict and read common keys.
        """
        data = (finding.get("SourceMetadata") or {}).get("Data") or {}
        for value in data.values():
            if isinstance(value, dict):
                return value
        return {}

    @classmethod
    def _parse_trufflehog_output(
        cls, raw: str, repo_url: str, capture_raw: bool = False
    ) -> List[Secret]:
        """Parse trufflehog JSONL (combined stdout/stderr) into Secret entities.

        Pure and defensive: non-JSON lines (banner/progress) and lines without a
        detector are skipped; every nested lookup tolerates missing keys.
        """
        results: List[Secret] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                finding = json.loads(line)
            except Exception:
                continue
            if not isinstance(finding, dict):
                continue

            detector = finding.get("DetectorName")
            raw_secret = finding.get("Raw") or finding.get("RawV2")
            # A real finding always carries a detector and a secret payload.
            if not detector and not raw_secret:
                continue

            redacted = finding.get("Redacted") or _mask(raw_secret)
            meta = cls._extract_git_meta(finding)
            line_no = meta.get("line")
            try:
                line_no = int(line_no) if line_no is not None else None
            except (TypeError, ValueError):
                line_no = None

            try:
                results.append(
                    Secret(
                        detector=detector,
                        verified=finding.get("Verified"),
                        redacted=redacted,
                        raw=raw_secret if capture_raw else None,
                        source_url=meta.get("repository") or repo_url,
                        commit=meta.get("commit"),
                        file=meta.get("file"),
                        line=line_no,
                        email=meta.get("email"),
                        timestamp=meta.get("timestamp"),
                        source="trufflehog",
                    )
                )
            except Exception:
                continue
        return results

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        only_verified = str(self.params.get("only_verified", "false")).lower() == "true"
        capture_raw = str(self.params.get("capture_raw", "false")).lower() == "true"

        results: List[OutputType] = []
        self._pairs: List[tuple] = []

        try:
            tool = TrufflehogTool()
        except Exception as e:
            Logger.error(
                self.sketch_id,
                {"message": f"(Trufflehog) Docker unavailable: {e}"},
            )
            return results

        for repo_obj in data:
            repo_url = repo_obj.url
            if not repo_url:
                continue
            try:
                raw = tool.scan_git(repo_url, only_verified=only_verified)
                secrets = self._parse_trufflehog_output(
                    raw, repo_url, capture_raw=capture_raw
                )
                for secret in secrets:
                    self._pairs.append((repo_obj, secret))
                results.extend(secrets)
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Trufflehog) failed to scan {repo_url}: {e}"},
                )
                continue
        return results

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType]
    ) -> List[OutputType]:
        if not self._graph_service:
            return results

        for source_repo, secret in getattr(self, "_pairs", []):
            try:
                self.create_node(source_repo)
                self.create_node(secret)
                self.create_relationship(source_repo, secret, "LEAKS_SECRET")
                self.log_graph_message(
                    f"(Trufflehog) {source_repo.url} -> {secret.detector} "
                    f"({'verified' if secret.verified else 'unverified'})"
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Trufflehog) failed to create graph nodes: {e}"},
                )
                continue
        return results


# Make types available at module level for easy access
InputType = RepositoryToSecretsEnricher.InputType
OutputType = RepositoryToSecretsEnricher.OutputType
