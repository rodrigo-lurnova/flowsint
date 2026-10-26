#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Apify (apify.com): ejecuta actores públicos y devuelve los items del dataset.

Requiere APIFY_TOKEN (variable de entorno). Sin token, las funciones lanzan
SinToken y el CLI sale con un mensaje claro: no se hace ninguna petición.

AVISO: los actores de LinkedIn extraen datos de linkedin.com, cuyas Condiciones
de uso prohíben el scraping. Usarlos es decisión y responsabilidad de quien
ejecuta la herramienta (base legal RGPD: interés legítimo / due diligence, con
minimización: solo personas vinculadas a la empresa investigada). Este script no
usa cookies ni cuentas de LinkedIn: los actores elegidos son «no cookies».

Mecanismo (API v2, 2026-10):
  POST https://api.apify.com/v2/acts/<usuario>~<actor>/run-sync-get-dataset-items
       ?timeout=<s>&maxItems=<n>&maxTotalChargeUsd=<usd>
       Authorization: Bearer <APIFY_TOKEN>      body = input JSON del actor
  -> 201 con la lista de items (ejecución síncrona, máx. 300 s).
  maxTotalChargeUsd limita el gasto de actores «pay per event» (APIFY_MAX_USD, 0.10 por defecto).
  Catálogo y esquemas de input son públicos sin token:
    GET /v2/store?search=...   GET /v2/acts/<id>   GET /v2/actor-builds/<buildId>

Actores por defecto (precio plan FREE/BRONZE, Apify Store 2026-10; varían por plan):
  linkedin_buscar   harvestapi/linkedin-profile-search-by-name   (4.45★, 2.5k usuarios/mes)
                    input {firstName,lastName,strictSearch,maxPages,currentCompanies?,locations?,profileScraperMode:"Short"}
                    ~$0.004 por página de búsqueda (hasta 25 perfiles) + $0.002 por perfil «main» → una
                    búsqueda típica de 1 página ≈ $0.01–0.06
  linkedin_perfil   harvestapi/linkedin-profile-scraper          (4.63★, 14k usuarios/mes)
                    input {urls:[...]}  ~$0.004/perfil ($0.01 con búsqueda de email)
  linkedin_empleados harvestapi/linkedin-company-employees       (4.0★, 5.6k usuarios/mes)
                    input {companies:[url empresa LinkedIn], maxItems, searchQuery?, jobTitles?,
                    profileScraperMode:"Short ($4 per 1k)"}  $0.02 por arranque + ~$0.003–0.004/perfil
  google            apify/google-search-scraper                  (4.61★, 25k usuarios/mes)
                    input {queries:"q1\\nq2", maxPagesPerQuery:1, countryCode:"es", languageCode:"es"}
                    ~$0.0045 por página de resultados (alternativa a SearXNG para dorks)
Todos se pueden sustituir por otro actor con --actor / APIFY_ACTOR_<CLAVE>.

Uso:
  apify_actores.py linkedin-buscar "Rodrigo Martínez" --empresa Lurnova
  apify_actores.py linkedin-perfil https://www.linkedin.com/in/xxxx
  apify_actores.py linkedin-empleados https://www.linkedin.com/company/lurnova --max 10
  apify_actores.py google 'site:linkedin.com/in "Rodrigo Martínez" Lurnova'
  apify_actores.py run apify/google-search-scraper --input '{"queries":"lurnova"}'
"""
import argparse
import json
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from personas_comun import coincidencia_nombre, normalizar  # noqa: E402

API = "https://api.apify.com/v2"
ACTORES = {
    "linkedin_buscar": "harvestapi/linkedin-profile-search-by-name",
    "linkedin_perfil": "harvestapi/linkedin-profile-scraper",
    "linkedin_empleados": "harvestapi/linkedin-company-employees",
    "google": "apify/google-search-scraper",
}
AVISO_TOS = ("Los actores de LinkedIn rascan linkedin.com (sus ToS lo prohíben); su uso es decisión y "
             "responsabilidad del usuario.")


class SinToken(RuntimeError):
    pass


def actor(clave: str) -> str:
    return os.getenv(f"APIFY_ACTOR_{clave.upper()}", ACTORES[clave])


def ejecutar(actor_id: str, entrada: dict, token: str | None = None, max_items: int | None = None,
             max_usd: float | None = None, timeout: int = 240) -> list[dict]:
    """Ejecuta un actor en modo síncrono y devuelve los items del dataset."""
    token = token or os.getenv("APIFY_TOKEN")
    if not token:
        raise SinToken("Falta APIFY_TOKEN: integración Apify desactivada (se omite).")
    max_usd = max_usd if max_usd is not None else float(os.getenv("APIFY_MAX_USD", "0.10"))
    params = {"timeout": timeout, "maxTotalChargeUsd": max_usd}
    if max_items:
        params["maxItems"] = max_items
    url = f"{API}/acts/{actor_id.replace('/', '~')}/run-sync-get-dataset-items"
    hdr = {"Authorization": f"Bearer {token}"}
    r = httpx.post(url, params=params, json=entrada, headers=hdr, timeout=timeout + 30)
    if r.status_code == 400 and "max-total-charge-usd-below-minimum" in r.text:
        # Algunos actores (p. ej. google-search-scraper) exigen un tope mínimo; el cobro real sigue siendo por uso
        m = re.search(r"minimum of \$([\d.]+)", r.text)
        params["maxTotalChargeUsd"] = float(m.group(1)) if m else 0.5
        r = httpx.post(url, params=params, json=entrada, headers=hdr, timeout=timeout + 30)
    if r.status_code >= 400:
        raise RuntimeError(f"Apify {actor_id}: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    return data if isinstance(data, list) else data.get("items", [])


# ------------------------------------------------------------------ normalización de perfiles

def _empresa_actual(p: dict) -> tuple[str | None, str | None]:
    for k in ("currentPosition", "currentPositions", "positions", "experience"):
        v = p.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            x = v[0]
            emp = x.get("companyName") or x.get("company")
            if isinstance(emp, dict):
                emp = emp.get("name")
            return emp, x.get("position") or x.get("title")
    return p.get("companyName"), p.get("jobTitle")


def nombre_apellidos(nombre: str) -> tuple[str, str]:
    """Separa nombre de pila y apellidos (con tildes originales). Admite «APELLIDOS, NOMBRE»."""
    if "," in nombre:
        ap, nom = nombre.split(",", 1)
        return nom.strip().title(), ap.strip().title()
    toks = nombre.split()
    if len(toks) >= 4:
        return " ".join(toks[:2]), " ".join(toks[2:])
    return (toks[0], " ".join(toks[1:])) if toks else (nombre, "")


def perfil_linkedin(p: dict) -> dict:
    """Item de un actor harvestapi (u otro parecido) -> dict común para SocialAccount."""
    url = p.get("linkedinUrl") or p.get("url") or p.get("profileUrl")
    if not url and p.get("publicIdentifier"):
        url = f"https://www.linkedin.com/in/{p['publicIdentifier']}"
    loc = p.get("location")
    if isinstance(loc, dict):
        loc = loc.get("linkedinText") or (loc.get("parsed") or {}).get("text")
    empresa, cargo = _empresa_actual(p)
    nombre = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x) or p.get("fullName") or p.get("name")
    foto = p.get("photo") or p.get("profilePicture") or p.get("pictureUrl")
    if isinstance(foto, dict):
        foto = foto.get("url")
    return {"plataforma": "linkedin", "url": url, "usuario": p.get("publicIdentifier"),
            "nombre": nombre, "titular": p.get("headline"), "ubicacion": loc, "empresa": empresa, "cargo": cargo,
            "foto": foto, "seguidores": p.get("followerCount") or p.get("followersCount"),
            "conexiones": p.get("connectionsCount"), "fuente": "apify", "bruto_id": p.get("id")}


def puntuar(perfil: dict, nombre: str, empresa: str | None) -> tuple[float, list[str]]:
    ev, s = [], 0.0
    cn = coincidencia_nombre(nombre, perfil.get("nombre") or "")
    s += 0.5 * cn
    ev.append(f"nombre {cn:.0%}")
    if empresa:
        ne = normalizar(empresa)
        txt = normalizar(" ".join(str(perfil.get(k) or "") for k in ("empresa", "titular", "cargo")))
        if ne and ne in txt:
            s += 0.4
            ev.append(f"empresa «{empresa}» en el perfil")
    if perfil.get("ubicacion") and any(k in normalizar(perfil["ubicacion"]) for k in ("espana", "spain")):
        s += 0.05
        ev.append("ubicación en España")
    return round(min(s, 0.99), 2), ev


def linkedin_buscar(nombre: str, empresa: str | None = None, empresa_url: str | None = None,
                    ubicacion: str | None = None, max_items: int = 10, token: str | None = None) -> list[dict]:
    """Busca perfiles por nombre (+ filtro de empresa si se da su URL de LinkedIn). Puntúa por empresa."""
    first, last = nombre_apellidos(nombre)
    entrada = {"firstName": first, "lastName": last, "strictSearch": False, "maxPages": 1,
               "profileScraperMode": "Short", "maxItems": max_items}
    if empresa_url:
        entrada["currentCompanies"] = [empresa_url]
    if ubicacion:
        entrada["locations"] = [ubicacion]
    items = ejecutar(actor("linkedin_buscar"), entrada, token, max_items=max_items)
    out = []
    for it in items:
        p = perfil_linkedin(it)
        if not p["url"]:
            continue
        p["confianza"], p["evidencias"] = puntuar(p, nombre, empresa)
        if empresa_url:
            p["confianza"] = min(0.99, p["confianza"] + 0.3)
            p["evidencias"].append("filtrado por empresa actual en LinkedIn")
        p["evidencias"].append(f"Apify {actor('linkedin_buscar')}")
        out.append(p)
    return sorted(out, key=lambda x: -x["confianza"])


def linkedin_perfil(urls: list[str], token: str | None = None) -> list[dict]:
    items = ejecutar(actor("linkedin_perfil"), {"urls": urls}, token, max_items=len(urls))
    return [{**perfil_linkedin(i), "bruto": i} for i in items]


def linkedin_empleados(empresa_url: str, max_items: int = 25, busqueda: str | None = None,
                       cargos: list[str] | None = None, token: str | None = None) -> list[dict]:
    entrada = {"companies": [empresa_url], "maxItems": max_items, "profileScraperMode": "Short ($4 per 1k)"}
    if busqueda:
        entrada["searchQuery"] = busqueda
    if cargos:
        entrada["jobTitles"] = cargos
    return [perfil_linkedin(i) for i in ejecutar(actor("linkedin_empleados"), entrada, token, max_items=max_items)]


def google(consultas: list[str], token: str | None = None, pais: str = "es") -> list[dict]:
    """Resultados orgánicos [{url,titulo,snippet,consulta}] vía apify/google-search-scraper."""
    items = ejecutar(actor("google"), {"queries": "\n".join(consultas), "maxPagesPerQuery": 1,
                                       "countryCode": pais, "languageCode": "es"}, token)
    out = []
    for it in items:
        q = (it.get("searchQuery") or {}).get("term")
        for o in it.get("organicResults", []):
            out.append({"url": o.get("url"), "titulo": o.get("title"), "snippet": o.get("description"),
                        "consulta": q, "motor": "google(apify)"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("linkedin-buscar")
    b.add_argument("nombre")
    b.add_argument("--empresa")
    b.add_argument("--empresa-url", help="https://www.linkedin.com/company/<x> (filtro exacto)")
    b.add_argument("--ubicacion")
    b.add_argument("--max", type=int, default=10)
    p = sub.add_parser("linkedin-perfil")
    p.add_argument("urls", nargs="+")
    e = sub.add_parser("linkedin-empleados")
    e.add_argument("empresa_url")
    e.add_argument("--max", type=int, default=25)
    e.add_argument("--busqueda")
    g = sub.add_parser("google")
    g.add_argument("consultas", nargs="+")
    r = sub.add_parser("run")
    r.add_argument("actor")
    r.add_argument("--input", default="{}")
    r.add_argument("--max", type=int)
    for s in (b, p, e, g, r):
        s.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        if a.cmd == "linkedin-buscar":
            res = linkedin_buscar(a.nombre, a.empresa, a.empresa_url, a.ubicacion, a.max)
        elif a.cmd == "linkedin-perfil":
            res = linkedin_perfil(a.urls)
        elif a.cmd == "linkedin-empleados":
            res = linkedin_empleados(a.empresa_url, a.max, a.busqueda)
        elif a.cmd == "google":
            res = google(a.consultas)
        else:
            res = ejecutar(a.actor, json.loads(a.input), max_items=a.max)
    except SinToken as ex:
        print(str(ex), file=sys.stderr)
        return 2
    if a.cmd.startswith("linkedin"):
        print("AVISO: " + AVISO_TOS, file=sys.stderr)
    if a.json or a.cmd == "run":
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 0
    for x in res:
        if "url" in x and "titulo" in x:
            print(f"- {x['titulo']}\n  {x['url']}\n  {(x.get('snippet') or '')[:160]}")
        else:
            print(f"- {x.get('confianza', '')} {x.get('nombre')} | {x.get('titular')} | {x.get('ubicacion')}\n  {x.get('url')}"
                  + (f"\n  {', '.join(x.get('evidencias', []))}" if x.get("evidencias") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
