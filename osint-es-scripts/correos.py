#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Correos probables de una persona en el dominio de su empresa (con confianza y evidencias).

Pensado para due diligence: solo personas vinculadas a la empresa investigada.
NO hace verificación SMTP (RCPT TO) ni ninguna otra comprobación activa sobre el
servidor de correo: solo señales pasivas/públicas.

Mecanismo observado (2026-10):
  1. Candidatos por patrones habituales (nombre.apellido, inicial+apellido, nombre,
     nombre.apellido1apellido2, ...), normalizando tildes/ñ y dos apellidos españoles.
  2. MX del dominio: dnspython si está instalado; si no, DNS-over-HTTPS
     GET https://cloudflare-dns.com/dns-query?name=<dom>&type=MX (Accept: application/dns-json).
     El proveedor (Microsoft 365, Google Workspace...) se deduce del host MX.
  3. Correos públicos en la web de la empresa: portada + páginas enlazadas cuyo
     enlace contiene contacto/aviso-legal/privacidad/equipo/nosotros... (mailto,
     texto, ofuscación «[at]» y data-cfemail de Cloudflare). Si junto al correo
     aparece un nombre, se deduce el patrón real de la empresa.
  4. GitHub sin token (10 búsquedas/min; GITHUB_TOKEN opcional sube a 30/min):
       GET https://api.github.com/search/commits?q=<dominio>   -> pares (autor, email)
       GET https://api.github.com/search/commits?q=author-email:<candidato>
     Un commit público firmado con el candidato es una confirmación fuerte.
  5. Gravatar: GET https://gravatar.com/avatar/<sha256(email)>?d=404 -> 200 si existe.
  6. Hunter.io (opcional, HUNTER_API_KEY): /v2/domain-search (patrón + correos) y
     /v2/email-finder (first_name, last_name, domain).

Uso:
  correos.py "Rodrigo Martínez" lurnova.ai
  correos.py "MARTINEZ GARCIA, RODRIGO" lurnova.ai --json
  correos.py --solo-dominio lurnova.ai          # correos públicos + patrón del dominio
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from personas_comun import (GENERICOS, HEADERS, coincidencia_nombre, contexto_email, crawl_web,  # noqa: E402
                            extraer_emails, host_raiz, normalizar, parsear_html, partir_nombre)

# Patrones con frecuencia aproximada a priori en empresas españolas/europeas.
# Variables: {n} nombre, {n2} segundo nombre, {a1} primer apellido, {a2} segundo, {i} inicial, {j} inicial ap1,
# {k} inicial ap2, {m} inicial segundo nombre
PATRONES = [
    ("{n}.{a1}", 0.30), ("{n}", 0.22), ("{i}{a1}", 0.18), ("{n}{a1}", 0.10), ("{i}.{a1}", 0.08),
    ("{n}.{a1}{a2}", 0.05), ("{n}.{a1}.{a2}", 0.03), ("{i}{a1}{a2}", 0.04), ("{i}{a1}{k}", 0.04),
    ("{n}{j}", 0.04), ("{n}.{j}", 0.03), ("{a1}.{n}", 0.02), ("{a1}{i}", 0.02), ("{n}_{a1}", 0.02),
    ("{n}-{a1}", 0.01), ("{a1}", 0.02), ("{n}{a1}{a2}", 0.02), ("{i}{m}{a1}", 0.02), ("{n}{n2}", 0.01),
    ("{n}.{n2}.{a1}", 0.01), ("{i}{j}{k}", 0.01),
]
PROVEEDORES = {"outlook.com": "Microsoft 365", "google.com": "Google Workspace", "googlemail.com": "Google Workspace",
               "zoho": "Zoho Mail", "ovh.net": "OVH", "ionos": "IONOS", "1and1": "IONOS", "secureserver.net": "GoDaddy",
               "pphosted.com": "Proofpoint", "mimecast": "Mimecast", "barracuda": "Barracuda", "arsys": "Arsys",
               "dinahosting": "Dinahosting", "hostinger": "Hostinger", "yandex": "Yandex", "icloud.com": "iCloud",
               "messagingengine.com": "Fastmail", "protonmail": "Proton"}


def _vars(p: dict) -> dict:
    n, a1, a2, n2 = p.get("nombre", ""), p.get("apellido1", ""), p.get("apellido2", ""), p.get("nombre2", "")
    return {"n": n, "n2": n2, "a1": a1, "a2": a2, "i": n[:1], "j": a1[:1], "k": a2[:1], "m": n2[:1]}


def generar_candidatos(nombre: str, dominio: str, apellidos_primero: bool | None = None) -> list[dict]:
    """[{email, patron, prior}] sin duplicados; omite patrones que necesitan piezas ausentes."""
    out: dict[str, dict] = {}
    for idx, interp in enumerate(partir_nombre(nombre, apellidos_primero)):
        v = _vars(interp)
        for pat, prior in PATRONES:
            usados = re.findall(r"\{(\w+)\}", pat)
            if any(not v[u] for u in usados):
                continue
            local = pat.format(**v)
            if len(local) < 2:
                continue
            e = f"{local}@{dominio}"
            pr = prior * (1.0 if idx == 0 else 0.6)
            if e not in out or out[e]["prior"] < pr:
                out[e] = {"email": e, "patron": pat, "prior": round(pr, 3), "interpretacion": interp}
    return sorted(out.values(), key=lambda c: -c["prior"])


def patron_de(local: str, nombre: str) -> list[str]:
    """Qué patrones (de PATRONES) generan `local` para ese nombre."""
    res = []
    for interp in partir_nombre(nombre):
        v = _vars(interp)
        for pat, _ in PATRONES:
            usados = re.findall(r"\{(\w+)\}", pat)
            if all(v[u] for u in usados) and pat.format(**v) == local:
                res.append(pat)
    return list(dict.fromkeys(res))


# ------------------------------------------------------------------ MX

def mx(dominio: str, client: httpx.Client) -> dict:
    hosts = []
    try:
        import dns.resolver  # type: ignore
        ans = dns.resolver.resolve(dominio, "MX", lifetime=8)
        hosts = sorted((r.preference, str(r.exchange).rstrip(".")) for r in ans)
        via = "dns"
    except ImportError:
        via = "doh"
        r = client.get("https://cloudflare-dns.com/dns-query", params={"name": dominio, "type": "MX"},
                       headers={"Accept": "application/dns-json"})
        for a in r.json().get("Answer", []) or []:
            if a.get("type") == 15:
                pref, host = a["data"].split(None, 1)
                hosts.append((int(pref), host.rstrip(".")))
        hosts.sort()
    except Exception as e:  # NXDOMAIN, NoAnswer, timeout
        return {"tiene_mx": False, "hosts": [], "proveedor": None, "error": type(e).__name__, "via": "dns"}
    prov = None
    for _, h in hosts:
        for k, v in PROVEEDORES.items():
            if k in h.lower():
                prov = v
                break
        prov = prov or (None if not h else ("propio" if dominio in h else None))
    return {"tiene_mx": bool(hosts), "hosts": [h for _, h in hosts], "proveedor": prov, "via": via}


# ------------------------------------------------------------------ fuentes

def correos_web(dominio: str, max_paginas: int = 12, client: httpx.Client | None = None) -> list[dict]:
    """Correos que aparecen en la web del dominio: [{email, url, contexto}] (solo @dominio o subdominios)."""
    out: dict[str, dict] = {}
    raiz = host_raiz(dominio)
    for pag in crawl_web(dominio, max_paginas=max_paginas, client=client):
        parsed = parsear_html(pag["html"])
        for e in extraer_emails(pag["html"], parsed):
            d = e.split("@", 1)[1]
            if d != raiz and not d.endswith("." + raiz):
                continue
            if e not in out:
                out[e] = {"email": e, "url": pag["url"], "contexto": contexto_email(parsed["bloques"], e)[:400]}
    return list(out.values())


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "osint-es-scripts"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return h


def github_commits(q: str, client: httpx.Client, per_page: int = 30) -> list[dict]:
    """Búsqueda de commits públicos -> [{autor, email, repo, url, fecha}]. [] si rate limit."""
    r = client.get("https://api.github.com/search/commits", params={"q": q, "per_page": per_page},
                   headers=_gh_headers())
    if r.status_code != 200:
        return []
    out = []
    for it in r.json().get("items", []):
        a = it.get("commit", {}).get("author", {}) or {}
        out.append({"autor": a.get("name"), "email": (a.get("email") or "").lower(), "fecha": a.get("date"),
                    "repo": (it.get("repository") or {}).get("full_name"), "url": it.get("html_url"),
                    "login": (it.get("author") or {}).get("login")})
    return out


def gravatar(email: str, client: httpx.Client) -> dict | None:
    h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    try:
        r = client.get(f"https://gravatar.com/avatar/{h}", params={"d": "404", "s": "80"})
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return {"avatar": f"https://gravatar.com/avatar/{h}", "perfil": f"https://gravatar.com/{h}"}
    return None


def hunter(dominio: str, nombre: str | None, client: httpx.Client, key: str) -> dict:
    out: dict = {}
    r = client.get("https://api.hunter.io/v2/domain-search", params={"domain": dominio, "limit": 10, "api_key": key})
    if r.status_code == 200:
        d = r.json().get("data", {})
        out["patron"] = d.get("pattern")
        out["organizacion"] = d.get("organization")
        out["correos"] = [{"email": e["value"], "confianza": e.get("confidence"), "nombre":
                           " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x),
                           "cargo": e.get("position"), "fuentes": [s.get("uri") for s in e.get("sources", [])][:3]}
                          for e in d.get("emails", [])]
    else:
        out["error_domain_search"] = f"HTTP {r.status_code}: {r.text[:150]}"
    if nombre:
        interp = (partir_nombre(nombre) or [{}])[0]
        ap = " ".join(x for x in (interp.get("apellido1"), interp.get("apellido2")) if x)
        r = client.get("https://api.hunter.io/v2/email-finder",
                       params={"domain": dominio, "first_name": interp.get("nombre"), "last_name": ap, "api_key": key})
        if r.status_code == 200:
            d = r.json().get("data", {})
            out["finder"] = {"email": d.get("email"), "score": d.get("score"), "cargo": d.get("position")}
        else:
            out["error_email_finder"] = f"HTTP {r.status_code}: {r.text[:150]}"
    return out


# ------------------------------------------------------------------ patrón de la empresa

_NOMBRE_EN_TEXTO = re.compile(r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñü]+(?:\s+(?:de\s+(?:la\s+)?|del\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+){1,3})\b")


def detectar_patron(publicos: list[dict], commits: list[dict], dominio: str) -> dict:
    """Vota patrones a partir de pares (nombre, email) conocidos del dominio."""
    votos: dict[str, list[str]] = {}
    pares = []
    for c in commits:
        if c["email"].endswith("@" + dominio) and c.get("autor") and " " in c["autor"].strip():
            pares.append((c["autor"], c["email"], f"GitHub commit {c.get('repo')}"))
    for p in publicos:
        for m in _NOMBRE_EN_TEXTO.finditer(p.get("contexto") or ""):
            pares.append((m.group(1), p["email"], f"web {p['url']}"))
    for nom, email, fuente in pares:
        local = email.split("@")[0]
        if local in GENERICOS:
            continue
        for pat in patron_de(local, nom):
            votos.setdefault(pat, []).append(f"{email} ↔ «{nom}» ({fuente})")
    # forma del local-part de correos personales sin nombre asociado (señal débil)
    formas: dict[str, int] = {}
    for p in publicos + [{"email": c["email"]} for c in commits if c["email"].endswith("@" + dominio)]:
        local = p["email"].split("@")[0]
        if local in GENERICOS or any(ch.isdigit() for ch in local):
            continue
        if re.fullmatch(r"[a-z]{2,}\.[a-z]{2,}", local):
            formas["{n}.{a1}"] = formas.get("{n}.{a1}", 0) + 1
        elif re.fullmatch(r"[a-z]\.[a-z]{2,}", local):
            formas["{i}.{a1}"] = formas.get("{i}.{a1}", 0) + 1
        elif re.fullmatch(r"[a-z]{3,12}", local):
            formas["{n}|{i}{a1}"] = formas.get("{n}|{i}{a1}", 0) + 1
    if votos:
        pat = max(votos, key=lambda k: len(votos[k]))
        return {"patron": pat, "metodo": "nombre+correo", "evidencias": votos[pat][:5], "votos":
                {k: len(v) for k, v in votos.items()}, "formas": formas}
    if formas:
        f = max(formas, key=formas.get)
        return {"patron": None, "forma_probable": f, "metodo": "forma del local-part", "formas": formas, "evidencias": []}
    return {"patron": None, "metodo": None, "evidencias": [], "formas": {}}


# ------------------------------------------------------------------ orquestación

def analizar_dominio(dominio: str, max_paginas: int = 12, github: bool = True,
                     client: httpx.Client | None = None, hunter_key: str | None = None) -> dict:
    """Correos públicos + patrón de un dominio (sin persona). Base para Domain -> Email."""
    dominio = host_raiz(re.sub(r"^https?://", "", dominio).split("/")[0])
    propio = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)
    try:
        res = {"dominio": dominio, "mx": mx(dominio, client)}
        try:
            res["correos_publicos"] = correos_web(dominio, max_paginas, client)
        except httpx.HTTPError as e:
            res["correos_publicos"], res["error_web"] = [], f"{type(e).__name__}: {e}"
        res["github"] = github_commits(f'"{dominio}"', client) if github else []
        res["github"] = [c for c in res["github"] if c["email"].endswith("@" + dominio)]
        hk = hunter_key or os.getenv("HUNTER_API_KEY")
        res["hunter"] = hunter(dominio, None, client, hk) if hk else None
        res["patron"] = detectar_patron(res["correos_publicos"], res["github"], dominio)
        if res["hunter"] and res["hunter"].get("patron") and not res["patron"].get("patron"):
            hp = res["hunter"]["patron"].replace("{first}", "{n}").replace("{last}", "{a1}") \
                .replace("{f}", "{i}").replace("{l}", "{j}")
            res["patron"].update({"patron": hp, "metodo": "hunter", "evidencias": ["Hunter.io domain-search"]})
        return res
    finally:
        if propio:
            client.close()


def buscar_correos(nombre: str, dominio: str, max_paginas: int = 12, max_verificar: int = 6,
                   gravatar_on: bool = True, github: bool = True, hunter_key: str | None = None,
                   apellidos_primero: bool | None = None, dominio_info: dict | None = None) -> dict:
    """Candidatos puntuados para (persona, dominio). `dominio_info` permite reutilizar analizar_dominio()."""
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as c:
        hk = hunter_key or os.getenv("HUNTER_API_KEY")
        info = dominio_info or analizar_dominio(dominio, max_paginas, github, c, hk)
        dominio = info["dominio"]
        cands = generar_candidatos(nombre, dominio, apellidos_primero)
        por_email = {x["email"]: {**x, "evidencias": [f"patrón {x['patron']} (frecuencia a priori {x['prior']:.0%})"],
                                  "confianza": x["prior"]} for x in cands}
        pat = info["patron"].get("patron")
        forma = info["patron"].get("forma_probable")
        publicos = {p["email"]: p for p in info["correos_publicos"]}
        gh = {g["email"]: g for g in info["github"]}

        for e, c_ in por_email.items():
            if pat and c_["patron"] == pat:
                c_["confianza"] = max(c_["confianza"], 0.6)
                c_["evidencias"].append(f"coincide con el patrón de la empresa {pat} ({info['patron']['metodo']})")
            elif forma and c_["patron"] in forma.split("|"):
                c_["confianza"] += 0.12
                c_["evidencias"].append(f"coincide con la forma habitual de los correos del dominio ({forma})")
            if e in publicos:
                c_["confianza"] = 0.95
                c_["evidencias"].append(f"publicado en {publicos[e]['url']}")
            if e in gh:
                c_["confianza"] = max(c_["confianza"], 0.9)
                c_["evidencias"].append(f"commit público en GitHub ({gh[e].get('repo')})")

        # correos públicos que encajan con el nombre aunque no estén en los patrones
        for e, p in publicos.items():
            if e not in por_email and coincidencia_nombre(nombre, p.get("contexto", "")) >= 0.67 \
                    and e.split("@")[0] not in GENERICOS:
                por_email[e] = {"email": e, "patron": "publicado", "prior": 0, "confianza": 0.7,
                                "evidencias": [f"publicado junto al nombre en {p['url']}"]}

        hunter_res = None
        if hk:
            hunter_res = hunter(dominio, nombre, c, hk)
            f = hunter_res.get("finder") or {}
            if f.get("email"):
                x = por_email.setdefault(f["email"], {"email": f["email"], "patron": "hunter", "prior": 0,
                                                      "confianza": 0, "evidencias": []})
                x["confianza"] = max(x["confianza"], (f.get("score") or 50) / 100)
                x["evidencias"].append(f"Hunter email-finder score {f.get('score')}")

        orden = sorted(por_email.values(), key=lambda x: -x["confianza"])
        # verificaciones pasivas sobre los mejores candidatos
        for i, x in enumerate(orden[:max_verificar]):
            if github and x["confianza"] < 0.9:
                if i:
                    time.sleep(0.6 if os.getenv("GITHUB_TOKEN") else 6.5)  # 10 búsquedas/min sin token
                hits = github_commits(f"author-email:{x['email']}", c, per_page=5)
                if hits:
                    x["confianza"] = max(x["confianza"], 0.9)
                    x["evidencias"].append(f"{len(hits)}+ commits públicos con este autor en GitHub "
                                           f"({hits[0].get('repo')}; login {hits[0].get('login')})")
                    x["github_login"] = hits[0].get("login")
            if gravatar_on:
                g = gravatar(x["email"], c)
                if g:
                    x["confianza"] += 0.25
                    x["evidencias"].append("tiene Gravatar (cuenta registrada con este correo)")
                    x["gravatar"] = g["avatar"]
        if not info["mx"]["tiene_mx"]:
            for x in orden:
                x["confianza"] *= 0.2
                x["evidencias"].append("¡el dominio no tiene MX!")
        for x in orden:
            x["confianza"] = round(min(x["confianza"], 0.99), 2)
            x.pop("interpretacion", None)
        orden.sort(key=lambda x: -x["confianza"])
        return {"persona": nombre, "dominio": dominio, "mx": info["mx"], "patron_empresa": info["patron"],
                "correos_publicos": info["correos_publicos"], "github_dominio": info["github"],
                "hunter": hunter_res or info.get("hunter"), "candidatos": orden}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nombre", nargs="?", help="nombre completo de la persona")
    ap.add_argument("dominio", nargs="?", help="dominio de la empresa (p. ej. lurnova.ai)")
    ap.add_argument("--solo-dominio", metavar="DOMINIO", help="solo correos públicos + patrón de un dominio")
    ap.add_argument("--apellidos-primero", action="store_true", help="el nombre viene como «Apellido1 Apellido2 Nombre»")
    ap.add_argument("--max-paginas", type=int, default=12, help="páginas de la web a revisar")
    ap.add_argument("--verificar", type=int, default=6, help="candidatos a comprobar en GitHub/Gravatar")
    ap.add_argument("--sin-github", action="store_true")
    ap.add_argument("--sin-gravatar", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.solo_dominio:
        res = analizar_dominio(a.solo_dominio, a.max_paginas, not a.sin_github)
        if a.json:
            json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
            return 0
        m = res["mx"]
        print(f"{res['dominio']}  MX: {', '.join(m['hosts']) or 'NINGUNO'}  ({m.get('proveedor')})")
        print(f"Patrón: {res['patron'].get('patron') or res['patron'].get('forma_probable')}  [{res['patron'].get('metodo')}]")
        for e in res["patron"].get("evidencias", []):
            print(f"   · {e}")
        for p in res["correos_publicos"]:
            print(f"  {p['email']:<40} {p['url']}")
        for g in res["github"]:
            print(f"  {g['email']:<40} GitHub {g['autor']} ({g['repo']})")
        return 0

    if not (a.nombre and a.dominio):
        ap.error("indica nombre y dominio, o --solo-dominio")
    res = buscar_correos(a.nombre, a.dominio, a.max_paginas, a.verificar, not a.sin_gravatar, not a.sin_github,
                         apellidos_primero=a.apellidos_primero or None)
    res["candidatos"] = res["candidatos"][:a.top]
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    m = res["mx"]
    print(f"{res['persona']} @ {res['dominio']}   MX: {', '.join(m['hosts']) or 'NINGUNO'} ({m.get('proveedor')})")
    pe = res["patron_empresa"]
    print(f"Patrón de la empresa: {pe.get('patron') or pe.get('forma_probable') or 'desconocido'}  [{pe.get('metodo')}]")
    if res["correos_publicos"]:
        print("Correos públicos en la web: " + ", ".join(p["email"] for p in res["correos_publicos"]))
    print()
    for x in res["candidatos"]:
        print(f"  {x['confianza']:>4.0%}  {x['email']:<40} {x['patron']}")
        for e in x["evidencias"][1:]:
            print(f"          · {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
