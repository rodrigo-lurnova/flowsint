#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""TED (Tenders Electronic Daily, DOUE S): anuncios de contratación en los que una
empresa figura como adjudicataria.

Mecanismo (API pública v3 de la Oficina de Publicaciones, sin clave, 2026-10):
  POST https://api.ted.europa.eu/v3/notices/search
       {"query": "<consulta experta>", "fields": [...], "limit": N, "page": 1,
        "paginationMode": "PAGE_NUMBER"}
  Consultas expertas usadas:
    winner-identifier IN ("A95758389" "ESA95758389")      (por NIF; también con prefijo ES)
    winner-name ~ "Iberdrola Clientes"                     (por nombre; búsqueda por palabras)
    ... AND buyer-country = ESP  SORT BY publication-date DESC
  `winner-identifier` no admite el operador ~ (devuelve 400).
  Respuesta: {"notices": [{publication-number, notice-title{lang}, buyer-name{lang:[..]},
  winner-name{lang:[..]}, winner-identifier[..], total-value, classification-cpv, ...}],
  "totalNoticeCount": N}. Enlaces: https://ted.europa.eu/es/notice/-/detail/<publication-number>

Limitaciones: solo contratos por encima de los umbrales UE (o publicados voluntariamente);
los campos winner-* proceden sobre todo de anuncios eForms (desde 2023). El identificador
del adjudicatario a veces es el del comprador (error de origen): compruébalo.

Uso:
  ted.py A95758389
  ted.py "Iberdrola Clientes" --max 25 --json
  ted.py "BLINDITEX" --todos-paises
"""
import argparse
import json
import re
import sys

import httpx

API = "https://api.ted.europa.eu/v3/notices/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept": "application/json"}
FIELDS = ["publication-number", "notice-title", "title-proc", "publication-date", "notice-type",
          "buyer-name", "buyer-country", "winner-name", "winner-identifier", "total-value",
          "total-value-cur", "result-value-notice", "classification-cpv", "place-of-performance"]
NIF_RE = re.compile(r"^(ES)?[A-Z0-9]\d{7}[A-Z0-9]$")


def _lang(v, prefer=("spa", "eng")):
    """TED devuelve los textos por idioma: {"spa": "...", "eng": "..."} o {"spa": [...]}."""
    if not isinstance(v, dict) or not v:
        return v
    for k in prefer:
        if k in v:
            return v[k]
    return next(iter(v.values()))


def consulta(texto: str, solo_espana: bool = True) -> str:
    t = re.sub(r"[\s.\-]", "", texto.upper())
    if NIF_RE.match(t):
        nif = t[2:] if t.startswith("ES") and len(t) == 11 else t
        q = f'winner-identifier IN ("{nif}" "ES{nif}")'
    else:
        q = 'winner-name ~ "{}"'.format(texto.replace('"', ""))
    if solo_espana:
        q += " AND buyer-country = ESP"
    return q + " SORT BY publication-date DESC"


def buscar(client: httpx.Client, texto: str, max_items: int = 25, solo_espana: bool = True) -> dict:
    q = consulta(texto, solo_espana)
    out, page, total = [], 1, 0
    size = min(max(max_items, 1), 100)
    while len(out) < max_items:
        r = client.post(API, json={"query": q, "fields": FIELDS, "limit": size, "page": page,
                                   "paginationMode": "PAGE_NUMBER"})
        r.raise_for_status()
        data = r.json()
        total = data.get("totalNoticeCount", 0)
        notices = data.get("notices", [])
        for n in notices:
            pub = n.get("publication-number")
            titulo = _lang(n.get("title-proc")) or _lang(n.get("notice-title"))
            out.append({
                "publicacion": pub,
                "fecha": (n.get("publication-date") or "")[:10],
                "tipo": n.get("notice-type"),
                "titulo": titulo[0] if isinstance(titulo, list) else titulo,
                "comprador": "; ".join(_lang(n.get("buyer-name")) or []),
                "adjudicatarios": _lang(n.get("winner-name")) or [],
                "identificadores": n.get("winner-identifier") or [],
                "importe": n.get("total-value"),
                "moneda": (n.get("total-value-cur") or [None])[0],
                "cpv": sorted(set(n.get("classification-cpv") or [])),
                "url": f"https://ted.europa.eu/es/notice/-/detail/{pub}",
                "pdf": f"https://ted.europa.eu/es/notice/{pub}/pdf",
            })
        if not notices or page * size >= total:
            break
        page += 1
    return {"consulta": q, "total": total, "resultados": out[:max_items]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="NIF del adjudicatario o nombre")
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--todos-paises", action="store_true", help="no limitar a compradores españoles")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=60) as c:
        res = buscar(c, a.texto, a.max, not a.todos_paises)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"TED: {res['total']} anuncios  ({res['consulta']})")
    for i in res["resultados"]:
        imp = f"{i['importe']:,.2f} {i['moneda'] or ''}" if isinstance(i["importe"], (int, float)) else "—"
        print(f"\n- {i['fecha']}  {i['publicacion']}  {imp}\n  {i['titulo']}\n  Comprador: {i['comprador']}"
              f"\n  Adjudicatario: {', '.join(i['adjudicatarios'])} {i['identificadores']}\n  {i['url']}")
    return 0 if res["resultados"] else 1


if __name__ == "__main__":
    sys.exit(main())
