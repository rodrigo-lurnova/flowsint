#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""BDNS (Base de Datos Nacional de Subvenciones): ayudas concedidas a una empresa.

Mecanismo observado en la web (infosubvenciones.es, 2026-10):
  1. GET /bdnstrans/api/terceros?ambito=C&busqueda=<NIF o nombre>&vpd=GE
       -> {"terceros": [{"id": 22139267, "descripcion": "B26613679 - BLINDITEX, S.L."}]}
  2. GET /bdnstrans/api/concesiones/busqueda?vpd=GE&beneficiario=<id>&page=0&pageSize=100
         &order=fechaConcesion&direccion=desc
       -> {"content": [...], "totalElements": N}
El parámetro `beneficiario` solo admite el id interno, no el NIF.

Uso:
  bdns.py B26613679
  bdns.py "BLINDITEX" --max 500 --json
"""
import argparse
import json
import sys

import httpx

BASE = "https://www.infosubvenciones.es/bdnstrans/api"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (osint-es-scripts)"}


def buscar_terceros(client: httpx.Client, texto: str) -> list[dict]:
    r = client.get(f"{BASE}/terceros", params={"ambito": "C", "busqueda": texto, "vpd": "GE"})
    r.raise_for_status()
    return r.json().get("terceros", [])


def concesiones(client: httpx.Client, id_persona: int, max_items: int) -> list[dict]:
    out, page, size = [], 0, min(100, max_items)
    while len(out) < max_items:
        r = client.get(
            f"{BASE}/concesiones/busqueda",
            params={"vpd": "GE", "beneficiario": id_persona, "page": page, "pageSize": size,
                    "order": "fechaConcesion", "direccion": "desc"},
        )
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("content", []))
        if data.get("last", True):
            break
        page += 1
    return out[:max_items]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="NIF o nombre (mínimo 3 caracteres)")
    ap.add_argument("--max", type=int, default=100, help="máximo de concesiones por beneficiario")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    a = ap.parse_args()

    with httpx.Client(headers=HEADERS, timeout=30) as c:
        terceros = buscar_terceros(c, a.texto)
        if not terceros:
            print("Sin coincidencias en BDNS.", file=sys.stderr)
            return 1
        result = []
        for t in terceros:
            items = concesiones(c, t["id"], a.max)
            result.append({"id": t["id"], "beneficiario": t["descripcion"], "concesiones": items,
                           "total_importe": sum(i.get("importe") or 0 for i in items)})

    if a.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    for r in result:
        print(f"\n== {r['beneficiario']}  (id BDNS {r['id']})  {len(r['concesiones'])} concesiones, "
              f"{r['total_importe']:,.2f} €")
        for i in r["concesiones"]:
            print(f"  {i.get('fechaConcesion')}  {i.get('importe') or 0:>12,.2f} €  {i.get('nivel2')} / "
                  f"{i.get('nivel3')}\n      {i.get('convocatoria')}\n      {i.get('urlBR') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
