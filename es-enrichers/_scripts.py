"""Acceso a los scripts de osint-es-scripts/ desde los enrichers.

Los scripts son CLIs autocontenidos (solo httpx + stdlib) que también exponen
funciones importables; docker-compose.prod.yml los monta en SCRIPTS_DIR.
"""
import importlib
import os
import sys

SCRIPTS_DIR = os.getenv("OSINT_ES_SCRIPTS_DIR", "/opt/osint-es-scripts")


def load(name: str):
    """Importa osint-es-scripts/<name>.py como módulo (p. ej. load("bdns"))."""
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    return importlib.import_module(name)
