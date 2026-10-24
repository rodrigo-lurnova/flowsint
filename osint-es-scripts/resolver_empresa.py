#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""Resolución de identidad de una empresa española: nombre (o NIF) → candidatos con
NIF/CIF validado, LEI, denominaciones normalizadas y web probable.

Una coincidencia de nombre es solo un candidato: el script agrupa por NIF y marca
las homonimias (varios NIF distintos para el mismo nombre), nunca las fusiona.

Mecanismo observado (2026-10), todas las fuentes sin clave:
  - BDNS (infosubvenciones.es): GET /bdnstrans/api/terceros?ambito=C&busqueda=<texto>&vpd=GE
      -> "NIF - NOMBRE" de cada beneficiario (varias grafías por NIF). Ver bdns.py.
  - GLEIF: GET https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]=<texto>
      &filter[entity.legalAddress.country]=ES  (o filter[entity.registeredAs]=<NIF>)
      -> LEI, denominación legal, estado, forma jurídica, ciudad. GLEIF no guarda el NIF:
      `registeredAs` suele ser el nº de inscripción registral, así que el LEI se asocia
      a un NIF solo si la denominación normalizada coincide.
  - TED (API v3): POST https://api.ted.europa.eu/v3/notices/search
      {"query": "winner-name ~ \"<texto>\"", "fields": ["winner-name","winner-identifier"]}
      -> pares (adjudicatario, identificador). El identificador de TED a veces es el del
      comprador: solo se acepta si es un CIF válido y la denominación coincide. Ver ted.py.
  - CNMV: búsqueda por entidad (ver cnmv.py); solo emisores y entidades registradas.
  - Web probable: <nombre>.{es,com,eu,...}; se resuelve el DNS y se lee la portada
    buscando el NIF y el nombre (prueba débil: una web que no menciona el NIF es solo
    una posibilidad).

Validación del NIF: DNI/NIE (letra = número mod 23) y CIF (dígito o letra de control
sobre los 7 dígitos centrales; P, Q, R, S, N, W y las 00xxxxx llevan letra; A, B, E, H, dígito).

Uso:
  resolver_empresa.py "BLINDITEX"
  resolver_empresa.py B26613679 --json
  resolver_empresa.py "Iberdrola Clientes" --fuentes bdns,gleif,ted --sin-web
"""
import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import socket
import sys
import unicodedata

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts)", "Accept-Language": "es-ES,es;q=0.9"}
GLEIF = "https://api.gleif.org/api/v1/lei-records"
TED = "https://api.ted.europa.eu/v3/notices/search"
FUENTES = ("bdns", "gleif", "ted", "cnmv")
TLDS = ("es", "com", "eu", "net", "org", "io", "ai", "cat", "eus", "gal")

# --- NIF / CIF -------------------------------------------------------------------------
DNI_LETRAS = "TRWAGMYFPDXBNJZSQVHLCKE"
CIF_LETRA_CONTROL = "JABCDEFGHI"
CIF_TIPOS = {
    "A": "sociedad anónima", "B": "sociedad de responsabilidad limitada", "C": "sociedad colectiva",
    "D": "sociedad comanditaria", "E": "comunidad de bienes", "F": "sociedad cooperativa",
    "G": "asociación o fundación", "H": "comunidad de propietarios", "J": "sociedad civil",
    "N": "entidad extranjera", "P": "corporación local", "Q": "organismo público",
    "R": "congregación o institución religiosa", "S": "órgano de la Administración",
    "U": "unión temporal de empresas", "V": "otro tipo", "W": "establecimiento permanente de no residente",
}
NIF_RE = re.compile(r"^(ES)?[A-Z0-9]\d{7}[A-Z0-9]$")


def limpiar_nif(nif: str) -> str:
    s = re.sub(r"[\s.\-/]", "", (nif or "").upper())
    return s[2:] if s.startswith("ES") and len(s) == 11 else s


def validar_nif(nif: str) -> dict:
    """Valida DNI, NIE y CIF. Devuelve {"nif", "valido", "tipo", "detalle"}."""
    s = limpiar_nif(nif)
    out = {"nif": s, "valido": False, "tipo": None, "detalle": None}
    if re.fullmatch(r"\d{8}[A-Z]", s):
        out.update(tipo="persona física (DNI)", valido=s[8] == DNI_LETRAS[int(s[:8]) % 23])
    elif re.fullmatch(r"[XYZ]\d{7}[A-Z]", s):
        num = "XYZ".index(s[0]) * 10**7 + int(s[1:8])
        out.update(tipo="persona física extranjera (NIE)", valido=s[8] == DNI_LETRAS[num % 23])
    elif re.fullmatch(r"[KLM]\d{7}[A-Z]", s):
        out.update(tipo="persona física (NIF K/L/M)", valido=s[8] == DNI_LETRAS[int(s[1:8]) % 23])
    elif re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", s):
        d = s[1:8]
        pares = sum(int(d[i]) for i in (1, 3, 5))
        impares = sum(sum(divmod(2 * int(d[i]), 10)) for i in (0, 2, 4, 6))
        c = (10 - (pares + impares) % 10) % 10
        ctrl = s[8]
        if s[0] in "PQRSNW" or d.startswith("00"):
            ok = ctrl == CIF_LETRA_CONTROL[c]
        elif s[0] in "ABEH":
            ok = ctrl == str(c)
        else:
            ok = ctrl in (str(c), CIF_LETRA_CONTROL[c])
        out.update(tipo="persona jurídica (CIF)", valido=ok, detalle=CIF_TIPOS.get(s[0]))
    return out


def parece_nif(texto: str) -> bool:
    return bool(NIF_RE.match(limpiar_nif(texto)))


# --- Denominación ----------------------------------------------------------------------
FORMAS_LARGAS = [
    ("SOCIEDAD LIMITADA UNIPERSONAL", "SLU"), ("SOCIEDAD ANONIMA UNIPERSONAL", "SAU"),
    ("SOCIEDAD LIMITADA LABORAL", "SLL"), ("SOCIEDAD LIMITADA NUEVA EMPRESA", "SLNE"),
    ("SOCIEDAD LIMITADA PROFESIONAL", "SLP"), ("SOCIEDAD DE RESPONSABILIDAD LIMITADA", "SL"),
    ("SOCIEDAD LIMITADA", "SL"), ("SOCIEDAD ANONIMA", "SA"), ("SOCIEDAD COOPERATIVA", "SCOOP"),
    ("S COOP", "SCOOP"), ("COMUNIDAD DE BIENES", "CB"), ("SOCIEDAD CIVIL", "SC"),
]
FORMAS = {"SL", "SLU", "SA", "SAU", "SLL", "SLNE", "SLP", "SAL", "SCOOP", "COOP", "CB", "SC", "SCP",
          "AIE", "UTE", "SRL", "SE", "SAD", "SOCIMI"}


def normalizar(nombre: str) -> dict:
    """'Blinditex, S.L.' -> {"denominacion": "BLINDITEX SL", "base": "BLINDITEX", "forma": "SL"}."""
    s = unicodedata.normalize("NFKD", html.unescape(nombre or "")).encode("ascii", "ignore").decode().upper()
    s = s.replace(".", "")
    s = re.sub(r"[,;:'\"()\[\]/\\\-–]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for largo, corto in FORMAS_LARGAS:
        s = re.sub(rf"\b{largo}\b", corto, s)
    toks = s.split()
    forma = None
    for i in range(len(toks) - 1, 0, -1):  # forma jurídica: último token reconocido (puede ir seguido de sufijos)
        if toks[i] in FORMAS:
            forma = toks[i]
            toks = toks[:i] + toks[i + 1:]
            break
    return {"denominacion": s, "base": " ".join(toks), "forma": forma}


# Letra del CIF ↔ forma jurídica de la denominación (para descartar identificadores cruzados).
FORMA_LETRA = {"SA": "A", "SAU": "A", "SAL": "A", "SAD": "A", "SE": "A", "SOCIMI": "A",
               "SL": "B", "SLU": "B", "SLL": "B", "SLNE": "B", "SLP": "B", "SRL": "B",
               "SCOOP": "F", "COOP": "F", "UTE": "U", "CB": "E", "SC": "J", "SCP": "J"}


def forma_coherente(nif: str, nombre: str) -> bool:
    """False si la denominación declara una forma (S.L., S.A....) incompatible con la letra del CIF."""
    forma = normalizar(nombre)["forma"]
    letra = FORMA_LETRA.get(forma or "")
    return letra is None or limpiar_nif(nif)[:1] == letra


def hasta_forma(nombre: str) -> str:
    """'IBERDROLA S.A.-DONOSTIA' -> 'IBERDROLA SA' (quita lo que va tras la forma jurídica)."""
    toks = normalizar(nombre)["denominacion"].split()
    for i, t in enumerate(toks):
        if i and t in FORMAS:
            return " ".join(toks[: i + 1])
    return " ".join(toks)


def coincidencia(consulta: str, candidato: str) -> str:
    """exacta (misma base) | contiene (todas las palabras de la consulta) | parcial."""
    q, c = normalizar(consulta)["base"], normalizar(candidato)["base"]
    if q == c:
        return "exacta"
    if set(q.split()) <= set(c.split()):
        return "contiene"
    return "parcial"


def mejor_denominacion(candidato: dict, consulta: str | None = None) -> str | None:
    """Denominación más fiable de un candidato: la legal de GLEIF, la que coincide con la consulta o la primera."""
    if candidato.get("gleif"):
        return candidato["gleif"]["nombre"]
    dens = candidato.get("denominaciones") or []
    if consulta and not parece_nif(consulta):
        exactas = [d for d in dens if coincidencia(consulta, d) == "exacta"]
        if exactas:
            return exactas[0]
    return min(dens, key=lambda d: len(normalizar(d)["denominacion"])) if dens else None


# --- Fuentes ---------------------------------------------------------------------------
def desde_bdns(client: httpx.Client, texto: str) -> list[dict]:
    import bdns
    out = []
    for t in bdns.buscar_terceros(client, texto):
        nif, _, nombre = t["descripcion"].partition(" - ")
        out.append({"fuente": "BDNS", "nif": limpiar_nif(nif), "nombre": nombre.strip(), "id_bdns": t["id"]})
    return out


def _gleif_item(x: dict) -> dict:
    a = x["attributes"]
    e = a["entity"]
    return {"fuente": "GLEIF", "lei": a["lei"], "nombre": e["legalName"]["name"],
            "registrado_como": e.get("registeredAs"), "estado": e.get("status"),
            "estado_lei": a.get("registration", {}).get("status"),
            "forma_juridica": (e.get("legalForm") or {}).get("id"),
            "ciudad": (e.get("legalAddress") or {}).get("city"),
            "otros_nombres": [n["name"] for n in e.get("otherNames") or []],
            "url": f"https://search.gleif.org/#/record/{a['lei']}"}


def desde_gleif(client: httpx.Client, texto: str, nif: str | None = None, max_items: int = 20) -> list[dict]:
    out = []
    if nif:
        r = client.get(GLEIF, params={"filter[entity.registeredAs]": nif, "page[size]": 5})
        r.raise_for_status()
        out += [_gleif_item(x) | {"por_nif": True} for x in r.json().get("data", [])]
    if texto and not parece_nif(texto):
        r = client.get(GLEIF, params={"filter[entity.legalName]": texto,
                                      "filter[entity.legalAddress.country]": "ES", "page[size]": max_items})
        r.raise_for_status()
        out += [_gleif_item(x) for x in r.json().get("data", [])]
    return out


def desde_ted(client: httpx.Client, texto: str, nif: str | None = None, max_items: int = 100) -> list[dict]:
    if nif:
        q = f'winner-identifier IN ("{nif}" "ES{nif}")'
    else:
        q = 'winner-name ~ "{}"'.format(texto.replace('"', ""))
    r = client.post(TED, json={"query": q, "fields": ["winner-name", "winner-identifier", "publication-number"],
                               "limit": max_items, "page": 1, "paginationMode": "PAGE_NUMBER"})
    r.raise_for_status()
    pares = {}
    for n in r.json().get("notices", []):
        nombres = next(iter((n.get("winner-name") or {}).values()), [])
        ids = n.get("winner-identifier") or []
        if len(nombres) != len(ids):  # sin correspondencia fiable nombre ↔ identificador
            continue
        for nombre, ident in zip(nombres, ids):
            v = validar_nif(ident)
            if not v["valido"] or (nif and v["nif"] != nif) or not forma_coherente(v["nif"], nombre):
                continue
            if not nif and coincidencia(texto, nombre) == "parcial":  # otro adjudicatario del mismo anuncio
                continue
            pares.setdefault((v["nif"], nombre), n.get("publication-number"))
    return [{"fuente": "TED", "nif": k[0], "nombre": k[1], "anuncio": p} for k, p in pares.items()]


def desde_cnmv(client: httpx.Client, texto: str) -> list[dict]:
    import cnmv
    if parece_nif(texto):
        return []
    return [{"fuente": "CNMV", "nif": limpiar_nif(e["nif"]), "nombre": e["nombre"]} for e in cnmv.buscar(client, texto)]


# --- Web probable ----------------------------------------------------------------------
def _resuelve(dominio: str) -> bool:
    try:
        socket.getaddrinfo(dominio, 443)
        return True
    except OSError:
        return False


def webs_probables(client: httpx.Client, base: str, nifs: list[str], tlds=TLDS) -> list[dict]:
    """Prueba <base>.<tld> (sin espacios y con guiones). Devuelve los que resuelven, con indicios."""
    slug = re.sub(r"[^a-z0-9 ]", "", base.lower()).strip()
    if not slug:
        return []
    variantes = {slug.replace(" ", ""), slug.replace(" ", "-")}
    dominios = sorted({f"{v}.{t}" for v in variantes for t in tlds})
    with cf.ThreadPoolExecutor(8) as ex:
        vivos = [d for d, ok in zip(dominios, ex.map(_resuelve, dominios)) if ok]
    out = []
    for d in vivos:
        item = {"dominio": d, "url": None, "titulo": None, "nif_en_portada": [], "nombre_en_portada": False}
        try:
            r = client.get(f"https://{d}", timeout=10, follow_redirects=True)
            txt = r.text[:400_000]
            t = re.search(r"<title[^>]*>(.*?)</title>", txt, re.S | re.I)
            plano = re.sub(r"[\s.\-]", "", re.sub(r"<[^>]+>", " ", txt)).upper()
            item.update(url=str(r.url), estado_http=r.status_code,
                        titulo=html.unescape(t.group(1)).strip()[:200] if t else None,
                        nif_en_portada=[n for n in nifs if n and n in plano],
                        nombre_en_portada=slug.replace(" ", "").upper() in plano)
        except httpx.HTTPError as e:
            item["error"] = type(e).__name__
        item["confianza"] = "alta" if item["nif_en_portada"] else ("media" if item["nombre_en_portada"] else "baja")
        out.append(item)
    return sorted(out, key=lambda i: ("alta", "media", "baja").index(i["confianza"]))


# --- Resolución ------------------------------------------------------------------------
def resolver(texto: str, fuentes=FUENTES, web: bool = True, max_candidatos: int = 15,
             client: httpx.Client | None = None) -> dict:
    """Devuelve {"consulta", "normalizado", "nif_consulta", "candidatos", "homonimia", "avisos", "webs"}."""
    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=40, follow_redirects=True)
    nif = limpiar_nif(texto) if parece_nif(texto) else None
    avisos, crudos = [], []
    try:
        tareas = {"bdns": lambda: desde_bdns(client, texto),
                  "gleif": lambda: desde_gleif(client, texto, nif),
                  "ted": lambda: desde_ted(client, texto, nif),
                  "cnmv": lambda: desde_cnmv(client, texto)}
        for f in fuentes:
            try:
                crudos += tareas[f]()
            except Exception as e:  # una fuente caída no invalida las demás
                avisos.append(f"{f}: {type(e).__name__}: {e}"[:200])
        if nif and "gleif" in fuentes:  # GLEIF no indexa el NIF: probar con la denominación que dio otra fuente
            for nombre in list(dict.fromkeys(hasta_forma(c["nombre"]) for c in crudos if c.get("nif") == nif))[:2]:
                try:
                    crudos += desde_gleif(client, nombre, max_items=5)
                except Exception as e:
                    avisos.append(f"gleif: {type(e).__name__}: {e}"[:200])

        # Agrupar por NIF (las grafías distintas del mismo NIF son la misma entidad).
        por_nif: dict[str, dict] = {}
        sin_nif: list[dict] = []
        for c in crudos:
            if c.get("nif"):
                g = por_nif.setdefault(c["nif"], {"nif": c["nif"], **validar_nif(c["nif"]),
                                                  "denominaciones": [], "fuentes": [], "lei": None,
                                                  "gleif": None, "id_bdns": []})
                if c["nombre"] and c["nombre"] not in g["denominaciones"]:
                    g["denominaciones"].append(c["nombre"])
                if c["fuente"] not in g["fuentes"]:
                    g["fuentes"].append(c["fuente"])
                if c.get("id_bdns"):
                    g["id_bdns"].append(c["id_bdns"])
            else:
                sin_nif.append(c)

        # Asociar LEI a un NIF solo con evidencia: búsqueda por NIF o misma denominación normalizada.
        for g in sin_nif:
            destino = None
            if g.get("por_nif") and nif in por_nif:
                destino = por_nif[nif]
            else:
                bases = {normalizar(g["nombre"])["denominacion"], *(normalizar(n)["denominacion"] for n in g["otros_nombres"])}
                ok = [p for p in por_nif.values() if bases & {normalizar(d)["denominacion"] for d in p["denominaciones"]}]
                destino = ok[0] if len(ok) == 1 else None
            if destino and not destino["lei"]:
                destino.update(lei=g["lei"], gleif=g)
                destino["fuentes"].append("GLEIF")
            else:
                por_nif[f"LEI:{g['lei']}"] = {"nif": None, "valido": None, "tipo": None, "detalle": None,
                                              "denominaciones": [g["nombre"]], "fuentes": ["GLEIF"],
                                              "lei": g["lei"], "gleif": g, "id_bdns": []}

        cands = list(por_nif.values())
        for c in cands:
            mejor = mejor_denominacion(c, texto)
            c["normalizada"] = normalizar(mejor)["denominacion"] if mejor else None
            if nif:
                c["coincidencia"] = "exacta" if c["nif"] == nif else "parcial"
            else:
                niveles = [coincidencia(texto, d) for d in c["denominaciones"]] or ["parcial"]
                c["coincidencia"] = min(niveles, key=("exacta", "contiene", "parcial").index)
            c["puntuacion"] = ({"exacta": 60, "contiene": 30, "parcial": 0}[c["coincidencia"]]
                               + 10 * len(c["fuentes"]) + (5 if c["valido"] else 0) + (5 if c["lei"] else 0))
        cands.sort(key=lambda c: -c["puntuacion"])
        cands = cands[:max_candidatos]

        fuertes = [c for c in cands if c["coincidencia"] == "exacta"]
        homonimia = len({c["nif"] or c["lei"] for c in fuertes}) > 1
        if homonimia:
            avisos.append(f"HOMONIMIA: {len(fuertes)} entidades distintas con la misma denominación base; "
                          "no fusionar sin un identificador.")
        if not fuertes and cands:
            avisos.append("Ningún candidato coincide exactamente con la denominación consultada.")
        for c in cands:
            if c["nif"] and not c["valido"]:
                avisos.append(f"NIF {c['nif']} con dígito de control incorrecto (dato de origen erróneo).")
            if c["nif"] and not all(forma_coherente(c["nif"], d) for d in c["denominaciones"]):
                avisos.append(f"NIF {c['nif']}: la letra no casa con la forma jurídica de alguna denominación.")

        nombre_web = mejor_denominacion(fuertes[0], texto) if fuertes else (None if nif else texto)
        base = normalizar(nombre_web)["base"] if nombre_web else ""
        webs = webs_probables(client, base, [c["nif"] for c in cands if c["nif"]]) if web and base else []
        if sum(w["confianza"] == "media" for w in webs) > 1 and not any(w["confianza"] == "alta" for w in webs):
            avisos.append("Varias webs usan el nombre y ninguna muestra el NIF en portada: posible homonimia web.")
        return {"consulta": texto, "normalizado": normalizar(texto) if not nif else None,
                "nif_consulta": validar_nif(nif) if nif else None, "candidatos": cands,
                "homonimia": homonimia, "avisos": avisos, "webs": webs}
    finally:
        if own:
            client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="denominación (o parte) o NIF/CIF")
    ap.add_argument("--fuentes", default=",".join(FUENTES), help=f"subconjunto de {','.join(FUENTES)}")
    ap.add_argument("--sin-web", action="store_true", help="no buscar la web probable")
    ap.add_argument("--max", type=int, default=15, help="máximo de candidatos")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    res = resolver(a.texto, [f for f in a.fuentes.split(",") if f in FUENTES], not a.sin_web, a.max)
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    if res["nif_consulta"]:
        v = res["nif_consulta"]
        print(f"NIF {v['nif']}: {'válido' if v['valido'] else 'NO válido'} — {v['tipo']} {v['detalle'] or ''}")
    else:
        print(f"Consulta «{a.texto}» → {res['normalizado']['denominacion']} (base «{res['normalizado']['base']}»)")
    for c in res["candidatos"]:
        ok = "✓" if c["valido"] else ("✗" if c["valido"] is False else "·")
        print(f"\n[{c['coincidencia']:8}] {c['nif'] or '—'} {ok}  {c['detalle'] or ''}  LEI {c['lei'] or '—'}"
              f"  ({', '.join(c['fuentes'])})\n    " + " | ".join(c["denominaciones"][:4]))
    for w in res["webs"]:
        print(f"\nWeb [{w['confianza']}] {w['dominio']}  {w.get('titulo') or ''}  NIF en portada: {w['nif_en_portada'] or 'no'}")
    for av in res["avisos"]:
        print(f"\n! {av}")
    return 0 if res["candidatos"] else 1


if __name__ == "__main__":
    sys.exit(main())
