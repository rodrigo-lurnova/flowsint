#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Ficha de actividad pública de una empresa: encadena resolver_empresa.py con el resto de
scripts (BDNS, CNMV, BOE TEU/TEJU, CENDOJ, PLACSP, TED y sanciones UE).

Flujo:
  1. resolver_empresa.resolver(texto) -> candidatos con NIF/LEI.
  2. Si hay un único candidato con coincidencia exacta (o se pasa --nif), se usa su NIF en las
     fuentes que lo admiten (BDNS, TEU/TEJU, TED, sanciones) y su denominación en las que solo
     buscan por texto (PLACSP, CENDOJ, CNMV). Si hay homonimia, NO elige: consulta por el texto
     dado y lo deja anotado en "avisos".
  3. Cada fuente se consulta una vez, con tope de resultados (--max), y un fallo en una fuente
     no detiene las demás. CENDOJ respeta su aviso legal: consulta puntual, máx. 200.

Uso:
  ficha_empresa.py "BLINDITEX"
  ficha_empresa.py "Iberdrola" --nif A48010615 --fuentes cnmv,ted,cendoj --json
"""
import argparse
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolver_empresa  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept-Language": "es-ES,es;q=0.9"}
FUENTES = ("bdns", "cnmv", "teu", "teju", "cendoj", "placsp", "ted", "sanciones")


def elegir_identidad(res: dict, nif: str | None = None) -> tuple[str | None, str | None, list[str]]:
    """(nif, denominación) del candidato inequívoco, o (None, None) si hay duda."""
    avisos = []
    if nif:
        nif = resolver_empresa.limpiar_nif(nif)
        c = next((c for c in res["candidatos"] if c["nif"] == nif), None)
        return nif, (resolver_empresa.mejor_denominacion(c) if c else None), avisos
    exactos = [c for c in res["candidatos"] if c["coincidencia"] == "exacta" and c["nif"]]
    if len(exactos) == 1:
        c = exactos[0]
        return c["nif"], resolver_empresa.mejor_denominacion(c, res["consulta"]), avisos
    if len(exactos) > 1:
        avisos.append("Homonimia: varios NIF con la misma denominación; se consulta por texto. Usa --nif.")
    else:
        avisos.append("Sin candidato exacto: se consulta por el texto dado.")
    return None, None, avisos


def nif_cnmv(nif: str) -> str:
    """La ficha de la CNMV solo responde con el NIF en formato A-48010615."""
    return f"{nif[0]}-{nif[1:]}" if nif and "-" not in nif else nif


def consultar(fuente: str, nif: str | None, nombre: str, max_items: int):
    """Consulta una fuente con el cliente y las cabeceras de su propio script."""
    import bdns, boe_edictos, cendoj, cnmv, placsp, sanciones_ue, ted  # noqa: E401
    mod = {"bdns": bdns, "cnmv": cnmv, "teu": boe_edictos, "teju": boe_edictos, "cendoj": cendoj,
           "placsp": placsp, "ted": ted, "sanciones": sanciones_ue}[fuente]
    with httpx.Client(headers=mod.HEADERS, timeout=60, follow_redirects=True) as client:
        if fuente == "bdns":
            terceros = bdns.buscar_terceros(client, nif or nombre)
            if not nif:  # por nombre: descartar beneficiarios que solo empiezan igual (LURNOVA ≠ LURNOVARO)
                terceros = [t for t in terceros if resolver_empresa.coincidencia(
                    nombre, t["descripcion"].partition(" - ")[2]) != "parcial"]
            return [{"id": t["id"], "beneficiario": t["descripcion"],
                     "concesiones": bdns.concesiones(client, t["id"], max_items)} for t in terceros[:5]]
        if fuente == "cnmv":
            ents = [{"nif": nif_cnmv(nif)}] if nif else cnmv.buscar(client, nombre)[:5]
            return [f for f in (cnmv.ficha(client, e["nif"]) for e in ents) if f["secciones"]]
        if fuente in ("teu", "teju"):
            r = boe_edictos.buscar(client, nif or nombre, fuente == "teju", "", "", 50)
            r["resultados"] = r["resultados"][:max_items]
            return r
        if fuente == "cendoj":
            client.get(cendoj.BASE + "indexAN.jsp")
            total, items = cendoj.pagina(client, f'"{nombre}"' if " " in nombre else nombre, 1)
            return {"total": total, "resultados": items[:max_items]}
        if fuente == "placsp":
            return placsp.buscar(client, nombre, max_items)
        if fuente == "ted":
            return ted.buscar(client, nif or nombre, max_items)
        return sanciones_ue.buscar(client, nif or nombre, max_items)


def ficha(texto: str, nif: str | None = None, fuentes=FUENTES, max_items: int = 10, web: bool = True) -> dict:
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as c:
        ident = resolver_empresa.resolver(nif or texto, web=web, client=c)
        nif_ok, nombre, avisos = elegir_identidad(ident, nif)
        # Las fuentes de texto libre buscan mejor sin la forma jurídica ("IBERDROLA", no "IBERDROLA SA").
        nombre = resolver_empresa.normalizar(nombre or texto)["base"] or texto
        out = {"consulta": texto, "nif": nif_ok, "denominacion": nombre, "identidad": ident,
               "avisos": ident["avisos"] + avisos, "fuentes": {}}
        for f in fuentes:
            try:
                out["fuentes"][f] = consultar(f, nif_ok, nombre, max_items)
            except Exception as e:
                out["fuentes"][f] = None
                out["avisos"].append(f"{f}: {type(e).__name__}: {e}"[:200])
    return out


def _n(v) -> int:
    if v is None:
        return 0
    if isinstance(v, dict):
        return len(v.get("resultados", []))
    return sum(len(x.get("concesiones", x.get("secciones", []))) for x in v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="denominación o NIF")
    ap.add_argument("--nif", help="fija el NIF (resuelve homonimias)")
    ap.add_argument("--fuentes", default=",".join(FUENTES), help=f"subconjunto de {','.join(FUENTES)}")
    ap.add_argument("--max", type=int, default=10, help="máximo de resultados por fuente")
    ap.add_argument("--sin-web", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    res = ficha(a.texto, a.nif, [f for f in a.fuentes.split(",") if f in FUENTES], a.max, not a.sin_web)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(f"== {res['denominacion']}  NIF {res['nif'] or '¿?'}")
    for c in res["identidad"]["candidatos"][:5]:
        print(f"   candidato [{c['coincidencia']}] {c['nif'] or '—'} LEI {c['lei'] or '—'}  {c['denominaciones'][:1]}")
    for w in res["identidad"]["webs"][:3]:
        print(f"   web [{w['confianza']}] {w['dominio']}")
    for f, v in res["fuentes"].items():
        print(f"   {f:10} {_n(v)} resultados" if v is not None else f"   {f:10} error")
    for av in res["avisos"]:
        print(f"! {av}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
