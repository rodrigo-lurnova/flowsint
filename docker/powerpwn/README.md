# powerpwn image (Copilot Studio Hunter)

The `domain_to_copilot_agents` enricher discovers exposed Microsoft Copilot
Studio bots by running [power-pwn](https://github.com/mbrg/power-pwn)'s Copilot
Studio Hunter `deep-scan`. power-pwn pins **Python ≤3.11** and **pydantic
1.10.7**, which conflict with flowsint's runtime (Python ≥3.12, pydantic 2), so
it cannot be a pip dependency. Instead flowsint runs it as an isolated Docker
image (Docker-in-Docker, the same pattern used for `subfinder` and
`trufflehog`). No official power-pwn image is published, so you build it here.

## Build & push

```bash
# From the repository root. Pin POWERPWN_REF to a tag/commit for reproducibility.
docker build \
  --build-arg POWERPWN_REF=main \
  -t your-registry/powerpwn:latest \
  docker/powerpwn

docker push your-registry/powerpwn:latest
```

The image bundles everything `deep-scan` needs: the `powerpwn` CLI, `ffuf`,
Node.js and the Puppeteer recon scripts with the system Chromium.

> Build for the architecture of the host that will run the containers. Pass
> `--build-arg TARGETARCH=arm64` (and build on/for arm64) if your workers are
> ARM; the default is `amd64`.

## Wire it into flowsint

Set the image reference in your environment (see `.env.example`):

```bash
POWERPWN_IMAGE=your-registry/powerpwn:latest
```

The enricher reads `POWERPWN_IMAGE` (optionally `POWERPWN_IMAGE_TAG`, and
`POWERPWN_RESULTS_DIR` if you change the in-container results path). It runs the
container and then copies `chat_exists_output.txt` (one open-bot URL per line)
out of it over the Docker API, from which it builds `AiAgent` nodes. There is no
host bind-mount, so this works under Docker-in-Docker (the worker talks to the
host daemon through a mounted socket) without any shared filesystem.

## Run manually (debug)

```bash
docker run --rm -v "$PWD/out:/home/powerpwn/power-pwn/src/powerpwn/copilot_studio/final_results" \
  your-registry/powerpwn:latest copilot-studio-hunter deep-scan -d example.com
cat out/chat_exists_output.txt
```

## Legal / ethics

Only scan tenants/domains you are authorised to test. See the repository's
`DISCLAIMER.md` and `ETHICS.md`.
