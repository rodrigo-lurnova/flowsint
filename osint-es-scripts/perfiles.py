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
  - Brave Search API si hay BRAVE_API_KEY:
      GET api.search.brave.com/res/v1/web/search?q=site:<red> <nombre>
  - Sin BRAVE_API_KEY, SearXNG (SEARXNG_URL, por defecto http://localhost:9000):
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
from personas_comun import HEADERS, coincidencia_nombre, error_github, normalizar

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:9000")
MOTORES = os.getenv("SEARXNG_ENGINES", "yandex,seznam")

# red -> (dominio para site:, regex de URL de perfil con grupo "usuario")
REDES = {
    "linkedin": ("linkedin.com/in", r"linkedin\.com/in/(?P<usuario>[\w%-]+)"),
    "x": (
        "x.com",
        r"(?:x|twitter)\.com/(?P<usuario>(?!search|hashtag|i/|intent|share)\w{1,15})(?:/status/\d+)?/?$",
    ),
    "instagram": (
        "instagram.com",
        r"instagram\.com/(?P<usuario>(?!p/|reel|explore|stories)[\w.]{1,30})/?$",
    ),
    "facebook": (
        "facebook.com",
        r"facebook\.com/(?P<usuario>(?!groups|events|pages/category|share|watch)[\w.]{5,50})/?$",
    ),
    "tiktok": ("tiktok.com", r"tiktok\.com/@(?P<usuario>[\w.]{2,24})/?$"),
    "youtube": (
        "youtube.com",
        r"youtube\.com/(?:@|c/|channel/)(?P<usuario>[\w.-]+)/?$",
    ),
    "github": ("github.com", r"github\.com/(?P<usuario>[A-Za-z0-9-]{1,39})/?$"),
    "medium": ("medium.com", r"medium\.com/@(?P<usuario>[\w.-]+)/?$"),
}
NO_PERFIL_GITHUB = {
    "orgs",
    "topics",
    "features",
    "about",
    "pricing",
    "login",
    "search",
    "marketplace",
    "sponsors",
}


class MotoresSinRespuesta(RuntimeError):
    pass


def motivo(e: Exception) -> str:
    """Tipo del error y estado HTTP, sin el texto: puede llevar la URL interna de SearXNG."""
    estado = getattr(getattr(e, "response", None), "status_code", None)
    return type(e).__name__ + (f" HTTP {estado}" if estado else "")


def searxng(consulta: str, client: httpx.Client) -> list[dict]:
    r = client.get(
        f"{SEARXNG_URL}/search",
        params={"q": consulta, "format": "json", "engines": MOTORES},
    )
    r.raise_for_status()
    datos = r.json()
    if not datos.get("results") and datos.get("unresponsive_engines"):
        raise MotoresSinRespuesta(
            "motores sin respuesta: "
            + ", ".join(
                " ".join(str(x) for x in m) if isinstance(m, (list, tuple)) else str(m)
                for m in datos["unresponsive_engines"]
            )
        )
    return [
        {
            "url": x.get("url"),
            "titulo": x.get("title") or "",
            "snippet": x.get("content") or "",
            "motor": x.get("engine"),
        }
        for x in datos.get("results", [])
    ]


def brave(consulta: str, client: httpx.Client, clave: str) -> list[dict]:
    for intento in range(2):
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": consulta, "count": 20, "text_decorations": "false"},
            headers={"Accept": "application/json", "X-Subscription-Token": clave},
            follow_redirects=False,  # httpx reenviaría la clave a otro host
        )
        if r.status_code != 429 or intento:
            break
        time.sleep(2)  # límite por segundo del plan
    r.raise_for_status()
    return [
        {
            "url": x.get("url"),
            "titulo": x.get("title") or "",
            "snippet": x.get("description") or "",
            "motor": "brave",
        }
        for x in (r.json().get("web") or {}).get("results", [])
    ]


def google_apify(
    consultas: list[str], token: str | None = None, errores: list | None = None
) -> list[dict]:
    from apify_actores import google

    return [
        {**h, "motor": "google(apify)"}
        for h in google(consultas, token=token, errores=errores)
    ]


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


def puntuar(
    nombre: str, empresa: str, dominio: str | None, titulo: str, extra: str = ""
) -> tuple[float, list[str]]:
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


def github_usuarios(
    nombre: str,
    empresa: str,
    dominio: str | None,
    client: httpx.Client,
    errores: list | None = None,
) -> list[dict]:
    errores = [] if errores is None else errores
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

    def pedir(url: str, **params) -> dict | None:
        try:
            r = client.get(url, params=params or None, headers=h)
            if r.status_code == 200:
                return r.json()
            errores.append(error_github(r))
        except (httpx.HTTPError, ValueError) as e:
            errores.append(f"GitHub: {type(e).__name__}")
        return None

    res = pedir(
        "https://api.github.com/search/users", q=f'"{nombre}" in:name', per_page=10
    )
    out = []
    for u in (res or {}).get("items", [])[:10]:
        p = pedir(u["url"])
        if p is None:
            continue
        texto = " ".join(
            str(p.get(k) or "") for k in ("company", "blog", "email", "bio")
        )
        conf, ev = puntuar(nombre, empresa, dominio, p.get("name") or "", texto)
        if dominio and dominio in texto:
            conf, ev = min(0.99, conf + 0.2), ev + [f"dominio {dominio} en el perfil"]
        out.append(
            {
                "red": "github",
                "usuario": p.get("login"),
                "url": p.get("html_url"),
                "nombre": p.get("name"),
                "titulo": p.get("bio"),
                "confianza": conf,
                "evidencias": ev + ["API de GitHub"],
                "ubicacion": p.get("location"),
                "foto": p.get("avatar_url"),
            }
        )
    return out


def buscar_perfiles(
    nombre: str,
    empresa: str,
    dominio: str | None = None,
    redes: list[str] | None = None,
    apify: bool = False,
    min_confianza: float = 0.5,
    *,
    apify_token: str | None = None,
    brave_key: str | None = None,
) -> dict:
    """`errores` son las fuentes que han fallado; `avisos` los incluye. Sin claves se usan APIFY_TOKEN y BRAVE_API_KEY."""
    if not empresa:
        raise ValueError("Se requiere la empresa vinculada (minimización RGPD).")
    redes = redes or list(REDES)
    brave_key = (brave_key or os.getenv("BRAVE_API_KEY") or "").strip()
    # Sin comillas: el BORME escribe sin tildes («Saenz») y yandex es literal con comillas;
    # la precisión la da el filtro de nombre sobre el título.
    consultas = [(red, f"site:{REDES[red][0]} {nombre}") for red in redes]
    consultas.append(
        (None, f"{nombre} {sorted(claves_empresa(empresa, dominio), key=len)[-1]}")
    )
    hits, errores = [], []
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        for i, (red, q) in enumerate(consultas):
            if i:
                time.sleep(1.1)
            try:
                hits += [
                    {**h, "consulta": q}
                    for h in (brave(q, c, brave_key) if brave_key else searxng(q, c))
                ]
            except Exception as e:
                buscador = "Brave" if brave_key else "SearXNG"
                errores.append(f"{buscador} «{q}»: {motivo(e)}")
        if apify:
            try:
                hits += google_apify([q for _, q in consultas], apify_token, errores)
            except Exception as e:
                errores.append(f"Apify: {e}")
        gh = (
            github_usuarios(nombre, empresa, dominio, c, errores)
            if "github" in redes
            else []
        )

    perfiles: dict[str, dict] = {}
    for h in hits:
        host = urlparse(h["url"] or "").netloc.lower()
        for red in redes:
            if REDES[red][0].split("/")[0] not in host and not (
                red == "x" and "twitter.com" in host
            ):
                continue
            usuario = perfil_de(h["url"], red)
            if not usuario:
                continue
            conf, ev = puntuar(nombre, empresa, dominio, h["titulo"], h["snippet"])
            clave = f"{red}:{usuario.lower()}"
            if clave not in perfiles or conf > perfiles[clave]["confianza"]:
                perfiles[clave] = {
                    "red": red,
                    "usuario": usuario,
                    "url": h["url"].split("?", 1)[0],
                    "titulo": h["titulo"],
                    "confianza": conf,
                    "evidencias": ev + [f"{h['motor']}: {h['consulta']}"],
                }
    for g in gh:
        clave = f"github:{(g['usuario'] or '').lower()}"
        if clave in perfiles:
            g["confianza"] = min(
                0.99, max(g["confianza"], perfiles[clave]["confianza"]) + 0.1
            )
            g["evidencias"] += perfiles[clave]["evidencias"]
        perfiles[clave] = g
    orden = sorted(
        (p for p in perfiles.values() if p["confianza"] >= min_confianza),
        key=lambda p: -p["confianza"],
    )
    errores = list(dict.fromkeys(errores))
    return {
        "persona": nombre,
        "empresa": empresa,
        "dominio": dominio,
        "perfiles": orden,
        "avisos": list(errores),
        "errores": errores,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("nombre")
    ap.add_argument(
        "--empresa", required=True, help="empresa con la que la persona está vinculada"
    )
    ap.add_argument("--dominio")
    ap.add_argument("--redes", help=f"subconjunto de {','.join(REDES)}")
    ap.add_argument(
        "--apify", action="store_true", help="añadir Google vía Apify (de pago)"
    )
    ap.add_argument("--min", type=float, default=0.5, help="confianza mínima")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = buscar_perfiles(
        a.nombre,
        a.empresa,
        a.dominio,
        a.redes.split(",") if a.redes else None,
        a.apify,
        a.min,
    )
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    for av in res["avisos"]:
        print(f"aviso: {av}", file=sys.stderr)
    print(f"{len(res['perfiles'])} perfiles para «{a.nombre}» ({a.empresa})")
    for p in res["perfiles"]:
        print(
            f"  {p['confianza']:.2f}  {p['red']:<9} {p['url']}\n        {'; '.join(p['evidencias'][:3])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
