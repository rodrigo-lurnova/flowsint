"""Utilidades compartidas por los enrichers BORME (organization_*_borme, individual_*_borme).

La lógica de descarga/parseo vive en osint-es-scripts/borme.py (y apispain.py);
aquí solo: parámetros comunes, llamada no bloqueante y conversión a tipos Flowsint.
"""
import asyncio
import os
import re
from typing import Any, Dict, List, Optional

from flowsint_enrichers.es._scripts import load
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization

PARTICULAS = {"DE", "DEL", "LA", "LAS", "LOS", "Y", "SAN", "SANTA", "VAN", "VON", "DA", "DOS", "DI"}
PALABRAS_JURIDICAS = {"SOCIEDAD", "SOCIEDADES", "AUDITORES", "AUDITORIA", "ASESORES", "CONSULTORES", "BANCO",
                      "CAJA", "FUNDACION", "ASOCIACION", "HOLDING", "GROUP", "GRUPO", "INVERSIONES", "GESTION",
                      "LIMITED", "LTD", "LLC", "LLP", "INC", "GMBH", "BV", "SARL", "SAS", "SRL", "SPA", "AG",
                      "CORPORATION", "CORP", "COMPANY", "PARTNERS", "CAPITAL", "SERVICIOS", "ABOGADOS"}

PARAMS_COMUNES: List[Dict[str, Any]] = [
    {"name": "DIAS", "type": "string", "default": "90", "required": False,
     "description": "Días hacia atrás a revisar en el BORME si no se da DESDE (coste: ~2 peticiones/día hábil "
                    "en modo índice, ~30/día en modo completo)."},
    {"name": "DESDE", "type": "string", "required": False, "description": "Fecha inicial AAAA-MM-DD."},
    {"name": "HASTA", "type": "string", "required": False, "description": "Fecha final AAAA-MM-DD (hoy)."},
    {"name": "PROVINCIA", "type": "string", "required": False,
     "description": "Provincias del Registro Mercantil separadas por comas (nombre o código INE). "
                    "Reduce mucho el coste si se conoce."},
    {"name": "EXACTA", "type": "string", "default": "false", "required": False,
     "description": "true = solo denominación exacta (normalizada, sin forma jurídica)."},
    {"name": "BACKEND", "type": "string", "default": "auto", "required": False,
     "description": "auto | boe | apispain (auto usa apispain si hay APISPAIN_API_KEY)."},
    {"name": "APISPAIN_API_KEY", "type": "vaultSecret", "required": False,
     "description": "Clave opcional de apispain.es (plan Free: 100 peticiones/mes)."},
]


def p_bool(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "si", "sí", "yes", "y")


def p_list(v: Any) -> Optional[List[str]]:
    xs = [x.strip() for x in str(v or "").split(",") if x.strip()]
    return xs or None


def borme():
    return load("borme")


async def buscar_empresa(nombre: str, params: Dict[str, Any], api_key: Optional[str]) -> dict:
    mod = borme()
    backend = (params.get("BACKEND") or "auto").lower()
    exacta = p_bool(params.get("EXACTA"))
    if backend == "apispain" or (backend == "auto" and api_key):
        return await asyncio.to_thread(mod.buscar_empresa_apispain, nombre, exacta, api_key)
    return await asyncio.to_thread(
        mod.buscar_empresa, nombre, params.get("DESDE"), params.get("HASTA"), int(params.get("DIAS") or 90),
        p_list(params.get("PROVINCIA")), p_bool(params.get("COMPLETO")), exacta, mod.DEFAULT_DB, 0.5,
        int(params.get("MAX_PETICIONES") or 3000))


async def buscar_persona(nombre: str, params: Dict[str, Any]) -> dict:
    mod = borme()
    return await asyncio.to_thread(
        mod.buscar_persona, nombre, params.get("DESDE"), params.get("HASTA"), int(params.get("DIAS") or 30),
        p_list(params.get("PROVINCIA")), mod.DEFAULT_DB, 0.5, int(params.get("MAX_PETICIONES") or 1500))


def es_persona_juridica(nombre: str) -> bool:
    n = borme().normalizar_denominacion(nombre)
    if n["forma"] or len(n["base"].split()) == 1:  # un solo token (p. ej. socio único "MAJENTESS")
        return True
    return bool(set(n["base"].split()) & PALABRAS_JURIDICAS)


def _title(s: str) -> str:
    return " ".join("-".join(x.capitalize() for x in w.split("-")) if w.upper() not in PARTICULAS else w.lower()
                    for w in s.split())


def individuo(persona_borme: str, **extra) -> Individual:
    """'CABANES VERDU FRANCISCO JOSE' (apellidos primero, formato BORME) -> Individual
    con full_name en orden natural 'Francisco Jose Cabanes Verdu' y el original en aliases."""
    toks = re.sub(r"\s+", " ", persona_borme.replace(",", " ")).strip().split()
    grupos, buf = [], []
    for t in toks:
        buf.append(t)
        if t.upper() not in PARTICULAS:
            grupos.append(" ".join(buf))
            buf = []
    if buf:
        grupos.append(" ".join(buf))
    if "," in persona_borme:  # 'APELLIDOS, NOMBRE'
        ap, nom = persona_borme.split(",", 1)
        apellidos, nombre = ap.strip(), nom.strip()
    elif len(grupos) >= 3:
        apellidos, nombre = " ".join(grupos[:2]), " ".join(grupos[2:])
    elif len(grupos) == 2:
        apellidos, nombre = grupos[0], grupos[1]
    else:
        apellidos, nombre = "", persona_borme
    first, last = _title(nombre), _title(apellidos)
    return Individual(full_name=f"{first} {last}".strip(), first_name=first or None, last_name=last or None,
                      aliases=[persona_borme], source="BORME", **extra)


def org_actualizada(org: Organization, **campos) -> Organization:
    """Copia de `org` (mismo nodeLabel -> mismo nodo) con campos nuevos no vacíos."""
    base = org.model_dump(exclude_unset=True, exclude={"nodeLabel"})
    base.update({k: v for k, v in campos.items() if v not in (None, "", [], {})})
    return Organization(**base)


def municipio_de(domicilio: Optional[str]) -> Optional[str]:
    m = re.search(r"\(([^()]+)\)\s*$", domicilio or "")
    return m.group(1).strip() if m else None


def historial_cargos(actos: List[dict]) -> List[dict]:
    """Por (persona, cargo_codigo): primera/última fecha, última acción y si sigue vigente (inferido)."""
    h: Dict[tuple, dict] = {}
    for a in sorted(actos, key=lambda x: (x["fecha"], x.get("num") or 0)):
        for c in a.get("cargos") or []:
            k = (c["persona"].upper(), c["cargo_codigo"])
            e = h.setdefault(k, {"persona": c["persona"], "cargo": c["cargo"], "cargo_codigo": c["cargo_codigo"],
                                 "desde": None, "hasta": None, "ultima_accion": None, "vigente": False,
                                 "borme_ids": []})
            if c["accion"] in ("nombramiento", "reelección", "declaración", "vigente"):
                e["desde"] = e["desde"] if e["vigente"] else a["fecha"]
                e["vigente"], e["hasta"] = True, None
            elif c["accion"] in ("cese", "revocación", "cancelación"):
                e["vigente"], e["hasta"] = False, a["fecha"]
            e["ultima_accion"], e["cargo"] = c["accion"], c["cargo"]
            if a.get("borme_id") and a["borme_id"] not in e["borme_ids"]:
                e["borme_ids"].append(a["borme_id"])
        if "extincion" in (a.get("tipos") or []):
            for e in h.values():
                if e["vigente"]:
                    e["vigente"], e["hasta"] = False, a["fecha"]
    return list(h.values())


def api_key_de(enricher) -> Optional[str]:
    return enricher.get_secret("APISPAIN_API_KEY", os.getenv("APISPAIN_API_KEY"))
