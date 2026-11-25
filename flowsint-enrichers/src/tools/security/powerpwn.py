import io
import os
import tarfile
from typing import List, Optional

from docker.errors import APIError, NotFound

from ..dockertool import DockerTool

# In-container path where Copilot Studio Hunter writes its final results
# (anchored to the powerpwn package: src/powerpwn/copilot_studio/final_results).
# Overridable for images with a different layout.
DEFAULT_RESULTS_DIR = os.getenv(
    "POWERPWN_RESULTS_DIR",
    "/home/powerpwn/power-pwn/src/powerpwn/copilot_studio/final_results",
)
# File written by is_chat_live.js: one open/exposed bot URL per line.
OPEN_BOTS_FILENAME = "chat_exists_output.txt"


class PowerpwnTool(DockerTool):
    """Run mbrg/power-pwn via Docker-in-Docker.

    powerpwn pins Python <=3.11 and pydantic 1.10.7, which conflict with
    flowsint's runtime (Python >=3.12, pydantic 2.x), so it CANNOT be a pip
    dependency — it must run in its own image, exactly like the other DockerTools.

    There is no official published powerpwn image, so the image name is taken
    from the ``POWERPWN_IMAGE`` env var (build it from power-pwn's Dockerfile and
    push it to your registry). ``deep-scan`` additionally needs ffuf, Node.js and
    Puppeteer/Chromium, all of which the power-pwn Dockerfile installs.

    ``deep-scan`` writes its results to a file inside the container rather than to
    stdout. flowsint talks to the host Docker daemon through a mounted socket
    (Docker-in-Docker), so a host bind-mount would resolve against the host
    filesystem — not the worker container's — and the output would never come
    back. Instead the result file is copied out of the finished container over
    the Docker API (``get_archive``), which needs no shared filesystem, no host
    path translation and no cross-uid write permissions.
    """

    image = os.getenv("POWERPWN_IMAGE", "powerpwn/powerpwn")
    default_tag = os.getenv("POWERPWN_IMAGE_TAG", "latest")

    def __init__(self) -> None:
        # Honour a fully-qualified "repo:tag" in POWERPWN_IMAGE if provided.
        raw = os.getenv("POWERPWN_IMAGE", "powerpwn/powerpwn")
        if ":" in raw.rsplit("/", 1)[-1]:
            repo, _, tag = raw.rpartition(":")
            super().__init__(repo, tag)
        else:
            super().__init__(raw, self.default_tag)

    @classmethod
    def name(cls) -> str:
        return "powerpwn"

    @classmethod
    def description(cls) -> str:
        return "Microsoft Power Platform / Copilot offensive recon toolkit."

    @classmethod
    def category(cls) -> str:
        return "Copilot / Power Platform recon"

    @staticmethod
    def _read_member_from_archive(stream) -> str:
        """Decode the single regular file inside a ``get_archive`` tar stream."""
        buf = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buf) as tar:
            for member in tar:
                if member.isfile():
                    f = tar.extractfile(member)
                    if f is not None:
                        return f.read().decode("utf-8", errors="ignore")
        return ""

    def _copy_out_open_bots(self, container) -> List[str]:
        """Fetch the open-bot file from a finished container, one URL per line.

        Returns an empty list when the file is absent (e.g. no exposed bots were
        found, in which case the tool writes nothing).
        """
        remote_path = f"{DEFAULT_RESULTS_DIR.rstrip('/')}/{OPEN_BOTS_FILENAME}"
        try:
            stream, _ = container.get_archive(remote_path)
        except NotFound:
            return []
        content = self._read_member_from_archive(stream)
        return [ln.strip() for ln in content.splitlines() if ln.strip()]

    def copilot_deep_scan(
        self,
        domain: str,
        rate: Optional[int] = None,
        threads: Optional[int] = None,
        proxy: Optional[str] = None,
        timeout: int = 3600,
    ) -> List[str]:
        """Run ``copilot-studio-hunter deep-scan -d <domain>``.

        Returns the list of open/exposed Copilot Studio bot URLs discovered (one
        per line of ``chat_exists_output.txt``), copied out of the container over
        the Docker API. Returns an empty list when none are found.
        """
        args: List[str] = ["copilot-studio-hunter", "deep-scan", "-d", domain]
        if rate is not None:
            args += ["-r", str(rate)]
        if threads is not None:
            args += ["-t", str(threads)]
        if proxy:
            args += ["-x", proxy]
        command = " ".join(args)

        # Make sure the image is present. There is no published powerpwn image,
        # so it is typically built locally — only pull when it is genuinely
        # missing, otherwise a locally-built image would fail on a registry pull.
        if not self.is_installed():
            self.install()

        # The powerpwn console script is the entrypoint; the image's default CMD
        # starts a webserver, so override it to run the CLI instead. Run detached
        # so the result file can be copied out before the container is removed.
        container = self.client.containers.create(
            self.image,
            command=command,
            entrypoint="powerpwn",
            environment={"TERM": "dumb"},
            network_mode="bridge",
            tty=False,
            stdin_open=False,
        )
        try:
            container.start()
            try:
                container.wait(timeout=timeout)
            except Exception as e:
                # Timeout or transport error: stop the container so the archive
                # copy below still reflects whatever partial output exists.
                try:
                    container.kill()
                except (APIError, NotFound):
                    pass
                raise RuntimeError(
                    f"powerpwn deep-scan timed out or failed for {domain}: {e}"
                )
            return self._copy_out_open_bots(container)
        finally:
            try:
                container.remove(force=True)
            except (APIError, NotFound):
                pass
