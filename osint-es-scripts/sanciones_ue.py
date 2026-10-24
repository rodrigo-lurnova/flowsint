#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Sanciones financieras de la UE: busca una empresa (o persona) en la lista consolidada
oficial de la Comisión Europea (Financial Sanctions Files, FSF).

Mecanismo (2026-10): descarga pública, sin registro, del fichero completo
  GET https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw
  (el token es público y fijo: lo publica la propia Comisión en data.europa.eu)
  -> XML ~25 MB: <sanctionEntity euReferenceNumber designationDate> con <subjectType code=
     "person|enterprise">, <nameAlias wholeName>, <regulation programme numberTitle
     publicationUrl>, <address>, <identification number identificationTypeCode>.
Se guarda en caché local 24 h (OSINT_ES_CACHE o el directorio temporal) y se busca en local,
así que se hace una sola descarga al día aunque se consulten muchas empresas.
sanctionsmap.eu es solo un visor de los regímenes (no lista entidades por API) y
OpenSanctions exige clave de API desde 2024, por eso no se usan.

Coincidencias: "identificador" (el NIF/registro aparece en <identification>), "exacta" (misma
denominación normalizada, sin forma jurídica) o "contiene" (todas las palabras de la consulta).
Un nombre que coincide NO prueba que sea la misma entidad: revisa país, dirección e identificadores.

Uso:
  sanciones_ue.py "ROSNEFT"
  sanciones_ue.py "BLINDITEX" --json
  sanciones_ue.py A48010615
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from resolver_empresa import limpiar_nif, normalizar, parece_nif  # noqa: E402

URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
       "?token=dG9rZW4tMjAxNw")
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)"}
NS = "{http://eu.europa.ec/fpi/fsd/export}"
TTL = 24 * 3600


def ruta_cache() -> str:
    d = os.getenv("OSINT_ES_CACHE") or os.path.join(tempfile.gettempdir(), "osint-es-cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "eu_fsf_full.xml")


def descargar(client: httpx.Client, forzar: bool = False) -> str:
    path = ruta_cache()
    if not forzar and os.path.exists(path) and time.time() - os.path.getmtime(path) < TTL:
        return path
    tmp = path + ".part"
    with client.stream("GET", URL, timeout=180) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    os.replace(tmp, path)
    return path


def _entidades(path: str):
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != NS + "sanctionEntity":
            continue
        st = el.find(NS + "subjectType")
        regs = el.findall(NS + "regulation")
        yield {
            "referencia_ue": el.get("euReferenceNumber"),
            "fecha_designacion": el.get("designationDate") or None,
            "tipo": st.get("code") if st is not None else None,
            "nombres": list(dict.fromkeys(n.get("wholeName") for n in el.findall(NS + "nameAlias") if n.get("wholeName"))),
            "programas": sorted({r.get("programme") for r in regs if r.get("programme")}),
            "reglamentos": [{"numero": r.get("numberTitle"), "fecha": r.get("publicationDate"),
                             "url": (r.findtext(NS + "publicationUrl") or "").strip() or None} for r in regs],
            "direcciones": [", ".join(x for x in (a.get("street"), a.get("zipCode"), a.get("city"),
                                                   a.get("countryDescription")) if x)
                            for a in el.findall(NS + "address")],
            "identificadores": [{"numero": i.get("number"), "tipo": i.get("identificationTypeDescription"),
                                 "pais": i.get("countryIso2Code")} for i in el.findall(NS + "identification")
                                if i.get("number")],
            "observaciones": (el.findtext(NS + "remark") or "").strip() or None,
        }
        el.clear()


def buscar(client: httpx.Client, texto: str, max_items: int = 25, tipo: str | None = None) -> dict:
    """tipo: None (todos), "enterprise" o "person"."""
    path = descargar(client)
    nif = limpiar_nif(texto)
    por_id = parece_nif(texto) or (bool(re.fullmatch(r"[A-Z0-9]{6,20}", nif)) and sum(c.isdigit() for c in nif) >= 6)
    q = normalizar(texto)["base"]
    q_toks = set(q.split())
    out = []
    for e in _entidades(path):
        if tipo and e["tipo"] != tipo:
            continue
        nivel = None
        if por_id:
            if any(limpiar_nif(i["numero"]) == nif for i in e["identificadores"]):
                nivel = "identificador"
        else:
            bases = [normalizar(n)["base"] for n in e["nombres"]]
            if q in bases:
                nivel = "exacta"
            elif q_toks and any(q_toks <= set(b.split()) for b in bases):
                nivel = "contiene"
        if nivel:
            e["coincidencia"] = nivel
            out.append(e)
    out.sort(key=lambda e: ("identificador", "exacta", "contiene").index(e["coincidencia"]))
    return {"consulta": texto, "fichero": path, "actualizado": time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))), "total": len(out), "resultados": out[:max_items]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="denominación, nombre o NIF/identificador")
    ap.add_argument("--tipo", choices=["enterprise", "person"], help="filtrar por tipo de sujeto")
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as c:
        res = buscar(c, a.texto, a.max, a.tipo)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"Lista consolidada UE (descarga {res['actualizado']}): {res['total']} coincidencias para «{a.texto}»")
    for e in res["resultados"]:
        url = next((r["url"] for r in e["reglamentos"] if r["url"]), "")
        print(f"\n- [{e['coincidencia']}] {e['referencia_ue']} ({e['tipo']}, {e['fecha_designacion']}) "
              f"programas {', '.join(e['programas'])}\n  {' | '.join(e['nombres'][:4])}\n  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
