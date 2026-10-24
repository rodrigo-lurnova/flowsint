#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""apispain.es: API JSON de datos públicos españoles (BORME, BOE, BDNS, PLACE, AEAT).

Mecanismo observado (api.apispain.es, 2026-10):
  - Especificación OpenAPI 3 en https://api.apispain.es/docs/openapi.json
    (la página /docs es Scalar y carga ese fichero; no está en /openapi.json).
  - Auth: cabecera `Authorization: Bearer <api_key>` en todas las rutas de datos.
    Sin clave -> 401 {"error":"Unauthorized"}. La clave se obtiene en apispain.es
    (alta con email; este script NO crea cuentas). Variable: APISPAIN_API_KEY.
  - Planes (según la propia spec): Free 100 pet./mes y 10/min; Starter 300/mes,
    60/min; Pro 1.000/mes, 100/min; Business a medida. Cabeceras de consumo:
    X-Plan, X-RateLimit-Remaining-Minute, X-Apispain-Credits-Remaining...
  - Paginación por cursor: {"data": [...], "pagination": {"nextCursor": ...}} ->
    se reenvía tal cual en ?cursor=.
  - BORME: /v1/borme/empresas?nombre=  (búsqueda; si no hay datos BORME hace
    fallback a InfoNIF), /v1/borme/empresas/{nif} (ficha + últimos 20 actos desde
    2024; ?historico=true consume 1 crédito de historial), /{nif}/actos,
    /{nif}/cambios?desde=, /{nif}/scoring, /v1/borme/actos/{AAAA-MM-DD}.
    Cada acto trae `personas: [{cargo, nombre, accion: nombramiento|cese|vigente}]`.
  - BOE: /v1/boe/publicaciones?q=  y /v1/boe/publicaciones/{fecha}
  - BDNS: /v1/bdns/subvenciones?q=&ccaa=&sector=&abiertas=&importeMin=&importeMax=
  - PLACE: /v1/place/licitaciones?q=&cpv=&organo=&provincia=&abiertas=, /{id}
  - AEAT: /v1/aeat/deudores/{nif} (listado art. 95 bis LGT)
  - Públicas sin clave: /health, /v1/public/organos?q=, /v1/public/organos/{slug}

Uso:
  apispain.py health
  apispain.py organos "ayuntamiento de madrid"          # sin clave
  APISPAIN_API_KEY=... apispain.py buscar "LURNOVA" --json
  APISPAIN_API_KEY=... apispain.py empresa B12345674
  APISPAIN_API_KEY=... apispain.py actos B12345674 --max 100
  APISPAIN_API_KEY=... apispain.py borme-dia 2026-10-23
  APISPAIN_API_KEY=... apispain.py boe --q "subvenciones digitalización"
  APISPAIN_API_KEY=... apispain.py bdns --q digitalizacion --abiertas
  APISPAIN_API_KEY=... apispain.py licitaciones --q limpieza --provincia Madrid
"""
import argparse
import json
import os
import sys

import httpx

BASE = "https://api.apispain.es"
HEADERS = {"Accept": "application/json", "User-Agent": "osint-es-scripts (apispain.py)"}
ENV_KEY = "APISPAIN_API_KEY"
PUBLIC_PATHS = ("/health", "/v1/public/")


class ApispainError(RuntimeError):
    pass


class Apispain:
    """Cliente mínimo. `api_key=None` lee APISPAIN_API_KEY; las rutas públicas no la necesitan."""

    def __init__(self, api_key: str | None = None, timeout: float = 30, base: str = BASE):
        self.api_key = api_key if api_key is not None else os.getenv(ENV_KEY)
        self.client = httpx.Client(base_url=base, headers=HEADERS, timeout=timeout)
        self.last_usage: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def close(self):
        self.client.close()

    def get(self, path: str, params: dict | None = None) -> dict:
        headers = {}
        if not path.startswith(PUBLIC_PATHS):
            if not self.api_key:
                raise ApispainError(
                    f"Falta la clave de apispain.es: exporta {ENV_KEY}=<clave> (se obtiene en "
                    "https://apispain.es; plan Free = 100 peticiones/mes).")
            headers["Authorization"] = f"Bearer {self.api_key}"
        params = {k: v for k, v in (params or {}).items() if v not in (None, "", False)}
        params = {k: ("true" if v is True else v) for k, v in params.items()}
        r = self.client.get(path, params=params, headers=headers)
        self.last_usage = {k.lower(): v for k, v in r.headers.items()
                           if k.lower().startswith(("x-plan", "x-ratelimit", "x-apispain"))}
        if r.status_code == 401:
            raise ApispainError("401: clave de apispain.es ausente o no válida.")
        if r.status_code == 429:
            raise ApispainError(f"429: límite de apispain.es alcanzado ({self.last_usage}).")
        if r.status_code == 404:
            return {"data": None, "error": "no encontrado", "status": 404}
        if r.status_code >= 400:
            raise ApispainError(f"{r.status_code}: {r.text[:300]}")
        return r.json()

    def paginar(self, path: str, params: dict | None = None, max_items: int = 100) -> list:
        out, cursor = [], None
        params = dict(params or {})
        while len(out) < max_items:
            params.update({"limit": min(100, max_items - len(out)), "cursor": cursor})
            data = self.get(path, params)
            items = data.get("data") or []
            out.extend(items)
            cursor = (data.get("pagination") or {}).get("nextCursor")
            if not cursor or not items:
                break
        return out[:max_items]

    # --- Públicas -------------------------------------------------------------------
    def health(self) -> dict:
        return self.get("/health")

    def organos(self, q: str) -> dict:
        return self.get("/v1/public/organos", {"q": q})

    def organo(self, slug: str) -> dict:
        return self.get(f"/v1/public/organos/{slug}")

    # --- Cuenta ---------------------------------------------------------------------
    def me(self) -> dict:
        return self.get("/v1/me")

    # --- BORME ----------------------------------------------------------------------
    def buscar_empresas(self, nombre: str | None = None, provincia: str | None = None,
                        max_items: int = 20) -> list:
        return self.paginar("/v1/borme/empresas", {"nombre": nombre, "provincia": provincia}, max_items)

    def empresa(self, nif: str, historico: bool = False) -> dict:
        return self.get(f"/v1/borme/empresas/{nif}", {"historico": historico})

    def actos(self, nif: str, max_items: int = 100) -> list:
        return self.paginar(f"/v1/borme/empresas/{nif}/actos", {}, max_items)

    def cambios(self, nif: str, desde: str) -> dict:
        return self.get(f"/v1/borme/empresas/{nif}/cambios", {"desde": desde})

    def scoring(self, nif: str) -> dict:
        return self.get(f"/v1/borme/empresas/{nif}/scoring")

    def borme_dia(self, fecha: str) -> dict:
        return self.get(f"/v1/borme/actos/{fecha}")

    def deudores_aeat(self, nif: str) -> dict:
        return self.get(f"/v1/aeat/deudores/{nif}")

    # --- BOE / BDNS / PLACE ---------------------------------------------------------
    def boe(self, q: str | None = None, fecha: str | None = None, max_items: int = 20):
        if fecha:
            return self.get(f"/v1/boe/publicaciones/{fecha}").get("data")
        return self.paginar("/v1/boe/publicaciones", {"q": q}, max_items)

    def subvenciones(self, max_items: int = 20, **filtros) -> list:
        return self.paginar("/v1/bdns/subvenciones", filtros, max_items)

    def licitaciones(self, max_items: int = 20, **filtros) -> list:
        return self.paginar("/v1/place/licitaciones", filtros, max_items)

    def licitacion(self, id_: str) -> dict:
        return self.get(f"/v1/place/licitaciones/{id_}")


def personas_de_actos(actos: list[dict]) -> list[dict]:
    """Aplana `personas` de una lista de ActoBORME de apispain (formato común con borme.py)."""
    out = []
    for a in actos or []:
        for p in a.get("personas") or []:
            out.append({"fecha": (a.get("fechaPublicacion") or "")[:10], "accion": p.get("accion"),
                        "cargo": p.get("cargo"), "persona": p.get("nombre"), "borme_id": a.get("bormeId")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="salida JSON (por defecto ya es JSON indentado)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health", help="estado del servicio (público)")
    p = sub.add_parser("organos", help="buscar órganos de contratación (público)"); p.add_argument("q")
    p = sub.add_parser("organo", help="ficha pública de un órgano (público)"); p.add_argument("slug")
    sub.add_parser("me", help="cuenta y consumo")
    p = sub.add_parser("buscar", help="buscar empresas BORME por nombre")
    p.add_argument("nombre"); p.add_argument("--provincia"); p.add_argument("--max", type=int, default=20)
    p = sub.add_parser("empresa", help="ficha BORME por NIF")
    p.add_argument("nif"); p.add_argument("--historico", action="store_true",
                                         help="historial completo 2009-hoy (CONSUME 1 crédito de historial)")
    p = sub.add_parser("actos", help="actos BORME por NIF"); p.add_argument("nif")
    p.add_argument("--max", type=int, default=100)
    p = sub.add_parser("cambios", help="cambios desde fecha"); p.add_argument("nif"); p.add_argument("--desde", required=True)
    p = sub.add_parser("scoring", help="score de riesgo"); p.add_argument("nif")
    p = sub.add_parser("deudores", help="listado AEAT de deudores"); p.add_argument("nif")
    p = sub.add_parser("borme-dia", help="actos BORME de una fecha AAAA-MM-DD"); p.add_argument("fecha")
    p = sub.add_parser("boe", help="publicaciones BOE"); p.add_argument("--q"); p.add_argument("--fecha")
    p.add_argument("--max", type=int, default=20)
    p = sub.add_parser("bdns", help="convocatorias de subvenciones")
    for f in ("q", "ccaa", "sector"):
        p.add_argument(f"--{f}")
    p.add_argument("--abiertas", action="store_true"); p.add_argument("--importe-min", type=float)
    p.add_argument("--importe-max", type=float); p.add_argument("--max", type=int, default=20)
    p = sub.add_parser("licitaciones", help="licitaciones PLACE")
    for f in ("q", "cpv", "organo", "provincia"):
        p.add_argument(f"--{f}")
    p.add_argument("--abiertas", action="store_true"); p.add_argument("--importe-min", type=float)
    p.add_argument("--importe-max", type=float); p.add_argument("--max", type=int, default=20)
    p = sub.add_parser("licitacion", help="licitación por id"); p.add_argument("id")
    a = ap.parse_args()

    try:
        with Apispain() as api:
            c = a.cmd
            if c == "health": res = api.health()
            elif c == "organos": res = api.organos(a.q)
            elif c == "organo": res = api.organo(a.slug)
            elif c == "me": res = api.me()
            elif c == "buscar": res = api.buscar_empresas(a.nombre, a.provincia, a.max)
            elif c == "empresa": res = api.empresa(a.nif, a.historico)
            elif c == "actos": res = api.actos(a.nif, a.max)
            elif c == "cambios": res = api.cambios(a.nif, a.desde)
            elif c == "scoring": res = api.scoring(a.nif)
            elif c == "deudores": res = api.deudores_aeat(a.nif)
            elif c == "borme-dia": res = api.borme_dia(a.fecha)
            elif c == "boe": res = api.boe(a.q, a.fecha, a.max)
            elif c == "bdns":
                res = api.subvenciones(a.max, q=a.q, ccaa=a.ccaa, sector=a.sector, abiertas=a.abiertas,
                                       importeMin=a.importe_min, importeMax=a.importe_max)
            elif c == "licitaciones":
                res = api.licitaciones(a.max, q=a.q, cpv=a.cpv, organo=a.organo, provincia=a.provincia,
                                       abiertas=a.abiertas, importeMin=a.importe_min, importeMax=a.importe_max)
            elif c == "licitacion": res = api.licitacion(a.id)
            if api.last_usage:
                print(f"[apispain] consumo: {api.last_usage}", file=sys.stderr)
    except ApispainError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
