#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""CENDOJ (CGPJ): búsqueda de jurisprudencia por texto libre.

AVISO LEGAL del propio buscador: la información es de consulta; no está permitido
el uso comercial ni la descarga masiva. Este script hace consultas puntuales, con
pausa entre páginas y un tope de 200 resultados (el mismo que impone la web).

Mecanismo observado en la web (poderjudicial.es, 2026-10):
  GET  /search/indexAN.jsp              -> cookie JSESSIONID
  POST /search/search.action            (XHR, application/x-www-form-urlencoded)
       action=query&databasematch=AN&TEXT=<texto>&start=<n>&recordsPerPage=10
       &sort=IN_FECHARESOLUCION:decreasing  (+ el resto de campos del formulario vacíos;
       el servidor devuelve 500 si faltan y si no va la cabecera X-Requested-With)
  Respuesta HTML: enlaces a /search/AN/openDocument/<hash>/<fecha> con data-roj,
  data-fechares, y un bloque de metadatos (ECLI, nº resolución, municipio, ponente,
  nº recurso) más un resumen automático.

Uso:
  cendoj.py "Iberdrola" --max 20
  cendoj.py "Iberdrola" --json
"""
import argparse
import html
import json
import re
import sys
import time

import httpx

BASE = "https://www.poderjudicial.es/search/"
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept": "*/*",
           "Accept-Language": "es-ES,es;q=0.9", "X-Requested-With": "XMLHttpRequest",
           "Origin": "https://www.poderjudicial.es", "Referer": BASE + "indexAN.jsp"}
EMPTY_FIELDS = ("lastsentences ANYO maxresults page ID_NORMA ARTICULO org ccaa land TIPOINTERES_INSTITUCIONES "
                "INSTITUCION landing landingtype repetitivas FECHAENTRADA TIPORESOLUCION SUBTIPORESOLUCION "
                "SECCIONAUTO SECCION VALUESCOMUNIDAD ROJ ECLI FECHARESOLUCIONDESDE NUMERORESOLUCION "
                "FECHARESOLUCIONHASTA NUMERORECURSO PONENTE IDIOMA NORMA TIPOINTERES_JURIDICO TIPOINTERES_ACTUAL "
                "TIPOINTERES_IGUALDAD TIPOINTERES_DISCAPACIDAD TIPOINTERES_LECTURAFACIL HISTORICOPUBLICO").split()
PAGE = 10


def clean(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s))).strip()


def pagina(client: httpx.Client, texto: str, start: int) -> tuple[int, list[dict]]:
    data = {k: "" for k in EMPTY_FIELDS}
    data.update({"action": "query", "sort": "IN_FECHARESOLUCION:decreasing", "recordsPerPage": PAGE,
                 "databasematch": "AN", "start": start, "TEXT": texto})
    r = client.post(BASE + "search.action", data=data)
    r.raise_for_status()
    total = re.search(r"<b>([\d.]+) resultados</b>", r.text)
    items = []
    for m in re.finditer(r'<a href="(https://www\.poderjudicial\.es/search/AN/openDocument/[^"]+)" id="ref-\d+"[^>]*'
                         r'data-roj="([^"]*)" data-fechares="(\d+)"[^>]*>(.*?)</a>(.*?)(?=<div class="col-xs-12 act|$)',
                         r.text, re.S):
        url, roj, fecha, titulo, resto = m.groups()
        meta = {clean(k): clean(v) for k, v in re.findall(r"<li>\s*([^<]*?):\s*<b>(.*?)</b>", resto, re.S)}
        ecli = re.search(r"<b>(ECLI:[^<]+)</b>", resto)
        resumen = re.search(r'<div class="summary"[^>]*>(.*?)</div>', resto, re.S)
        items.append({"titulo": clean(titulo), "roj": roj, "ecli": ecli.group(1) if ecli else None,
                      "fecha": f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}", "url": url, **meta,
                      "resumen": clean(resumen.group(1)).removeprefix("Resumen Automático:").strip() if resumen else None})
    return int(total.group(1).replace(".", "")) if total else 0, items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto")
    ap.add_argument("--max", type=int, default=10, help="máximo de resultados (tope 200)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    a.max = min(a.max, 200)

    with httpx.Client(headers=HEADERS, timeout=40, follow_redirects=True) as c:
        c.get(BASE + "indexAN.jsp")
        total, items = pagina(c, a.texto, 1)
        while len(items) < min(a.max, total):
            time.sleep(1.5)
            _, more = pagina(c, a.texto, len(items) + 1)
            if not more:
                break
            items.extend(more)
    items = items[:a.max]

    if a.json:
        json.dump({"total": total, "resultados": items}, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"CENDOJ: {total} resultados para «{a.texto}» (mostrando {len(items)})")
    for i in items:
        print(f"\n- {i['titulo']}  [{i.get('ecli')}]\n  Ponente: {i.get('Ponente')}  Recurso: {i.get('Nº Recurso')}"
              f"\n  {i['url']}\n  {(i.get('resumen') or '')[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
