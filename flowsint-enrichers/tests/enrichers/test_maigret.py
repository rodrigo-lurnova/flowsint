import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from flowsint_enrichers.social import to_maigret
from flowsint_enrichers.social.to_maigret import MaigretEnricher
from flowsint_types import Username


def site(url, similar=False, status="Claimed"):
    return {
        "status": {"status": status, "url": url, "ids": {"fullname": "Foo"}},
        "is_similar": similar,
    }


REPORT = {
    "GitHub": site("https://github.com/foo"),
    "Discord": site("https://discord.com"),
    "StackOverflow": site("https://so.com/users?search=foo", similar=True),
    "Other": site("https://other.com/foo", status="Available"),
}


@pytest.fixture
def errors(monkeypatch):
    logged = []
    monkeypatch.setattr(
        to_maigret.Logger, "error", lambda _id, content: logged.append(content)
    )
    return logged


def make(**params):
    return MaigretEnricher(sketch_id="s", params=params, graph_service=object())


def test_command():
    cmd = make(tags="es").build_command("-foo", "/d")
    assert "--no-recursion" in cmd and "--tags=es" in cmd
    assert cmd[-2:] == ["--", "-foo"]
    assert "--all-sites" in make(top_sites="all").build_command("foo", "/d")


def test_scan_keeps_only_real_profiles(errors):
    def run(cmd, **_):
        out = Path(cmd[cmd.index("-fo") + 1])
        (out / "report_foo_simple.json").write_text(json.dumps(REPORT))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(to_maigret.subprocess, "run", run):
        results = asyncio.run(make().scan([Username(value="Foo")]))
    assert [r.platform for r in results] == ["GitHub"]
    assert errors == []


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_failures_are_logged(errors, failure):
    def run(cmd, **_):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 2, "", "boom")

    with patch.object(to_maigret.subprocess, "run", run):
        assert asyncio.run(make().scan([Username(value="foo")])) == []
    assert len(errors) == 1


def test_params_are_resolved():
    enricher = make(top_sites="1500")
    asyncio.run(enricher.async_init())
    assert enricher.params["top_sites"] == "1500"
    assert enricher.params["timeout"] == "300"
