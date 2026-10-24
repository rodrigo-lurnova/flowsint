#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""BOE: Tablón Edictal Único (TEU, notificaciones administrativas) y Tablón
Edictal Judicial Único (TEJU, edictos judiciales).

Mecanismo observado en la web (boe.es, 2026-10): formulario GET, mismo esquema
de parámetros en los dos buscadores:
  TEU : /notificaciones/notificaciones.php
  TEJU: /buscar/edictos_judiciales.php
  ?campo[0]=DOC&dato[0]=<texto: NIF, nombre...>&operador[0]=and
  &campo[1]=DEM&dato[1]=<departamento>&operador[1]=and
  &campo[2]=MATERIA (TEU) | JUR (TEJU)&dato[2]=<código>
  &campo[4]=FPU&dato[4][0]=<desde dd/mm/aaaa>&dato[4][1]=<hasta>
  &page_hits=50&sort_field[0]=FPU&sort_order[0]=desc&accion=Buscar
Resultados en <li class="resultado-busqueda"> con el boletín, el organismo, el
asunto y el enlace al PDF (/boe_n/dias/AAAA/MM/DD/not.php?id=BOE-N-...).

Ojo: el texto completo de cada edicto solo es público 3 meses; después solo con CSV.

Uso:
  boe_edictos.py "B26613679"             # TEU
  boe_edictos.py "BLINDITEX" --judicial  # TEJU
  boe_edictos.py "IBERDROLA" --desde 01/01/2026 --json
"""
import argparse
import html
import json
import re
import sys

import httpx

URLS = {"teu": "https://www.boe.es/notificaciones/notificaciones.php",
        "teju": "https://www.boe.es/buscar/edictos_judiciales.php"}
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept-Language": "es-ES,es;q=0.9"}


def clean(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s))).strip()


def buscar(client: httpx.Client, texto: str, judicial: bool, desde: str, hasta: str, hits: int) -> dict:
    url = URLS["teju" if judicial else "teu"]
    params = {"campo[0]": "DOC", "dato[0]": texto, "operador[0]": "and",
              "campo[1]": "DEM", "dato[1]": "", "operador[1]": "and",
              "campo[2]": "JUR" if judicial else "MATERIA", "dato[2]": "", "operador[2]": "and",
              "campo[3]": "NBO", "dato[3]": "", "operador[4]": "and",
              "campo[4]": "FPU", "dato[4][0]": desde, "dato[4][1]": hasta,
              "page_hits": hits, "sort_field[0]": "FPU", "sort_order[0]": "desc",
              "sort_field[1]": "id", "sort_order[1]": "asc", "accion": "Buscar"}
    r = client.get(url, params=params)
    r.raise_for_status()
    total = re.search(r"Resultados \d+ a \d+ de (\d+)", r.text)
    items = []
    for li in re.findall(r'<li class="resultado-busqueda">(.*?)</li>\s*</ul>\s*</li>', r.text, re.S):
        ps = re.findall(r"<p[^>]*>(.*?)</p>", li, re.S)
        ref = re.search(r"(BOE-N-\d{4}-\d+)", li)
        href = re.search(r'href="([^"]+)"', li)
        items.append({"boletin": clean(ps[0]) if ps else None,
                      "organismo": clean(ps[1]) if len(ps) > 1 else None,
                      "asunto": clean(ps[2]) if len(ps) > 2 else None,
                      "referencia": ref.group(1) if ref else None,
                      "pdf": "https://www.boe.es" + html.unescape(href.group(1)) if href else None})
    return {"fuente": "TEJU" if judicial else "TEU", "consulta": str(r.url),
            "total": int(total.group(1)) if total else len(items), "resultados": items}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="NIF, nombre o texto libre")
    ap.add_argument("--judicial", action="store_true", help="buscar en el TEJU en vez del TEU")
    ap.add_argument("--desde", default="", help="dd/mm/aaaa")
    ap.add_argument("--hasta", default="", help="dd/mm/aaaa")
    ap.add_argument("--hits", type=int, default=50, choices=[50, 200, 500, 1000, 2000])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        res = buscar(c, a.texto, a.judicial, a.desde, a.hasta, a.hits)

    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"{res['fuente']}: {res['total']} resultados para «{a.texto}»  ({res['consulta']})")
    for i in res["resultados"]:
        print(f"\n- {i['boletin']}\n  {i['organismo']}\n  {i['asunto']}\n  {i['referencia']}  {i['pdf']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
