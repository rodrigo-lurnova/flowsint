#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""CNMV: búsqueda de entidades en los Registros Oficiales y ficha por NIF.

Mecanismo observado en la web (cnmv.es, 2026-10):
  - Búsqueda por nombre: formulario ASP.NET WebForms. POST a
      /portal/consultas/busquedaporentidad
    con __VIEWSTATE, __VIEWSTATEGENERATOR y __EVENTVALIDATION (hay que leerlos
    antes con un GET) y el campo ctl00$ContentPrincipal$txtBusqueda. La respuesta
    trae un <select name="ctl00$ContentPrincipal$wuc_Entidades$lstSeleccion"> cuyas
    opciones son (NIF, nombre).
  - Ficha de entidad: GET directo sin estado
      /portal/consultas/DatosEntidad?nif=A-48010615
    Lista las secciones del registro con información para ese NIF (ISIN, folletos,
    hechos relevantes, participaciones significativas, cuentas anuales...), cada una
    con su enlace.

Uso:
  cnmv.py Iberdrola          # busca por nombre y muestra la ficha de cada NIF
  cnmv.py A-48010615         # ficha directa por NIF
  cnmv.py Iberdrola --json
"""
import argparse
import html
import json
import re
import sys
from urllib.parse import urljoin

import httpx

BASE = "https://www.cnmv.es/portal/consultas/"
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept-Language": "es-ES,es;q=0.9"}
NIF_RE = re.compile(r"^[A-Z]-?\d{7,8}[A-Z0-9]?$", re.I)


def buscar(client: httpx.Client, texto: str) -> list[dict]:
    page = client.get(BASE + "busquedaporentidad").text
    form = {k: re.search(rf'id="{k}" value="([^"]*)"', page).group(1)
            for k in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")}
    form.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "",
                 "ctl00$ContentPrincipal$txtBusqueda": texto,
                 "ctl00$ContentPrincipal$btnBuscar": "Buscar"})
    r = client.post(BASE + "busquedaporentidad", data=form)
    r.raise_for_status()
    sel = re.search(r'name="ctl00\$ContentPrincipal\$wuc_Entidades\$lstSeleccion".*?</select>', r.text, re.S)
    if not sel:
        return []
    return [{"nif": nif, "nombre": html.unescape(nombre).strip()}
            for nif, nombre in re.findall(r'<option[^>]*value="([^"]+)">(.*?)</option>', sel.group(0))]


def ficha(client: httpx.Client, nif: str) -> dict:
    r = client.get(BASE + "DatosEntidad", params={"nif": nif})
    r.raise_for_status()
    nombre = re.search(r"<title>\s*CNMV - [^-]+ - (.*?)\s*</title>", r.text, re.S)
    secciones = [{"seccion": html.unescape(re.sub(r"<[^>]+>", "", txt)).strip(),
                  "url": urljoin(str(r.url), html.unescape(href))}
                 for href, txt in re.findall(r'id="ctl00_ContentPrincipal_repIzquierda_ctl\d+_hlEnlace" href="([^"]+)">(.*?)</a>', r.text, re.S)]
    return {"nif": nif, "nombre": html.unescape(nombre.group(1)) if nombre else None,
            "url": str(r.url), "secciones": secciones}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="nombre (o parte) de la entidad, o NIF")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        entidades = [{"nif": a.texto.upper()}] if NIF_RE.match(a.texto) else buscar(c, a.texto)
        if not entidades:
            print("Sin resultados en la CNMV.", file=sys.stderr)
            return 1
        fichas = [ficha(c, e["nif"]) for e in entidades]

    if a.json:
        json.dump(fichas, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    for f in fichas:
        print(f"\n== {f['nombre']}  [{f['nif']}]  {f['url']}")
        for s in f["secciones"]:
            print(f"   - {s['seccion']}: {s['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
