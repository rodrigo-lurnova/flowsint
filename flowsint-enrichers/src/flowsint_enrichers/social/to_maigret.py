import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types import Username
from flowsint_types.social_account import SocialAccount

# fixya and Pling claim any username, even nonexistent ones
false_positives = ["LeagueOfLegends", "fixya", "Pling"]


@flowsint_enricher
class MaigretEnricher(Enricher):
    """[MAIGRET] Scans usernames for associated social accounts using Maigret."""

    # Define types as class attributes - base class handles schema generation automatically
    InputType = Username
    OutputType = SocialAccount

    def __init__(
        self,
        sketch_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        vault=None,
        params: Optional[Dict[str, Any]] = None,
        graph_service=None,
    ):
        super().__init__(
            sketch_id=sketch_id,
            scan_id=scan_id,
            params_schema=self.get_params_schema(),
            vault=vault,
            params=params,
            graph_service=graph_service,
        )

    @classmethod
    def name(cls) -> str:
        return "username_to_socials_maigret"

    @classmethod
    def category(cls) -> str:
        return "social"

    @classmethod
    def key(cls) -> str:
        return "username"

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "top_sites",
                "type": "string",
                "default": "500",
                "required": False,
                "description": "Number of most popular sites to check, or 'all' (slow).",
            },
            {
                "name": "tags",
                "type": "string",
                "required": False,
                "description": "Only check sites with these tags, comma separated (e.g. 'es,social').",
            },
            {
                "name": "timeout",
                "type": "string",
                "default": "300",
                "required": False,
                "description": "Maximum seconds per username.",
            },
        ]

    def build_command(self, username: str, output_dir: str) -> List[str]:
        command = [
            "maigret",
            "-J",
            "simple",
            "-fo",
            output_dir,
            # Recursion searches other aliases found in profiles: slow, and
            # their reports would not belong to this username.
            "--no-recursion",
            "--no-progressbar",
            "--no-color",
        ]
        top_sites = str(self.params.get("top_sites") or "500").strip().lower()
        if top_sites == "all":
            command.append("--all-sites")
        else:
            command += ["--top-sites", str(int(top_sites))]
        if self.params.get("tags"):
            command.append(f"--tags={self.params['tags']}")
        # "--" keeps a username starting with "-" from being read as an option
        return command + ["--", username]

    def run_maigret(self, username: str, output_dir: str) -> Optional[Path]:
        timeout = int(self.params.get("timeout") or 300)
        try:
            process = subprocess.run(
                self.build_command(username, output_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            Logger.error(
                self.sketch_id,
                {"message": f"[MAIGRET] {username}: no result after {timeout}s"},
            )
            return None
        # Maigret sanitizes the username in the file name, so look it up.
        report = next(Path(output_dir).glob("report_*_simple.json"), None)
        if process.returncode != 0 or report is None:
            detail = (process.stderr or process.stdout or "").strip()[-300:]
            Logger.error(
                self.sketch_id,
                {
                    "message": f"[MAIGRET] {username}: failed (exit code {process.returncode}): {detail}"
                },
            )
            return None
        return report

    def parse_maigret_output(
        self, username_obj: Username, output_file: Path
    ) -> List[SocialAccount]:
        results: List[SocialAccount] = []
        if not output_file.exists():
            return results

        try:
            with open(output_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            Logger.error(
                self.sketch_id,
                {
                    "message": f"Failed to load output file for {username_obj.value}: {e}"
                },
            )
            return results

        for platform, profile in raw_data.items():
            if profile.get("status", {}).get("status") != "Claimed":
                continue

            if any(fp in platform for fp in false_positives):
                continue

            status = profile.get("status", {})
            ids = status.get("ids", {})
            profile_url = status.get("url") or profile.get("url_user")
            if not profile_url:
                continue

            # Search pages and URLs without the username do not prove an account
            if profile.get("is_similar"):
                continue
            if username_obj.value.lower() not in unquote(profile_url).lower():
                continue

            try:
                followers = (
                    int(ids.get("follower_count", 0))
                    if ids.get("follower_count")
                    else None
                )
                following = (
                    int(ids.get("following_count", 0))
                    if ids.get("following_count")
                    else None
                )
                posts = (
                    int(ids.get("public_repos_count", 0))
                    + int(ids.get("public_gists_count", 0))
                    if "public_repos_count" in ids or "public_gists_count" in ids
                    else None
                )
            except ValueError:
                followers = following = posts = None

            display_name = ids.get("fullname") or ids.get("nickname")
            if display_name:
                nodeLabel = f"{display_name} ({platform})"
            else:
                nodeLabel = None

            try:
                results.append(
                    SocialAccount(
                        nodeLabel=nodeLabel,
                        username=username_obj,
                        display_name=display_name,
                        profile_url=profile_url,
                        profile_picture_url=ids.get("image"),
                        bio=ids.get("bio"),
                        location=ids.get("location"),
                        platform=platform,
                        created_at=ids.get("created_at"),
                        followers_count=followers,
                        following_count=following,
                        posts_count=posts,
                    )
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {
                        "message": f"Failed to create SocialAccount for {username_obj.value} on {platform}: {e}"
                    },
                )
                continue

        return results

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        results: List[OutputType] = []
        for profile in data:
            if not profile.value:
                continue
            try:
                with tempfile.TemporaryDirectory() as output_dir:
                    report = self.run_maigret(profile.value, output_dir)
                    if report is None:
                        continue
                    results.extend(self.parse_maigret_output(profile, report))
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"Failed to process username {profile.value}: {e}"},
                )
                continue
        return results

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType]
    ) -> List[OutputType]:
        if not self._graph_service:
            return results

        for profile in results:
            try:
                # Create username node
                self.create_node(profile.username)
                # Create social profile node
                self.create_node(profile)
                # Create relationship
                self.create_relationship(
                    profile.username, profile, "HAS_SOCIAL_ACCOUNT"
                )
                self.log_graph_message(
                    f"{profile.username.value} -> account found on {profile.platform}"
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {
                        "message": f"Failed to create graph nodes for {profile.username.value} on {profile.platform}: {e}"
                    },
                )
                continue
        return results


InputType = MaigretEnricher.InputType
OutputType = MaigretEnricher.OutputType
