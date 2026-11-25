"""Expone es-enrichers/ como flowsint_enrichers.es, igual que el montaje de docker-compose.prod.yml."""

import asyncio
import importlib.util
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RAIZ = Path(__file__).resolve().parents[2]
ES = RAIZ / "es-enrichers"

os.environ["OSINT_ES_SCRIPTS_DIR"] = str(RAIZ / "osint-es-scripts")
os.environ.setdefault("AUTH_SECRET", "test-only")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

import flowsint_enrichers  # noqa: E402
from flowsint_core.core.logger import Logger  # noqa: E402


def _montar_paquete() -> None:
    spec = importlib.util.spec_from_file_location(
        "flowsint_enrichers.es",
        ES / "__init__.py",
        submodule_search_locations=[str(ES)],
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["flowsint_enrichers.es"] = modulo
    spec.loader.exec_module(modulo)
    flowsint_enrichers.es = modulo


_montar_paquete()


class GrafoFalso:
    """GraphService mínimo: guarda lo escrito y responde a query() con `filas` (o lanza `error`)."""

    def __init__(self):
        self.nodos, self.relaciones, self.mensajes, self.consultas = [], [], [], []
        self.filas, self.error = [], None

    def query(self, cypher, parametros=None):
        self.consultas.append((cypher, parametros))
        if self.error:
            raise self.error
        return self.filas

    def create_node_from_flowsint_type(self, node_obj):
        self.nodos.append(node_obj)

    def create_relationship(self, from_obj, to_obj, rel_label="IS_RELATED_TO"):
        self.relaciones.append((from_obj.nodeLabel, rel_label, to_obj.nodeLabel))

    def log_graph_message(self, mensaje):
        self.mensajes.append(mensaje)

    def flush(self):
        pass


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    """Sin red, sin claves del entorno y sin registro en Postgres/Redis. Devuelve los mensajes del Logger."""
    for clave in (
        "APIFY_TOKEN",
        "HUNTER_API_KEY",
        "GITHUB_TOKEN",
        "APISPAIN_API_KEY",
        "PDCP_API_KEY",
    ):
        monkeypatch.delenv(clave, raising=False)

    def sin_red(*args, **kwargs):
        raise AssertionError("los tests no pueden usar la red")

    monkeypatch.setattr(socket, "create_connection", sin_red)
    monkeypatch.setattr(socket, "getaddrinfo", sin_red)
    mensajes = []
    for nivel in ("info", "error", "warn", "completed", "graph_append"):
        if hasattr(Logger, nivel):
            monkeypatch.setattr(
                Logger,
                nivel,
                staticmethod(lambda sketch, m, *a, **k: mensajes.append(str(m))),
            )
    return mensajes


@pytest.fixture
def grafo():
    return GrafoFalso()


@pytest.fixture
def correr(grafo):
    """correr(Clase, [entradas], PARAM=valor) -> enricher ya ejecutado (los secretos salen de una vault falsa)."""

    def _correr(clase, entradas, **params):
        secretos = {
            p["name"]
            for p in clase.get_params_schema()
            if p.get("type") == "vaultSecret"
        }
        vault = SimpleNamespace(
            get_secret=lambda clave: params.get(clave) if clave in secretos else None
        )
        enricher = clase(
            sketch_id="s",
            scan_id="t",
            vault=vault,
            graph_service=grafo,
            params={k: v for k, v in params.items() if k not in secretos},
        )
        enricher.resultados = asyncio.run(enricher.execute(entradas))
        return enricher

    return _correr
