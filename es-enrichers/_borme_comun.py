"""Utilidades compartidas por los enrichers BORME (organization_*_borme, individual_*_borme).

La lógica de descarga/parseo vive en osint-es-scripts/borme.py (y apispain.py);
aquí solo: parámetros comunes, llamada no bloqueante y conversión a tipos Flowsint.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from flowsint_enrichers.es import _evidencias as ev
from flowsint_enrichers.es._org import org_ids, sin_identificador
from flowsint_enrichers.es._scripts import load
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization

FUENTE = "BORME"
APISPAIN = "BORME apispain"
PARTICULAS = {
    "DE",
    "DEL",
    "LA",
    "LAS",
    "LOS",
    "Y",
    "SAN",
    "SANTA",
    "VAN",
    "VON",
    "DA",
    "DOS",
    "DI",
}
PALABRAS_JURIDICAS = {
    "SOCIEDAD",
    "SOCIEDADES",
    "AUDITORES",
    "AUDITORIA",
    "ASESORES",
    "CONSULTORES",
    "BANCO",
    "CAJA",
    "FUNDACION",
    "ASOCIACION",
    "HOLDING",
    "GROUP",
    "GRUPO",
    "INVERSIONES",
    "GESTION",
    "LIMITED",
    "LTD",
    "LLC",
    "LLP",
    "INC",
    "GMBH",
    "BV",
    "SARL",
    "SAS",
    "SRL",
    "SPA",
    "AG",
    "CORPORATION",
    "CORP",
    "COMPANY",
    "PARTNERS",
    "CAPITAL",
    "SERVICIOS",
    "ABOGADOS",
}

PARAMS_COMUNES: List[Dict[str, Any]] = [
    {
        "name": "DIAS",
        "type": "string",
        "default": "90",
        "required": False,
        "description": "Días hacia atrás a revisar en el BORME si no se da DESDE (coste: ~2 peticiones/día hábil "
        "en modo índice, ~30/día en modo completo).",
    },
    {
        "name": "DESDE",
        "type": "string",
        "required": False,
        "description": "Fecha inicial AAAA-MM-DD.",
    },
    {
        "name": "HASTA",
        "type": "string",
        "required": False,
        "description": "Fecha final AAAA-MM-DD (hoy).",
    },
    {
        "name": "PROVINCIA",
        "type": "string",
        "required": False,
        "description": "Provincias del Registro Mercantil separadas por comas (nombre o código INE). "
        "Reduce mucho el coste si se conoce.",
    },
    {
        "name": "EXACTA",
        "type": "string",
        "default": "false",
        "required": False,
        "description": "true = solo denominación exacta (normalizada, sin forma jurídica).",
    },
    {
        "name": "BACKEND",
        "type": "string",
        "default": "auto",
        "required": False,
        "description": "auto | boe | apispain (auto usa apispain si hay APISPAIN_API_KEY y, si falla, el BOE).",
    },
    {
        "name": "APISPAIN_API_KEY",
        "type": "vaultSecret",
        "required": False,
        "description": "Clave opcional de apispain.es (plan Free: 100 peticiones/mes).",
    },
]


def p_bool(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "si", "sí", "yes", "y")


def p_list(v: Any) -> Optional[List[str]]:
    xs = [x.strip() for x in str(v or "").split(",") if x.strip()]
    return xs or None


def borme():
    return load("borme")


class FalloBorme(RuntimeError):
    """Ningún backend ha respondido. `fallos`: [(fuente, excepción)]."""

    def __init__(self, fallos: List[tuple]):
        super().__init__("; ".join(f"{f}: {e}" for f, e in fallos))
        self.fallos = fallos


async def buscar_empresa(
    nombre: str, params: Dict[str, Any], api_key: Optional[str]
) -> dict:
    """Busca con apispain o el BOE. En modo auto, si apispain falla se usa el BOE y el fallo va en
    res["fallos"]; si no responde ninguno, FalloBorme."""
    mod = borme()
    backend = (params.get("BACKEND") or "auto").strip().lower()
    exacta = p_bool(params.get("EXACTA"))
    fallos: List[tuple] = []
    if backend == "apispain" or (backend == "auto" and api_key):
        try:
            return await asyncio.to_thread(
                mod.buscar_empresa_apispain, nombre, exacta, api_key
            )
        except Exception as exc:
            fallos.append((APISPAIN, exc))
            if backend == "apispain":
                raise FalloBorme(fallos) from exc
    try:
        res = await _buscar_empresa_boe(nombre, params, exacta)
    except Exception as exc:
        raise FalloBorme([*fallos, (FUENTE, exc)]) from exc
    return {**res, "fallos": fallos}


async def _buscar_empresa_boe(
    nombre: str, params: Dict[str, Any], exacta: bool
) -> dict:
    mod = borme()
    return await asyncio.to_thread(
        mod.buscar_empresa,
        nombre,
        params.get("DESDE"),
        params.get("HASTA"),
        int(params.get("DIAS") or 90),
        p_list(params.get("PROVINCIA")),
        p_bool(params.get("COMPLETO")),
        exacta,
        mod.DEFAULT_DB,
        0.5,
        int(params.get("MAX_PETICIONES") or 3000),
    )


async def buscar_persona(nombre: str, params: Dict[str, Any]) -> dict:
    mod = borme()
    return await asyncio.to_thread(
        mod.buscar_persona,
        nombre,
        params.get("DESDE"),
        params.get("HASTA"),
        int(params.get("DIAS") or 30),
        p_list(params.get("PROVINCIA")),
        mod.DEFAULT_DB,
        0.5,
        int(params.get("MAX_PETICIONES") or 1500),
    )


def denominacion(org: Organization) -> str:
    """Nombre con el que buscar en el BORME, que no publica el NIF: el del nodo o, si el nodo se llama como
    su NIF, la denominación resuelta. "" si no hay ninguna."""
    nombre = sin_identificador(org.name)
    if not nombre or load("resolver_empresa").parece_nif(nombre):
        nombre = org_ids(org)["nombre"]
    return nombre if len(borme().normalizar_denominacion(nombre)["base"]) >= 2 else ""


async def consultar_empresa(enricher, org: Organization) -> Optional[dict]:
    """buscar_empresa anotando fallos y cobertura en el enricher. None si no hay respuesta."""
    nombre = denominacion(org)
    if not nombre:
        ev.fallo(
            enricher,
            FUENTE,
            str(org.name),
            "sin denominación utilizable (el BORME no publica el NIF)",
        )
        return None
    api_key = api_key_de(enricher)
    try:
        res = await buscar_empresa(nombre, enricher.params, api_key)
        fallos = res.get("fallos") or []
    except FalloBorme as exc:
        res, fallos = None, exc.fallos
    except Exception as exc:
        res, fallos = None, [(FUENTE, exc)]
    for fuente, exc in fallos:
        ev.fallo(enricher, fuente, nombre, exc, api_key)
    if res is not None:
        anotar_cobertura(enricher, res, nombre)
    return res


def cobertura(res: dict, provincias: Optional[List[str]] = None) -> Optional[str]:
    """Texto de cobertura según el backend y las fechas con las que se ha consultado."""
    if res.get("backend") == "apispain":
        return f"{APISPAIN} desde 2024-01-01 (parcial: hasta 20 empresas y sus actos recientes)"
    rango = res.get("cubierto", [res.get("desde"), res.get("hasta")])
    if not rango or not all(rango):
        return None
    limite = f" (provincias: {', '.join(provincias)})" if provincias else ""
    return f"BORME BOE {rango[0]} a {rango[1]}{limite}"


def anotar_cobertura(enricher, res: dict, nombre: str) -> None:
    """Cobertura real de la consulta y, como fallo, los días del rango que no se han podido consultar."""
    sin = res.get("dias_sin_cubrir") or []
    if sin:
        tramo = f"{sin[0]}" if len(sin) == 1 else f"entre {sin[0]} y {sin[-1]}"
        dias = "1 día" if len(sin) == 1 else f"{len(sin)} días"
        ev.fallo(enricher, FUENTE, nombre, f"{dias} sin cubrir ({tramo})")
    textos = enricher.coverage.split("; ") if enricher.coverage else []
    texto = cobertura(res, p_list(enricher.params.get("PROVINCIA")))
    if texto and texto not in textos:
        enricher.coverage = "; ".join([*textos, texto])


def dudas(org: Organization, empresas: List[dict]) -> List[tuple]:
    """Por empresa: (denominación exacta, motivo de duda o None si es la consultada sin ambigüedad)."""
    norm = borme().normalizar_denominacion
    nombre, ids = denominacion(org), org_ids(org)
    nif = ids["nif"]
    q = norm(nombre)
    normas = [norm(e["empresa"] or "") for e in empresas]
    exactas = [i for i, n in enumerate(normas) if n["base"] == q["base"]]
    # Entre homónimas, el NIF propio del nodo (no uno deducido por nombre) dice cuál es si solo casa una
    con_nif = [
        i
        for i in exactas
        if nif and not ids["identidad_certeza"] and nif_de(empresas[i]) == nif
    ]
    unica = con_nif[0] if len(con_nif) == 1 else None
    out = []
    for i, (e, n) in enumerate(zip(empresas, normas)):
        otro = nif_de(e)
        exacta, duda = i in exactas, None
        if not exacta:
            duda = f"coincidencia parcial con «{nombre}»"
        elif len(exactas) > 1 and i != unica:
            duda = f"{len(exactas)} sociedades con la misma denominación"
        elif not misma_forma(n, q):
            duda = f"distinta forma jurídica que «{nombre}»"
        elif nif and otro and otro != nif:
            duda = f"otro NIF ({otro})"
        out.append((exacta, duda))
    return out


def nif_de(empresa: dict) -> Optional[str]:
    nif = (empresa.get("ficha") or {}).get("nif")
    return load("resolver_empresa").limpiar_nif(str(nif)) if nif else None


def tiene_otro_nif(org: Organization, empresa: dict) -> bool:
    """La empresa del BORME tiene un NIF conocido distinto del de `org`."""
    nif, otro = org_ids(org)["nif"], nif_de(empresa)
    return bool(nif and otro and nif != otro)


def empresa_aparte(empresa: dict, ident: str) -> Organization:
    """Nodo propio, con su identificador en la etiqueta para no escribir sobre otro nodo del sketch."""
    return Organization(
        name=empresa["empresa"], nom_complet=f"{empresa['empresa']} ({ident})"
    )


def misma_forma(a: dict, b: dict) -> bool:
    """Formas jurídicas compatibles entre dos denominaciones normalizadas (SL = SLU; si falta una, se acepta)."""
    fa, fb = a["forma"], b["forma"]
    return not fa or not fb or fa.rstrip("U") == fb.rstrip("U")


def mismas_palabras(a: str, b: str) -> bool:
    """Mismo nombre de persona, palabra por palabra y en cualquier orden."""
    return palabras(a) == palabras(b)


def palabras(nombre: str) -> tuple:
    texto = borme().norm_texto(nombre)
    return tuple(sorted(re.sub(r"[^A-Z0-9 ]", " ", texto).split()))


def misma_empresa(a: str, b: str) -> bool:
    """Misma denominación base y forma jurídica compatible."""
    na, nb = (borme().normalizar_denominacion(x or "") for x in (a, b))
    return bool(na["base"]) and na["base"] == nb["base"] and misma_forma(na, nb)


def url_acto(actos: List[dict], borme_ids: List[str]) -> Optional[str]:
    """URL del último de los `borme_ids` que la tenga."""
    urls = {a.get("borme_id"): a.get("url_pdf") or a.get("url_html") for a in actos}
    return next((urls[i] for i in reversed(borme_ids) if urls.get(i)), None)


def es_persona_juridica(nombre: str) -> bool:
    n = borme().normalizar_denominacion(nombre)
    if (
        n["forma"] or len(n["base"].split()) == 1
    ):  # un solo token (p. ej. socio único "MAJENTESS")
        return True
    return bool(set(n["base"].split()) & PALABRAS_JURIDICAS)


def _title(s: str) -> str:
    return " ".join(
        "-".join(x.capitalize() for x in w.split("-"))
        if w.upper() not in PARTICULAS
        else w.lower()
        for w in s.split()
    )


def individuo(persona_borme: str, **extra) -> Individual:
    """'CABANES VERDU FRANCISCO JOSE' (apellidos primero, formato BORME) -> Individual
    con full_name en orden natural 'Francisco Jose Cabanes Verdu' y el original en aliases."""
    toks = re.sub(r"\s+", " ", persona_borme.replace(",", " ")).strip().split()
    grupos, buf = [], []
    for t in toks:
        buf.append(t)
        if t.upper() not in PARTICULAS:
            grupos.append(" ".join(buf))
            buf = []
    if buf:
        grupos.append(" ".join(buf))
    if "," in persona_borme:  # 'APELLIDOS, NOMBRE'
        ap, nom = persona_borme.split(",", 1)
        apellidos, nombre = ap.strip(), nom.strip()
    elif len(grupos) >= 3:
        apellidos, nombre = " ".join(grupos[:2]), " ".join(grupos[2:])
    elif len(grupos) == 2:
        apellidos, nombre = grupos[0], grupos[1]
    else:
        apellidos, nombre = "", persona_borme
    first, last = _title(nombre), _title(apellidos)
    return Individual(
        full_name=f"{first} {last}".strip(),
        first_name=first or None,
        last_name=last or None,
        aliases=[persona_borme],
        source="BORME",
        **extra,
    )


def individuo_sin_datos(persona_borme: str) -> Individual:
    """Solo el nombre, en orden natural."""
    i = individuo(persona_borme)
    return Individual(
        full_name=i.full_name, first_name=i.first_name, last_name=i.last_name
    )


def org_actualizada(org: Organization, **campos) -> Organization:
    """Copia de `org` (mismo nodeLabel -> mismo nodo) con campos nuevos no vacíos."""
    base = org.model_dump(exclude_unset=True, exclude={"nodeLabel"})
    base.update({k: v for k, v in campos.items() if v not in (None, "", [], {})})
    return Organization(**base)


def municipio_de(domicilio: Optional[str]) -> Optional[str]:
    m = re.search(r"\(([^()]+)\)\s*$", domicilio or "")
    return m.group(1).strip() if m else None


def historial_cargos(actos: List[dict]) -> List[dict]:
    """Por (persona, cargo_codigo): primera/última fecha, última acción y si sigue vigente (inferido)."""
    h: Dict[tuple, dict] = {}
    for a in sorted(actos, key=lambda x: (x["fecha"], x.get("num") or 0)):
        for c in a.get("cargos") or []:
            k = (c["persona"].upper(), c["cargo_codigo"])
            e = h.setdefault(
                k,
                {
                    "persona": c["persona"],
                    "cargo": c["cargo"],
                    "cargo_codigo": c["cargo_codigo"],
                    "desde": None,
                    "hasta": None,
                    "ultima_accion": None,
                    "vigente": False,
                    "borme_ids": [],
                },
            )
            if c["accion"] in ("nombramiento", "reelección", "declaración", "vigente"):
                e["desde"] = e["desde"] if e["vigente"] else a["fecha"]
                e["vigente"], e["hasta"] = True, None
            elif c["accion"] in ("cese", "revocación", "cancelación"):
                e["vigente"], e["hasta"] = False, a["fecha"]
            e["ultima_accion"], e["cargo"] = c["accion"], c["cargo"]
            if a.get("borme_id") and a["borme_id"] not in e["borme_ids"]:
                e["borme_ids"].append(a["borme_id"])
        if "extincion" in (a.get("tipos") or []):
            for e in h.values():
                if e["vigente"]:
                    e["vigente"], e["hasta"] = False, a["fecha"]
    return list(h.values())


def api_key_de(enricher) -> Optional[str]:
    return ev.clave(enricher, "APISPAIN_API_KEY")
