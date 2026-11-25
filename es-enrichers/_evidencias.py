"""Hallazgos (Finding) y fallos de fuente de los enrichers de personas."""

import math
import os
from collections import Counter
from typing import Any, Iterable, List, Optional

from flowsint_core.core.enricher_base import Enricher, mask_secret
from flowsint_core.core.findings import Finding
from flowsint_core.core.logger import Logger
from flowsint_enrichers.es._scripts import load

UMBRAL_PROBABLE = 0.8
SIN_TOKEN = "APIFY_TOKEN no configurado"


def confianza(valor: Any) -> Optional[float]:
    """Confianza válida para Finding: recortada a 0-1, o None si no es un número finito."""
    if (
        isinstance(valor, bool)
        or not isinstance(valor, (int, float))
        or not math.isfinite(valor)
    ):
        return None
    return min(max(float(valor), 0.0), 1.0)


def certeza(valor: Any) -> str:
    """Regla de LinkedIn y perfiles: «probable» desde 0,8; sin confianza, «posible»."""
    c = confianza(valor)
    return "probable" if c is not None and c >= UMBRAL_PROBABLE else "posible"


def supera(valor: Any, minimo: float) -> bool:
    """Los elementos sin confianza no se descartan: se quedan como «posible»."""
    c = confianza(valor)
    return c is None or c >= minimo


def pct(valor: Any) -> str:
    c = confianza(valor)
    return "sin confianza" if c is None else f"{c:.0%}"


def anotar(
    enricher: Enricher,
    source: str,
    subject: Any,
    relation: str,
    objeto: Any,
    certainty: str,
    conf: Any = None,
    evidence: Optional[Iterable[Any]] = None,
    url: Any = None,
) -> None:
    """add_finding sin que un dato mal formado de la fuente tumbe el scan."""
    if not subject or not objeto:
        return
    enricher.add_finding(
        Finding(
            subject=str(subject),
            relation=relation,
            object=str(objeto),
            source=source,
            certainty=certainty,
            confidence=confianza(conf),
            evidence=[str(e) for e in evidence or [] if e],
            url=str(url) if url else None,
        )
    )


def fallo(
    enricher: Enricher,
    source: str,
    subject: str,
    error: Any,
    secreto: Optional[str] = None,
) -> None:
    """Registra el fallo de una fuente (add_failure + log). `secreto` cubre las claves que llegan por entorno."""
    detalle = (
        f"{type(error).__name__}: {error}"
        if isinstance(error, Exception)
        else str(error)
    )
    enricher.add_failure(f"{source}: {subject}: {mask_secret(detalle, secreto)}")
    Logger.error(enricher.sketch_id, {"message": enricher.failures[-1]})


def aviso(
    enricher: Enricher,
    source: str,
    subject: str,
    detalle: str,
    secreto: Optional[str] = None,
) -> None:
    """Registra algo que limita el resultado sin ser un fallo de la fuente (add_warning + log)."""
    enricher.add_warning(f"{source}: {subject}: {mask_secret(detalle, secreto)}")
    Logger.info(enricher.sketch_id, {"message": enricher.warnings[-1]})


def error_de_fuente(
    enricher: Enricher,
    source: str,
    subject: str,
    error: str,
    secreto: Optional[str] = None,
) -> None:
    """Error que devuelve un script: aviso si solo afecta a un candidato o es un límite de peticiones."""
    limite = load("personas_comun").LIMITE_GITHUB
    leve = error.startswith("Gravatar: ") or limite in error
    (aviso if leve else fallo)(enricher, source, subject, error, secreto)


def sin_buscar(
    enricher: Enricher, source: str, que: str, total: int, motivos: List[str]
) -> None:
    """Un aviso por motivo con cuántas entradas no se han buscado («2 de 5 personas sin buscar: …»)."""
    for motivo, n in Counter(motivos).items():
        enricher.add_warning(f"{source}: {n} de {total} {que} sin buscar: {motivo}")
        Logger.info(enricher.sketch_id, {"message": enricher.warnings[-1]})


def relacion(nombre: str, certeza: str) -> str:
    """Relación con prefijo POSIBLE_ si el hallazgo solo es posible."""
    return f"POSIBLE_{nombre}" if certeza == "posible" else nombre


def clave(enricher: Enricher, nombre: str) -> Optional[str]:
    """Clave de la vault o, si falta, del entorno, sin espacios ni saltos de línea alrededor."""
    valores = (enricher.params.get(nombre), os.getenv(nombre))
    return next((v.strip() for v in valores if v and v.strip()), None)


def sin_token(enricher: Enricher) -> None:
    enricher.add_failure(SIN_TOKEN)
    Logger.error(
        enricher.sketch_id, {"message": f"[APIFY] {SIN_TOKEN} (vault o entorno)"}
    )
