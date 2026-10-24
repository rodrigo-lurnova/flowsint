#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""PLACSP (Plataforma de Contratación del Sector Público): licitaciones en las que una
empresa figura como adjudicataria, usando el buscador público de licitaciones.

Mecanismo observado en la web (contrataciondelestado.es, 2026-10): portal IBM WebSphere
con JSF/MyFaces, con estado en la URL de acción del formulario y en javax.faces.ViewState.
  1. GET  /wps/portal/licitaciones
       -> <form id="viewns_<portlet>_:form1" action="/wps/portal/...">, ViewState
  2. POST <action> con los campos ocultos + <form1>:_idcl=<form1>:linkFormularioBusqueda
       (equivale a myfaces.oam.submitForm(...) del enlace "Licitaciones")
       -> formulario de búsqueda con el campo <form1>:texAdjudicatarioMAQ ("Adjudicatario")
  3. POST <action> con todos los campos del formulario (selects con su valor por defecto),
       texAdjudicatarioMAQ=<nombre> y <form1>:button1=Buscar
       -> <table id="myTablaBusquedaCustom">: expediente, objeto, tipo, estado, importe,
          fecha de presentación, órgano; cada fila trae el enlace permanente
          /wps/poc?uri=deeplink:detalle_licitacion&idEvl=<id>. "Página 1 de N Total: M".
  4. Paginación: POST con el botón "Siguiente >>" (name dinámico, class pagination-button).
El campo Adjudicatario busca por palabras del nombre (≥3 caracteres); NO admite NIF.
Solo cubre los perfiles alojados en la Plataforma (no las plataformas autonómicas agregadas).
Sin CAPTCHA. El script hace una consulta puntual con pausa entre páginas.

Alternativa masiva (no usada): datos abiertos ATOM/CODICE en
https://contrataciondelestado.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom
(12 MB por página y ficheros ZIP mensuales de cientos de MB; útil para análisis masivo, no
para una consulta por empresa).

Uso:
  placsp.py "IBERDROLA CLIENTES" --max 20
  placsp.py "BLINDITEX" --json
"""
import argparse
import html
import json
import re
import sys
import time

import httpx

BASE = "https://contrataciondelestado.es"
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept-Language": "es-ES,es;q=0.9"}


def clean(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _formulario(page: str) -> tuple[str, str, dict]:
    """Devuelve (id del form, action, campos con sus valores por defecto) como lo enviaría el navegador."""
    m = re.search(r'<form id="([^"]+:form1)"[^>]*action="([^"]+)"(.*?)</form>', page, re.S)
    if not m:
        raise RuntimeError("PLACSP: no se encontró el formulario (¿ha cambiado la web?)")
    fid, action, body = m.group(1), html.unescape(m.group(2)), m.group(3)
    data = {}
    for inp in re.finditer(r"<input([^>]*)>", body):
        a = inp.group(1)
        name, typ = re.search(r'name="([^"]+)"', a), re.search(r'type="([^"]+)"', a)
        if not name or (typ and typ.group(1) in ("submit", "button", "image", "checkbox", "radio")):
            continue
        val = re.search(r'value="([^"]*)"', a)
        data[name.group(1)] = html.unescape(val.group(1)) if val else ""
    for sel in re.finditer(r'<select([^>]*)name="([^"]+)"([^>]*)>(.*?)</select>', body, re.S):
        if "multiple" in sel.group(1) + sel.group(3):
            continue
        opt = (re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', sel.group(4))
               or re.search(r'<option[^>]*value="([^"]*)"', sel.group(4)))
        if opt:
            data[sel.group(2)] = html.unescape(opt.group(1))
    return fid, action, data


def _filas(page: str) -> list[dict]:
    tabla = re.search(r'id="myTablaBusquedaCustom".*?<tbody>(.*?)</tbody>', page, re.S)
    if not tabla:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tabla.group(1), re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 6:
            continue
        exp = re.search(r'id="[^"]*textoEnlace_\d+">(.*?)</span>', tds[0], re.S)
        det = re.search(r"href='(https://contrataciondelestado\.es/wps/poc\?uri=deeplink:detalle_licitacion[^']+)'", tds[0])
        objeto = re.findall(r"<div>(.*?)</div>", tds[0], re.S)
        perfil = re.search(r"href='([^']*deeplink:perfilContratante[^']*)'", tds[5])
        tipo = [clean(x) for x in re.findall(r"<div>(.*?)</div>", tds[1], re.S) if clean(x)]
        importe = clean(tds[3]).replace(".", "").replace(",", ".")
        out.append({"expediente": clean(exp.group(1)) if exp else None,
                    "objeto": clean(objeto[-1]) if objeto else None,
                    "tipo": " / ".join(tipo), "estado": clean(tds[2]),
                    "importe": float(importe) if re.fullmatch(r"-?\d+(\.\d+)?", importe) else None,
                    "fecha_presentacion": clean(tds[4]) or None,
                    "organo": clean(tds[5]),
                    "url": html.unescape(det.group(1)) if det else None,
                    "url_organo": html.unescape(perfil.group(1)) if perfil else None})
    return out


def buscar(client: httpx.Client, adjudicatario: str, max_items: int = 25, pausa: float = 1.5) -> dict:
    if not re.search(r"[A-Za-z0-9]{3}", adjudicatario):
        raise ValueError("El adjudicatario debe tener al menos una palabra de 3 caracteres")
    page = client.get(BASE + "/wps/portal/licitaciones").text
    fid, action, data = _formulario(page)
    data[f"{fid}:_idcl"] = f"{fid}:linkFormularioBusqueda"
    page = client.post(BASE + action, data=data).text

    fid, action, data = _formulario(page)
    data[f"{fid}:texAdjudicatarioMAQ"] = adjudicatario
    data[f"{fid}:button1"] = "Buscar"
    r = client.post(BASE + action, data=data)
    r.raise_for_status()
    page = r.text
    m = re.search(r"Total:\s*([\d.]+)", clean(page))
    total = int(m.group(1).replace(".", "")) if m else 0
    items = _filas(page)
    while len(items) < min(max_items, total):
        sig = re.search(r'<input[^>]*name="([^"]+)"[^>]*value="Siguiente &gt;&gt;"', page)
        if not sig:
            break
        time.sleep(pausa)
        fid, action, data = _formulario(page)
        data[sig.group(1)] = "Siguiente >>"
        page = client.post(BASE + action, data=data).text
        nuevos = _filas(page)
        if not nuevos:
            break
        items.extend(nuevos)
    return {"consulta": adjudicatario, "total": total, "resultados": items[:max_items]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adjudicatario", help="nombre (o parte) del adjudicatario; no admite NIF")
    ap.add_argument("--max", type=int, default=20, help="máximo de licitaciones (20 por página)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as c:
        res = buscar(c, a.adjudicatario, a.max)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"PLACSP: {res['total']} licitaciones con adjudicatario «{a.adjudicatario}» (mostrando {len(res['resultados'])})")
    for i in res["resultados"]:
        imp = f"{i['importe']:,.2f} €" if i["importe"] is not None else "—"
        print(f"\n- {i['expediente']}  [{i['estado']}]  {imp}  {i['tipo']}\n  {i['objeto']}\n  Órgano: {i['organo']}\n  {i['url']}")
    return 0 if res["resultados"] else 1


if __name__ == "__main__":
    sys.exit(main())
