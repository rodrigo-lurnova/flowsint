from typing import List, Optional

from ..dockertool import DockerTool


class TrufflehogTool(DockerTool):
    """Run trufflehog via its official Docker image (Docker-in-Docker).

    trufflehog streams one JSON object per finding to stdout with ``--json``;
    its banner/progress goes to stderr. DockerTool captures both combined, so
    callers must parse stdout line by line and ignore non-JSON lines.
    """

    image = "trufflesecurity/trufflehog"
    default_tag = "latest"

    def __init__(self) -> None:
        super().__init__(self.image, self.default_tag)

    @classmethod
    def name(cls) -> str:
        return "trufflehog"

    @classmethod
    def description(cls) -> str:
        return "Find, verify, and analyze leaked credentials in source and artifacts."

    @classmethod
    def category(cls) -> str:
        return "Secret scanning"

    def install(self) -> None:
        super().install()

    def is_installed(self) -> bool:
        return super().is_installed()

    def update(self) -> None:
        self.install()

    def scan_git(
        self,
        repo_url: str,
        only_verified: bool = False,
        extra_args: Optional[List[str]] = None,
        timeout: int = 600,
    ) -> str:
        """Scan a remote git repository and return raw combined output (JSONL).

        ``--no-update`` stops the container trying to self-update; ``--json``
        emits machine-readable findings. ``--results`` selects which result
        classes to emit (``--only-verified`` is a hidden legacy alias).
        """
        args: List[str] = ["git", repo_url, "--json", "--no-update"]
        if only_verified:
            args.append("--results=verified")
        else:
            args.append("--results=verified,unverified,unknown")
        if extra_args:
            args.extend(extra_args)
        command = " ".join(args)
        return super().launch(command, timeout=timeout)
