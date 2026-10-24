#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Perfiles públicos en redes de una persona vinculada a una empresa (opcional).

Solo para due diligence de personas con relación documentada con la empresa
investigada (administradores, fundadores, empleados): exige `--empresa`. No entra
en las redes: busca en motores de búsqueda las URLs de perfil ya indexadas y las
puntúa por coincidencia de nombre y de empresa en título/snippet.

Mecanismo (2026-10):
  - SearXNG (SEARXNG_URL, por defecto http://localhost:9000):
      GET /search?q=site:<red> "<nombre>"&format=json&engines=yandex,seznam
    En la instancia local solo yandex y seznam respetan `site:` sin CAPTCHA
    (bing ignora `site:`; google, duckduckgo, startpage, brave... bloqueados).
  - Google vía Apify (opcional, --apify, requiere APIFY_TOKEN): apify/google-search-scraper,
    ~0,0045 $ por consulta (ver apify_actores.py).
  - GitHub: GET api.github.com/search/users?q=<nombre> in:name (+ GITHUB_TOKEN opcional);
    se confirma con company/blog/email del perfil.
  Pausa de 1 s entre consultas al buscador.

Uso:
  perfiles.py "Sergio Sáenz Solano" --empresa "Sofias" --dominio sofias.ai
  perfiles.py "Nombre Apellido" --empresa ACME --redes linkedin,x,github --json
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import httpx

from personas_comun import HEADERS, coincidencia_nombre, normalizar

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:9000")
MOTORES = os.getenv("SEARXNG_ENGINES", "yandex,seznam")

# red -> (dominio para site:, regex de URL de perfil con grupo "usuario")
REDES = {
    "linkedin": ("linkedin.com/in", r"linkedin\.com/in/(?P<usuario>[\w%-]+)"),
    "x": ("x.com", r"(?:x|twitter)\.com/(?P<usuario>(?!search|hashtag|i/|intent|share)\w{1,15})(?:/status/\d+)?/?$"),
    "instagram": ("instagram.com", r"instagram\.com/(?P<usuario>(?!p/|reel|explore|stories)[\w.]{1,30})/?$"),
    "facebook": ("facebook.com", r"facebook\.com/(?P<usuario>(?!groups|events|pages/category|share|watch)[\w.]{5,50})/?$"),
    "tiktok": ("tiktok.com", r"tiktok\.com/@(?P<usuario>[\w.]{2,24})/?$"),
    "youtube": ("youtube.com", r"youtube\.com/(?:@|c/|channel/)(?P<usuario>[\w.-]+)/?$"),
    "github": ("github.com", r"github\.com/(?P<usuario>[A-Za-z0-9-]{1,39})/?$"),
    "medium": ("medium.com", r"medium\.com/@(?P<usuario>[\w.-]+)/?$"),
}
NO_PERFIL_GITHUB = {"orgs", "topics", "features", "about", "pricing", "login", "search", "marketplace", "sponsors"}


def searxng(consulta: str, client: httpx.Client) -> list[dict]:
    r = client.get(f"{SEARXNG_URL}/search", params={"q": consulta, "format": "json", "engines": MOTORES})
    r.raise_for_status()
    return [{"url": x.get("url"), "titulo": x.get("title") or "", "snippet": x.get("content") or "",
             "motor": x.get("engine")} for x in r.json().get("results", [])]


def google_apify(consultas: list[str]) -> list[dict]:
    from apify_actores import google
    return [{**h, "motor": "google(apify)"} for h in google(consultas)]


def perfil_de(url: str, red: str) -> str | None:
    m = re.search(REDES[red][1], url.split("?", 1)[0], re.I)
    if not m or (red == "github" and m["usuario"].lower() in NO_PERFIL_GITHUB):
        return None
    return m["usuario"]


FORMAS = r"\b(s\.?\s?l\.?u?|s\.?\s?a\.?u?|sociedad (?:limitada|anonima)|slne|scoop|s\.?\s?coop)\.?$"


def claves_empresa(empresa: str, dominio: str | None) -> set[str]:
    """Variantes con las que la empresa aparece en títulos: sin forma jurídica, primera palabra, raíz del dominio."""
    base = normalizar(re.sub(FORMAS, "", empresa.strip(), flags=re.I))
    claves = {base}
    if base.split() and len(base.split()[0]) >= 4:
        claves.add(base.split()[0])
    if dominio:
        claves.add(normalizar(dominio.split(".")[0]))
    return claves


def puntuar(nombre: str, empresa: str, dominio: str | None, titulo: str, extra: str = "") -> tuple[float, list[str]]:
    """El nombre se busca solo en el título (titular del perfil): un snippet que menciona a la persona
    suele ser el perfil de otra. Título/snippet cuentan para la empresa."""
    cn = coincidencia_nombre(nombre, titulo)
    if cn < 0.67:
        return 0.0, [f"nombre {cn:.0%} en el título"]
    s, ev = 0.6 * cn, [f"nombre {cn:.0%} en el título"]
    t = normalizar(f"{titulo} {extra}")
    claves = [k for k in claves_empresa(empresa, dominio) if len(k) >= 3]
    if any(k in t for k in claves):
        s += 0.35
        ev.append(f"empresa «{empresa}» en el resultado")
    return round(min(s, 0.99), 2), ev


def github_usuarios(nombre: str, empresa: str, dominio: str | None, client: httpx.Client) -> list[dict]:
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = client.get("https://api.github.com/search/users", params={"q": f'"{nombre}" in:name', "per_page": 10},
                   headers=h)
    if r.status_code != 200:
        return []
    out = []
    for u in r.json().get("items", [])[:10]:
        p = client.get(u["url"], headers=h).json()
        texto = " ".join(str(p.get(k) or "") for k in ("company", "blog", "email", "bio"))
        conf, ev = puntuar(nombre, empresa, dominio, p.get("name") or "", texto)
        if dominio and dominio in texto:
            conf, ev = min(0.99, conf + 0.2), ev + [f"dominio {dominio} en el perfil"]
        out.append({"red": "github", "usuario": p.get("login"), "url": p.get("html_url"), "nombre": p.get("name"),
                    "titulo": p.get("bio"), "confianza": conf, "evidencias": ev + ["API de GitHub"],
                    "ubicacion": p.get("location"), "foto": p.get("avatar_url")})
    return out


def buscar_perfiles(nombre: str, empresa: str, dominio: str | None = None, redes: list[str] | None = None,
                    apify: bool = False, min_confianza: float = 0.5) -> dict:
    if not empresa:
        raise ValueError("Se requiere la empresa vinculada (minimización RGPD).")
    redes = redes or list(REDES)
    # Sin comillas: el BORME escribe sin tildes («Saenz») y yandex es literal con comillas;
    # la precisión la da el filtro de nombre sobre el título.
    consultas = [(red, f"site:{REDES[red][0]} {nombre}") for red in redes]
    consultas.append((None, f"{nombre} {sorted(claves_empresa(empresa, dominio), key=len)[-1]}"))
    hits, avisos = [], []
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        for i, (red, q) in enumerate(consultas):
            if i:
                time.sleep(1)
            try:
                hits += [{**h, "consulta": q} for h in searxng(q, c)]
            except Exception as e:
                avisos.append(f"SearXNG «{q}»: {type(e).__name__}: {e}")
        if apify:
            try:
                hits += google_apify([q for _, q in consultas])
            except Exception as e:
                avisos.append(f"Apify: {e}")
        gh = github_usuarios(nombre, empresa, dominio, c) if "github" in redes else []

    perfiles: dict[str, dict] = {}
    for h in hits:
        host = urlparse(h["url"] or "").netloc.lower()
        for red in redes:
            if REDES[red][0].split("/")[0] not in host and not (red == "x" and "twitter.com" in host):
                continue
            usuario = perfil_de(h["url"], red)
            if not usuario:
                continue
            conf, ev = puntuar(nombre, empresa, dominio, h["titulo"], h["snippet"])
            clave = f"{red}:{usuario.lower()}"
            if clave not in perfiles or conf > perfiles[clave]["confianza"]:
                perfiles[clave] = {"red": red, "usuario": usuario, "url": h["url"].split("?", 1)[0],
                                   "titulo": h["titulo"], "confianza": conf,
                                   "evidencias": ev + [f"{h['motor']}: {h['consulta']}"]}
    for g in gh:
        clave = f"github:{(g['usuario'] or '').lower()}"
        if clave in perfiles:
            g["confianza"] = min(0.99, max(g["confianza"], perfiles[clave]["confianza"]) + 0.1)
            g["evidencias"] += perfiles[clave]["evidencias"]
        perfiles[clave] = g
    orden = sorted((p for p in perfiles.values() if p["confianza"] >= min_confianza), key=lambda p: -p["confianza"])
    return {"persona": nombre, "empresa": empresa, "dominio": dominio, "perfiles": orden, "avisos": avisos}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nombre")
    ap.add_argument("--empresa", required=True, help="empresa con la que la persona está vinculada")
    ap.add_argument("--dominio")
    ap.add_argument("--redes", help=f"subconjunto de {','.join(REDES)}")
    ap.add_argument("--apify", action="store_true", help="añadir Google vía Apify (de pago)")
    ap.add_argument("--min", type=float, default=0.5, help="confianza mínima")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = buscar_perfiles(a.nombre, a.empresa, a.dominio, a.redes.split(",") if a.redes else None, a.apify, a.min)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    for av in res["avisos"]:
        print(f"aviso: {av}", file=sys.stderr)
    print(f"{len(res['perfiles'])} perfiles para «{a.nombre}» ({a.empresa})")
    for p in res["perfiles"]:
        print(f"  {p['confianza']:.2f}  {p['red']:<9} {p['url']}\n        {'; '.join(p['evidencias'][:3])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
