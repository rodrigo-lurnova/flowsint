#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""BORME Sección Primera (actos inscritos): nombre de empresa -> actos, cargos y
administradores (o persona -> empresas donde tiene cargo), con caché SQLite.

Mecanismo observado (boe.es, 2026-10):
  1. Sumario diario (API de datos abiertos, JSON):
       GET https://www.boe.es/datosabiertos/api/borme/sumario/AAAAMMDD
           (Accept: application/json; 404 si ese día no hay BORME)
     -> data.sumario.diario[].seccion[codigo="A"].item[] con un documento por
        provincia: {identificador: "BORME-A-2026-205-28", titulo: "MADRID",
        url_pdf, url_html, url_xml}. El sufijo es el código INE de la provincia.
        El item "-99" es el ÍNDICE ALFABÉTICO DE SOCIEDADES del día
        (denominación -> documento provincial + nº de anuncio).
  2. Texto de cada documento (HTML, mismo contenido que el PDF/XML):
       GET https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-205-28
     -> <h5 class="articulo">472511 - CH LOCATIONS SL.</h5>
        <p class="parrafo">Constitución. Comienzo de operaciones: 30.03.26. Objeto
        social: ... Domicilio: ... Capital: 3.000,00 Euros. Nombramientos.
        Adm. Unico: APELLIDOS NOMBRE.  Datos registrales. S 8 , H A 208710, ...</p>
     Se usa txt.php y no xml.php porque robots.txt de boe.es excluye
     /diario_borme/xml.php (no así txt.php). No hace falta parsear PDFs.
  No hay buscador público por denominación para la Sección Primera (el de
  /buscar/anborme.php solo cubre la Sección Segunda, anuncios). LibreBORME
  (librebor.me) responde con un challenge de Cloudflare y OpenMercantil/bormeapi
  no resuelven/no responden desde aquí: no se usan. Alternativa con clave:
  apispain.es (--backend apispain, ver apispain.py).

Estrategia y coste (por eso hay caché en ~/.cache/osint-es/borme.sqlite):
  - modo índice (por defecto): por día hábil 1 sumario (~30 KB) + índice
    alfabético (~0,9 MB) y solo los documentos provinciales donde sale la
    empresa. 90 días ≈ 63 días hábiles ≈ 130 peticiones ≈ 60 MB, ~2-3 min.
  - --provincia MADRID: sumario + documento de esa provincia (50-250 KB):
    90 días ≈ 130 peticiones y 5-15 MB. Es lo más barato si se conoce.
  - --completo (obligatorio para buscar personas): todos los documentos
    provinciales: ~30/día -> 90 días ≈ 1.900 peticiones ≈ 90 MB, ~20-25 min con
    la pausa por defecto. Todo queda en caché y se reutiliza en otras consultas.
  Pausa de --pausa s (0,5 por defecto) entre peticiones de red; los días ya
  consultados no se vuelven a pedir.

`administradores_vigentes` es una INFERENCIA PARCIAL: nombramientos/reelecciones
sin cese/revocación posterior DENTRO DEL RANGO OBSERVADO. Un cargo nombrado antes
de --desde no aparece; uno cesado antes tampoco se descuenta.

Uso:
  borme.py "LURNOVA"                         # últimos 90 días, modo índice
  borme.py "CH LOCATIONS SL" --exacta --desde 2026-10-01 --json
  borme.py "IBERDROLA" --provincia BIZKAIA --dias 365
  borme.py --persona "GARCIA LOPEZ JUAN" --dias 30   # implica --completo
  borme.py "LURNOVA" --backend apispain      # requiere APISPAIN_API_KEY
  borme.py "LURNOVA" --solo-cache            # sin red
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata

import httpx

SUMARIO_URL = "https://www.boe.es/datosabiertos/api/borme/sumario/{fecha}"
TXT_URL = "https://www.boe.es/diario_borme/txt.php?id={id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (osint-es-scripts; borme.py; consultas puntuales)",
           "Accept-Language": "es-ES,es;q=0.9"}
DEFAULT_DB = os.getenv("BORME_DB") or os.path.join(
    os.getenv("OSINT_ES_CACHE_DIR") or os.path.expanduser("~/.cache/osint-es"), "borme.sqlite")
AVISO_VIGENTES = ("Inferencia parcial: nombramientos/reelecciones sin cese, revocación o cancelación "
                  "posterior dentro del rango de fechas observado. No equivale a una nota simple del "
                  "Registro Mercantil.")

# ----------------------------------------------------------------------------------------
# Normalización
# ----------------------------------------------------------------------------------------

FORMAS = [  # (regex sobre texto normalizado sin puntos, forma canónica); orden: más largas primero
    (r"SOCIEDAD LIMITADA PROFESIONAL UNIPERSONAL", "SLPU"), (r"SOCIEDAD LIMITADA PROFESIONAL", "SLP"),
    (r"SOCIEDAD LIMITADA UNIPERSONAL", "SLU"), (r"SOCIEDAD LIMITADA NUEVA EMPRESA", "SLNE"),
    (r"SOCIEDAD LIMITADA LABORAL", "SLL"), (r"SOCIEDAD DE RESPONSABILIDAD LIMITADA", "SL"),
    (r"SOCIEDAD LIMITADA", "SL"), (r"SOCIEDAD ANONIMA UNIPERSONAL", "SAU"),
    (r"SOCIEDAD ANONIMA LABORAL", "SAL"), (r"SOCIEDAD ANONIMA DEPORTIVA", "SAD"),
    (r"SOCIEDAD ANONIMA", "SA"), (r"SOCIEDAD COOPERATIVA(?: [A-Z]+)?", "SCOOP"),
    (r"AGRUPACION DE INTERES ECONOMICO", "AIE"), (r"SOCIEDAD CIVIL PROFESIONAL", "SCP"),
    (r"SOCIEDAD CIVIL", "SC"), (r"SOCIEDAD COMANDITARIA", "SCOM"),
    (r"S L P U|SLPU", "SLPU"), (r"S L P|SLP", "SLP"), (r"S L U|SLU", "SLU"), (r"S L N E|SLNE", "SLNE"),
    (r"S L L|SLL", "SLL"), (r"S R L|SRL", "SL"), (r"S L|SL", "SL"), (r"S A U|SAU", "SAU"),
    (r"S A L|SAL", "SAL"), (r"S A D|SAD", "SAD"), (r"S A|SA", "SA"),
    (r"S COOP(?: [A-Z]+)?|SCOOP|COOP", "SCOOP"), (r"A I E|AIE", "AIE"), (r"S C P|SCP", "SCP"),
]
_FORMAS_RE = [(re.compile(rf"(?:^|\s)(?:{p})$"), f) for p, f in FORMAS]


def norm_texto(s: str) -> str:
    """Mayúsculas, sin acentos (Ñ -> N), espacios colapsados."""
    s = "".join(c for c in unicodedata.normalize("NFKD", (s or "").upper()) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def normalizar_denominacion(nombre: str) -> dict:
    """'Lurnova, S.L.U. (en liquidación)' -> {'base': 'LURNOVA', 'forma': 'SLU', 'norm': 'LURNOVA SLU'}."""
    s = norm_texto(nombre)
    s = re.sub(r"\((?:R\.?M\.?|REGISTRO)[^)]*\)", " ", s)
    s = re.sub(r"\b(EN LIQUIDACION|SOCIEDAD ABSORBENTE|SOCIEDAD ABSORBIDA|SOCIEDAD BENEFICIARIA|"
               r"SOCIEDAD ESCINDIDA|UNIPERSONAL)\b(?=\W*$|\W*\))", " ", s)
    s = re.sub(r"[()]", " ", s)
    s = s.replace("&", " & ").replace(".", "")
    s = re.sub(r"[^A-Z0-9& ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    forma = None
    for rx, f in _FORMAS_RE:
        m = rx.search(s)
        if m and m.start() > 0:
            forma, s = f, s[:m.start()].strip()
            break
    return {"base": s, "forma": forma, "norm": f"{s} {forma}".strip() if forma else s}


def coincide(consulta: dict, candidato: dict, exacta: bool = False) -> bool:
    """Compara denominaciones normalizadas. Por defecto: la base consultada aparece como
    secuencia de palabras completa en la base candidata. `exacta`: bases iguales (y forma, si se dio)."""
    qb, cb = consulta["base"], candidato["base"]
    if not qb:
        return False
    if exacta:
        return qb == cb and (not consulta["forma"] or consulta["forma"] == candidato["forma"])
    return f" {qb} " in f" {cb} "


CARGO_CODIGOS = [  # (prefijo de la etiqueta sin espacios/puntos, código)
    ("SOCIOUNICO", "SOCIO_UNICO"), ("ADMUNI", "ADM_UNICO"), ("ADMINISTRADORUNICO", "ADM_UNICO"),
    ("ADMSOLID", "ADM_SOLIDARIO"), ("ADMMANCOM", "ADM_MANCOMUNADO"), ("ADMCONJ", "ADM_MANCOMUNADO"),
    ("ADMCONC", "ADM_CONCURSAL"), ("ADMSUPL", "ADM_SUPLENTE"), ("ADMINISTR", "ADMINISTRADOR"),
    ("CONSDEL", "CONSEJERO_DELEGADO"), ("CONDELEG", "CONSEJERO_DELEGADO"),
    ("CONSEJERODELEG", "CONSEJERO_DELEGADO"), ("CONSJDEL", "CONSEJERO_DELEGADO"),
    ("DELEG", "CONSEJERO_DELEGADO"), ("CONSEJ", "CONSEJERO"), ("CONSJ", "CONSEJERO"), ("CONSIND", "CONSEJERO"),
    ("VICEPRES", "VICEPRESIDENTE"), ("PRESIDENT", "PRESIDENTE"), ("PRESID", "PRESIDENTE"),
    ("SECRENOCONS", "SECRETARIO_NO_CONSEJERO"), ("SECRNOCONS", "SECRETARIO_NO_CONSEJERO"),
    ("VSECR", "VICESECRETARIO"), ("VICESEC", "VICESECRETARIO"), ("SECR", "SECRETARIO"),
    ("LIQ", "LIQUIDADOR"), ("APO", "APODERADO"), ("AUD", "AUDITOR"),
    ("REPR", "REPRESENTANTE"), ("RLC", "REPRESENTANTE"), ("SOCPROF", "SOCIO_PROFESIONAL"),
    ("MIEM", "MIEMBRO_COMITE"), ("MIE", "MIEMBRO_COMITE"), ("PRECOM", "MIEMBRO_COMITE"),
    ("SECCOM", "MIEMBRO_COMITE"), ("ENTDEP", "ENTIDAD_DEPOSITARIA"), ("ENTIDDEP", "ENTIDAD_DEPOSITARIA"),
    ("DIRECTOR", "DIRECTOR"), ("GERENTE", "DIRECTOR"),
]


def cargo_codigo(etiqueta: str) -> str:
    k = re.sub(r"[^A-Z0-9]", "", norm_texto(etiqueta))
    for pref, cod in CARGO_CODIGOS:
        if k.startswith(pref):
            return cod
    return re.sub(r"[^A-Z0-9]+", "_", norm_texto(etiqueta)).strip("_") or "CARGO"


# ----------------------------------------------------------------------------------------
# Parser del texto de un acto
# ----------------------------------------------------------------------------------------

CABECERAS = {  # cabecera literal -> tipo
    "Constitución": "constitucion", "Primera sucursal de sociedad extranjera": "constitucion_sucursal",
    "Nombramientos": "nombramiento", "Reelecciones": "reeleccion", "Ceses/Dimisiones": "cese",
    "Revocaciones": "revocacion", "Cancelaciones de oficio de nombramientos": "cancelacion_oficio",
    "Modificaciones estatutarias": "modificacion_estatutos",
    "Cambio de domicilio social": "cambio_domicilio", "Cambio de objeto social": "cambio_objeto",
    "Ampliación del objeto social": "cambio_objeto", "Ampliacion del objeto social": "cambio_objeto",
    "Cambio de denominación social": "cambio_denominacion",
    "Ampliación de capital": "ampliacion_capital", "Reducción de capital": "reduccion_capital",
    "Desembolso de dividendos pasivos": "desembolso",
    "Declaración de unipersonalidad": "socio_unico", "Sociedad unipersonal": "socio_unico",
    "Cambio de identidad del socio único": "socio_unico",
    "Pérdida del carácter de unipersonalidad": "perdida_unipersonalidad",
    "Pérdida del caracter de unipersonalidad": "perdida_unipersonalidad",
    "Disolución": "disolucion", "Extinción": "extincion", "Reactivación de la sociedad": "reactivacion",
    "Fusión por absorción": "fusion", "Fusión por unión": "fusion", "Escisión parcial": "escision",
    "Escisión total": "escision", "Segregación": "escision", "Cesión global de activo y pasivo": "cesion_global",
    "Transformación de sociedad": "transformacion", "Situación concursal": "concursal",
    "Modificación de poderes": "modificacion_poderes", "Emisión de obligaciones": "emision_obligaciones",
    "Página web de la sociedad": "pagina_web", "Depósito de libros": "deposito_libros",
    "Primera inscripción (O.M. 10/6/1.997)": "primera_inscripcion", "Primera inscripción": "primera_inscripcion",
    "Reapertura hoja registral": "reapertura", "Cierre provisional hoja registral": "cierre_provisional",
    "Cierre provisional de la hoja registral": "cierre_provisional", "Adaptación Ley 2/95": "adaptacion",
    "Adaptación Ley 44/2015": "adaptacion", "Acuerdo de ampliación de capital social sin ejecutar": "ampliacion_capital",
    "Crédito incobrable": "credito_incobrable", "Empresario Individual": "empresario_individual",
    "Otros conceptos": "otros", "Fe de erratas": "fe_erratas", "Datos registrales": "datos_registrales",
}
_CAB_RE = re.compile(r"(?:^|(?<=\.\s)|(?<=\.\s\s))(" + "|".join(
    re.escape(k) for k in sorted(CABECERAS, key=len, reverse=True)) + r")(?=[.:,\s]|$)[.:]?\s*")
ACCIONES = {"nombramiento": "nombramiento", "reeleccion": "reelección", "cese": "cese",
            "revocacion": "revocación", "cancelacion_oficio": "cancelación"}
_LABEL_RE = re.compile(r"(?:^|(?<=\.)|(?<=\.\s)|(?<=\.\s\s))\s*((?:Adm\.\s)?(?:(?!\.\s)[^:;]){1,26}?)\s*:\s+")
_SUB_CONSTITUCION = re.compile(r"(Comienzo de operaciones|Objeto social|Domicilio|Capital|Duración)\s*:\s*")


def _limpia(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().rstrip(".").strip()


def _fecha_corta(s: str) -> str | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s or "")
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 and y < 70 else (y + 1900 if y < 100 else y)
    try:
        return dt.date(y, mth, d).isoformat()
    except ValueError:
        return None


def _importe(s: str) -> float | None:
    m = re.search(r"([\d.]+(?:,\d+)?)\s*(Euros|Ptas)", s or "")
    if not m:
        return None
    v = float(m.group(1).replace(".", "").replace(",", "."))
    return round(v / 166.386, 2) if m.group(2) == "Ptas" else v


def parse_cargos(contenido: str, accion: str) -> list[dict]:
    """'Adm. Unico: X. Apoderado: A;B.' -> [{accion, cargo, cargo_codigo, persona}, ...]"""
    out = []
    ms = list(_LABEL_RE.finditer(contenido))
    for i, m in enumerate(ms):
        fin = ms[i + 1].start() if i + 1 < len(ms) else len(contenido)
        cargo = _limpia(m.group(1))
        for persona in contenido[m.end():fin].split(";"):
            persona = _limpia(persona)
            if persona:
                out.append({"accion": accion, "cargo": cargo, "cargo_codigo": cargo_codigo(cargo),
                            "persona": persona})
    return out


def parse_acto(texto: str) -> dict:
    """Trocea el texto de un anuncio de la Sección A en sus actos y extrae cargos y datos."""
    texto = re.sub(r"\s+", " ", texto or "").strip()
    texto = re.sub(r"(?<=[^.\s])\s+(?=Datos registrales\.)", ". ", texto)
    ms = list(_CAB_RE.finditer(texto))
    tipos, cargos, datos = [], [], {}
    for i, m in enumerate(ms):
        cab = m.group(1)
        tipo = CABECERAS[cab]
        cont = texto[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(texto)].strip()
        if tipo != "datos_registrales" and tipo not in tipos:
            tipos.append(tipo)
        if tipo in ACCIONES:
            cargos += parse_cargos(cont, ACCIONES[tipo])
        elif tipo in ("constitucion", "constitucion_sucursal"):
            sub = list(_SUB_CONSTITUCION.finditer(cont))
            for j, s in enumerate(sub):
                val = _limpia(cont[s.end():sub[j + 1].start() if j + 1 < len(sub) else len(cont)])
                k = {"Comienzo de operaciones": "comienzo_operaciones", "Objeto social": "objeto_social",
                     "Domicilio": "domicilio", "Capital": "capital_texto", "Duración": "duracion"}[s.group(1)]
                datos[k] = val
            if "capital_texto" in datos:
                datos["capital"] = _importe(datos["capital_texto"] + " ")
        elif tipo == "cambio_domicilio":
            datos["domicilio"] = _limpia(cont)
        elif tipo == "cambio_objeto":
            datos["objeto_social"] = _limpia(cont)
        elif tipo == "cambio_denominacion":
            datos["denominacion_nueva"] = _limpia(cont)
        elif tipo in ("ampliacion_capital", "reduccion_capital"):
            m2 = re.search(r"Resultante Suscrito:\s*([^.]*(?:\.\d{3})*[^.]*?(?:Euros|Ptas))", cont)
            datos["capital"] = _importe(m2.group(1)) if m2 else None
            datos.setdefault("capital_operacion", _importe(cont))
        elif tipo == "socio_unico":
            m2 = re.search(r"(?:Socio único|socio único)\s*:\s*(.+?)(?:\.\s|\.$|$)", cont)
            if m2:
                datos["socio_unico"] = _limpia(m2.group(1))
                cargos.append({"accion": "declaración", "cargo": "Socio único", "cargo_codigo": "SOCIO_UNICO",
                               "persona": datos["socio_unico"]})
        elif tipo == "perdida_unipersonalidad":
            datos["socio_unico"] = None
        elif tipo == "transformacion":
            m2 = re.search(r"Denominación y forma adoptada:\s*(.+?)(?:\.\s|\.$|$)", cont)
            if m2:
                datos["denominacion_nueva"] = _limpia(m2.group(1))
        elif tipo == "fusion":
            m2 = re.search(r"Sociedades absorbidas:\s*(.+?)(?:\.\s|\.$|$)", cont)
            if m2:
                datos["sociedades_absorbidas"] = [_limpia(x) for x in m2.group(1).split(";")]
        elif tipo == "pagina_web":
            m2 = re.search(r"(https?://\S+?|www\.\S+?)\.?(?:\s|$)", cont)
            if m2:
                datos["pagina_web"] = m2.group(1)
        elif tipo == "disolucion":
            datos["disolucion"] = _limpia(cont) or True
        elif tipo == "otros":
            datos["otros_conceptos"] = _limpia(cont)
        elif tipo == "datos_registrales":
            datos["datos_registrales"] = _limpia(cont)
            h = re.search(r"\bH ([A-Z]{1,3}) ?(\d+)", cont)
            if h:
                datos["hoja"] = f"{h.group(1)} {h.group(2)}"
            ins = re.search(r"I/A (\d+)", cont)
            if ins:
                datos["inscripcion"] = int(ins.group(1))
            f = re.search(r"\(\s*(\d{1,2}\.\d{1,2}\.\d{2,4})\)", cont)
            if f:
                datos["fecha_inscripcion"] = _fecha_corta(f.group(1))
    return {"tipos": tipos or ["otro"], "cargos": cargos, "datos": datos}


# ----------------------------------------------------------------------------------------
# Descarga y caché
# ----------------------------------------------------------------------------------------

def abrir_db(path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sumarios (fecha TEXT PRIMARY KEY, estado TEXT, items TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS docs (doc_id TEXT PRIMARY KEY, fecha TEXT, provincia TEXT, url_pdf TEXT,
        url_html TEXT, n_actos INTEGER, ts TEXT);
    CREATE TABLE IF NOT EXISTS actos (doc_id TEXT, num INTEGER, fecha TEXT, provincia TEXT, empresa TEXT,
        empresa_norm TEXT, texto TEXT, texto_norm TEXT, PRIMARY KEY (doc_id, num));
    CREATE INDEX IF NOT EXISTS actos_empresa ON actos(empresa_norm);
    CREATE INDEX IF NOT EXISTS actos_fecha ON actos(fecha);
    CREATE TABLE IF NOT EXISTS indices (fecha TEXT PRIMARY KEY, n INTEGER, ts TEXT);
    CREATE TABLE IF NOT EXISTS indice (fecha TEXT, doc_id TEXT, num INTEGER, empresa TEXT, empresa_norm TEXT,
        PRIMARY KEY (doc_id, num, empresa));
    CREATE INDEX IF NOT EXISTS indice_empresa ON indice(empresa_norm);
    """)
    return db


class Descargador:
    """HTTP educado contra boe.es: pausa entre peticiones, reintentos en 5xx, contador de coste."""

    def __init__(self, pausa: float = 0.5, max_peticiones: int = 3000, progreso=None, timeout: float = 60):
        self.client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self.pausa, self.max_peticiones, self.progreso = pausa, max_peticiones, progreso
        self.peticiones, self.bytes, self._ultima = 0, 0, 0.0

    def close(self):
        self.client.close()

    def get(self, url: str, **kw) -> httpx.Response:
        if self.peticiones >= self.max_peticiones:
            raise RuntimeError(f"Tope de {self.max_peticiones} peticiones alcanzado (--max-peticiones).")
        for intento in range(3):
            espera = self.pausa - (time.monotonic() - self._ultima)
            if espera > 0:
                time.sleep(espera)
            self._ultima = time.monotonic()
            self.peticiones += 1
            try:
                r = self.client.get(url, **kw)
            except httpx.TransportError:
                if intento == 2:
                    raise
                time.sleep(3 * (intento + 1))
                continue
            self.bytes += len(r.content)
            if r.status_code in (403, 429):
                raise RuntimeError(f"boe.es respondió {r.status_code} (límite/WAF): se detiene la descarga.")
            if r.status_code >= 500 and intento < 2:
                time.sleep(3 * (intento + 1))
                continue
            if self.progreso and self.peticiones % 20 == 0:
                self.progreso(f"[borme] {self.peticiones} peticiones, {self.bytes / 1e6:.1f} MB")
            return r
        return r


def _ahora() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sumario(db, dl: Descargador | None, fecha: dt.date) -> list[dict] | None:
    """Items de la Sección A del día (con caché). None si no hay BORME ese día."""
    f = fecha.strftime("%Y%m%d")
    row = db.execute("SELECT estado, items FROM sumarios WHERE fecha=?", (f,)).fetchone()
    if row:
        return json.loads(row[1]) if row[0] == "ok" else None
    if dl is None:
        return None
    r = dl.get(SUMARIO_URL.format(fecha=f), headers={"Accept": "application/json"})
    if r.status_code == 404:
        if fecha < dt.date.today():
            db.execute("INSERT OR REPLACE INTO sumarios VALUES (?,?,?,?)", (f, "sin_borme", "[]", _ahora()))
            db.commit()
        return None
    r.raise_for_status()
    items = []
    for diario in r.json()["data"]["sumario"].get("diario", []):
        diario = diario if isinstance(diario, list) else [diario]
        for d in diario:
            secs = d.get("seccion") or []
            for s in secs if isinstance(secs, list) else [secs]:
                if s.get("codigo") != "A":
                    continue
                its = s.get("item") or []
                for it in its if isinstance(its, list) else [its]:
                    items.append({"id": it["identificador"], "provincia": it.get("titulo"),
                                  "url_pdf": (it.get("url_pdf") or {}).get("texto"),
                                  "url_html": it.get("url_html") or TXT_URL.format(id=it["identificador"])})
    db.execute("INSERT OR REPLACE INTO sumarios VALUES (?,?,?,?)", (f, "ok", json.dumps(items), _ahora()))
    db.commit()
    return items


def parse_documento(html_txt: str) -> list[tuple[int, str, str]]:
    """HTML de un documento provincial -> [(num, empresa, texto)]."""
    out = []
    partes = re.split(r'<h5 class="articulo">', html_txt)[1:]
    for p in partes:
        cab, _, resto = p.partition("</h5>")
        cab = html.unescape(re.sub(r"<[^>]+>", "", cab)).strip()
        m = re.match(r"(\d+)\s*-\s*(.+?)\.?$", cab)
        if not m:
            continue
        resto = resto.split("</div>")[0]
        texto = " ".join(html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
                         for x in re.findall(r'<p class="parrafo[^"]*">(.*?)</p>', resto, re.S))
        out.append((int(m.group(1)), m.group(2).strip(), texto))
    return out


def documento(db, dl: Descargador | None, item: dict, fecha: dt.date) -> bool:
    """Descarga (si no está en caché) y guarda los actos de un documento provincial."""
    if db.execute("SELECT 1 FROM docs WHERE doc_id=?", (item["id"],)).fetchone():
        return True
    if dl is None:
        return False
    r = dl.get(TXT_URL.format(id=item["id"]))
    r.raise_for_status()
    actos = parse_documento(r.text)
    f = fecha.isoformat()
    db.executemany("INSERT OR REPLACE INTO actos VALUES (?,?,?,?,?,?,?,?)",
                   [(item["id"], n, f, item["provincia"], e, normalizar_denominacion(e)["norm"], t, norm_texto(t))
                    for n, e, t in actos])
    db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?)",
               (item["id"], f, item["provincia"], item["url_pdf"], item["url_html"], len(actos), _ahora()))
    db.commit()
    return True


def indice_dia(db, dl: Descargador | None, item99: dict, fecha: dt.date) -> bool:
    """Descarga y guarda el índice alfabético del día (denominación -> doc provincial, nº)."""
    f = fecha.isoformat()
    if db.execute("SELECT 1 FROM indices WHERE fecha=?", (f,)).fetchone():
        return True
    if dl is None:
        return False
    r = dl.get(TXT_URL.format(id=item99["id"]))
    r.raise_for_status()
    filas = re.findall(r"<td>([^<]+)</td>\s*<td>\s*<p[^>]*>(BORME-A-[\d-]+)</p>\s*</td>\s*<td[^>]*>\s*\((\d+)\)",
                       r.text)
    rows = []
    for emp, doc, num in filas:
        emp = html.unescape(emp).strip().rstrip(".")
        rows.append((f, doc, int(num), emp, normalizar_denominacion(emp)["norm"]))
    db.executemany("INSERT OR IGNORE INTO indice VALUES (?,?,?,?,?)", rows)
    db.execute("INSERT OR REPLACE INTO indices VALUES (?,?,?)", (f, len(rows), _ahora()))
    db.commit()
    return True


def _prov_ok(item: dict, provincias: list[str] | None) -> bool:
    if not provincias:
        return True
    cod, tit = item["id"].rsplit("-", 1)[-1], norm_texto(item["provincia"])
    return any(p == cod or norm_texto(p) in tit for p in provincias)


def parse_fecha(s: str | dt.date | None) -> dt.date | None:
    if s is None or isinstance(s, dt.date):
        return s
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Fecha no válida: {s} (usa AAAA-MM-DD o dd/mm/aaaa)")


def dias_habiles(desde: dt.date, hasta: dt.date):
    d = hasta
    while d >= desde:  # de más reciente a más antiguo: lo nuevo primero
        if d.weekday() < 5:
            yield d
        d -= dt.timedelta(days=1)


def cubrir(db, dl: Descargador | None, desde: dt.date, hasta: dt.date, provincias=None,
           completo: bool = False, consulta: dict | None = None, exacta: bool = False) -> dict:
    """Asegura en caché los documentos necesarios del rango. Devuelve estadística de cobertura."""
    est = {"dias_habiles": 0, "dias_con_borme": 0, "docs": 0, "sin_red": dl is None}
    for d in dias_habiles(desde, hasta):
        est["dias_habiles"] += 1
        items = sumario(db, dl, d)
        if not items:
            continue
        est["dias_con_borme"] += 1
        provs = [i for i in items if not i["id"].endswith("-99")]
        if provincias or completo:
            objetivo = [i for i in provs if _prov_ok(i, provincias)]
        else:
            i99 = next((i for i in items if i["id"].endswith("-99")), None)
            if not i99 or not indice_dia(db, dl, i99, d) or consulta is None:
                objetivo = []
            else:
                ids = {doc for doc, en in db.execute(
                    "SELECT doc_id, empresa_norm FROM indice WHERE fecha=? AND empresa_norm LIKE ?",
                    (d.isoformat(), f"%{consulta['base']}%"))
                    if coincide(consulta, normalizar_denominacion(en), exacta)}
                objetivo = [i for i in provs if i["id"] in ids]
        for it in objetivo:
            if documento(db, dl, it, d):
                est["docs"] += 1
    return est


# ----------------------------------------------------------------------------------------
# Consultas
# ----------------------------------------------------------------------------------------

def _acto_dict(row) -> dict:
    doc_id, num, fecha, provincia, empresa, texto, url_pdf, url_html = row
    p = parse_acto(texto)
    return {"fecha": fecha, "provincia": provincia, "num": num, "empresa": empresa,
            "tipo": p["tipos"][0], "tipos": p["tipos"], "texto": texto, "cargos": p["cargos"],
            "datos": p["datos"], "borme_id": doc_id, "url_pdf": url_pdf, "url_html": url_html}


def inferir(actos: list[dict]) -> dict:
    """Recorre los actos en orden cronológico -> ficha + administradores vigentes (inferencia parcial)."""
    vig: dict = {}
    ficha = {"denominacion": None, "forma_juridica": None, "nif": None, "provincia": None, "hoja_registral": None,
             "domicilio": None, "objeto_social": None, "capital": None, "fecha_constitucion": None,
             "comienzo_operaciones": None, "socio_unico": None, "pagina_web": None, "estado": "desconocido",
             "denominaciones": [], "ultimo_acto": None}
    for a in sorted(actos, key=lambda x: (x["fecha"], x.get("num") or 0)):
        d, f = a.get("datos") or {}, a["fecha"]
        if a.get("empresa") and a["empresa"] not in ficha["denominaciones"]:
            ficha["denominaciones"].append(a["empresa"])
        ficha["denominacion"] = d.get("denominacion_nueva") or a.get("empresa") or ficha["denominacion"]
        ficha["provincia"] = a.get("provincia") or ficha["provincia"]
        ficha["hoja_registral"] = d.get("hoja") or ficha["hoja_registral"]
        for k in ("domicilio", "objeto_social", "pagina_web", "comienzo_operaciones"):
            if d.get(k):
                ficha[k] = d[k]
        if d.get("capital") is not None:
            ficha["capital"] = d["capital"]
        if "socio_unico" in d:
            ficha["socio_unico"] = d["socio_unico"]
        tipos = a.get("tipos") or [a.get("tipo")]
        if "constitucion" in tipos or "constitucion_sucursal" in tipos:
            ficha["fecha_constitucion"] = d.get("fecha_inscripcion") or f
            ficha["estado"] = "activa"
        if "concursal" in tipos:
            ficha["estado"] = "concurso"
        if "disolucion" in tipos:
            ficha["estado"] = "disuelta"
            for k in [k for k in vig if vig[k]["cargo_codigo"] not in ("LIQUIDADOR", "AUDITOR", "SOCIO_UNICO")]:
                vig.pop(k)
        if "extincion" in tipos:
            ficha["estado"] = "extinguida"
            vig.clear()
        if "reactivacion" in tipos:
            ficha["estado"] = "activa"
        if ficha["estado"] == "desconocido" and tipos and tipos != ["fe_erratas"]:
            ficha["estado"] = "activa (inferido: con actos inscritos recientes)"
        ficha["ultimo_acto"] = f
        for c in a.get("cargos") or []:
            if c["cargo_codigo"] == "SOCIO_UNICO":
                for k in [k for k in vig if vig[k]["cargo_codigo"] == "SOCIO_UNICO"]:
                    vig.pop(k)
            k = (norm_texto(c["persona"]), c["cargo_codigo"])
            if c["accion"] in ("nombramiento", "reelección", "declaración", "vigente"):
                prev = vig.get(k)
                vig[k] = {"persona": c["persona"], "cargo": c["cargo"], "cargo_codigo": c["cargo_codigo"],
                          "desde": prev["desde"] if prev else f, "ultimo_acto": f, "borme_id": a.get("borme_id")}
            elif c["accion"] in ("cese", "revocación", "cancelación"):
                vig.pop(k, None)
        if "perdida_unipersonalidad" in tipos:
            for k in [k for k in vig if vig[k]["cargo_codigo"] == "SOCIO_UNICO"]:
                vig.pop(k)
    if ficha["denominacion"]:
        n = normalizar_denominacion(ficha["denominacion"])
        ficha["forma_juridica"] = n["forma"]
    return {"ficha": ficha, "administradores_vigentes": sorted(vig.values(), key=lambda x: (x["cargo_codigo"], x["persona"]))}


def _agrupar(actos: list[dict]) -> list[dict]:
    """Agrupa por hoja registral (o denominación) y calcula ficha/vigentes de cada sociedad."""
    grupos: dict = {}
    for a in actos:
        clave = a["datos"].get("hoja") and f"{a['provincia']}|{a['datos']['hoja']}" or normalizar_denominacion(a["empresa"])["norm"]
        grupos.setdefault(clave, []).append(a)
    out = []
    for g in grupos.values():
        g.sort(key=lambda x: (x["fecha"], x.get("num") or 0), reverse=True)
        inf = inferir(g)
        out.append({"empresa": inf["ficha"]["denominacion"], "ficha": inf["ficha"], "actos": g,
                    "administradores_vigentes": inf["administradores_vigentes"]})
    out.sort(key=lambda e: e["actos"][0]["fecha"], reverse=True)
    return out


def rango(desde=None, hasta=None, dias: int = 90) -> tuple[dt.date, dt.date]:
    hasta = parse_fecha(hasta) or dt.date.today()
    desde = parse_fecha(desde) or hasta - dt.timedelta(days=dias)
    return desde, hasta


def buscar_empresa(nombre: str, desde=None, hasta=None, dias: int = 90, provincias: list[str] | None = None,
                   completo: bool = False, exacta: bool = False, db_path: str = DEFAULT_DB, pausa: float = 0.5,
                   max_peticiones: int = 3000, solo_cache: bool = False, progreso=None) -> dict:
    """Nombre de sociedad -> {consulta, empresas: [{empresa, ficha, actos, administradores_vigentes}], coste}."""
    desde, hasta = rango(desde, hasta, dias)
    q = normalizar_denominacion(nombre)
    db = abrir_db(db_path)
    dl = None if solo_cache else Descargador(pausa, max_peticiones, progreso)
    try:
        est = cubrir(db, dl, desde, hasta, provincias, completo, q, exacta)
        sel = ("SELECT a.doc_id, a.num, a.fecha, a.provincia, a.empresa, a.texto, d.url_pdf, d.url_html "
               "FROM actos a LEFT JOIN docs d USING(doc_id) ")
        rows = db.execute(sel + "WHERE a.empresa_norm LIKE ? AND a.fecha BETWEEN ? AND ?",
                          (f"%{q['base']}%", desde.isoformat(), hasta.isoformat())).fetchall()
        actos = [_acto_dict(r) for r in rows
                 if coincide(q, normalizar_denominacion(r[4]), exacta)
                 and (not provincias or _prov_ok({"id": r[0], "provincia": r[3]}, provincias))]
        # La hoja registral no cambia con la denominación: se añaden los actos publicados con nombres
        # anteriores o posteriores (constitución, cargos...) de las mismas hojas.
        vistos = {(a["borme_id"], a.get("num")) for a in actos}
        for prov, hoja in {(a["provincia"], a["datos"]["hoja"]) for a in actos if a["datos"].get("hoja")}:
            for r in db.execute(sel + "WHERE a.texto LIKE ? AND a.provincia = ? AND a.fecha BETWEEN ? AND ?",
                                (f"%H {hoja},%", prov, desde.isoformat(), hasta.isoformat())).fetchall():
                a = _acto_dict(r)
                if a["datos"].get("hoja") == hoja and (a["borme_id"], a.get("num")) not in vistos:
                    vistos.add((a["borme_id"], a.get("num")))
                    actos.append(a)
    finally:
        if dl:
            dl.close()
        db.close()
    empresas = _agrupar(actos)
    empresas.sort(key=lambda e: normalizar_denominacion(e["empresa"] or "")["base"] != q["base"])
    return {"consulta": nombre, "normalizada": q["norm"], "backend": "boe", "desde": desde.isoformat(),
            "hasta": hasta.isoformat(), "modo": "provincia" if provincias else ("completo" if completo else "indice"),
            "coste": {**est, "peticiones": dl.peticiones if dl else 0, "mb": round(dl.bytes / 1e6, 2) if dl else 0},
            "aviso": AVISO_VIGENTES, "empresas": empresas}


def buscar_persona(nombre: str, desde=None, hasta=None, dias: int = 30, provincias: list[str] | None = None,
                   db_path: str = DEFAULT_DB, pausa: float = 0.5, max_peticiones: int = 3000,
                   solo_cache: bool = False, progreso=None) -> dict:
    """Persona (en cualquier orden de nombre/apellidos) -> empresas donde figura con cargo en el rango.
    Requiere los documentos completos del rango (los descarga si no están en caché)."""
    desde, hasta = rango(desde, hasta, dias)
    tokens = [t for t in re.sub(r"[^A-Z0-9 ]", " ", norm_texto(nombre)).split() if len(t) > 1]
    if not tokens:
        raise ValueError("Nombre de persona vacío")
    db = abrir_db(db_path)
    dl = None if solo_cache else Descargador(pausa, max_peticiones, progreso)
    try:
        est = cubrir(db, dl, desde, hasta, provincias, completo=True)
        sql = ("SELECT a.doc_id, a.num, a.fecha, a.provincia, a.empresa, a.texto, d.url_pdf, d.url_html "
               "FROM actos a LEFT JOIN docs d USING(doc_id) WHERE a.fecha BETWEEN ? AND ? "
               + " ".join("AND a.texto_norm LIKE ?" for _ in tokens))
        rows = db.execute(sql, (desde.isoformat(), hasta.isoformat(), *[f"%{t}%" for t in tokens])).fetchall()
    finally:
        if dl:
            dl.close()
        db.close()
    qset = set(tokens)
    por_empresa: dict = {}
    for r in rows:
        a = _acto_dict(r)
        mios = [c for c in a["cargos"]
                if qset <= set(re.sub(r"[^A-Z0-9 ]", " ", norm_texto(c["persona"])).split())]
        if not mios:
            continue
        clave = a["datos"].get("hoja") and f"{a['provincia']}|{a['datos']['hoja']}" or normalizar_denominacion(a["empresa"])["norm"]
        e = por_empresa.setdefault(clave, {"empresa": a["empresa"], "provincia": a["provincia"],
                                           "hoja_registral": a["datos"].get("hoja"), "cargos": [], "actos": []})
        e["actos"].append(a)
        for c in mios:
            e["cargos"].append({**c, "fecha": a["fecha"], "borme_id": a["borme_id"], "url_pdf": a["url_pdf"]})
    empresas = []
    for e in por_empresa.values():
        vig = inferir(e["actos"])["administradores_vigentes"]
        e["cargos_vigentes"] = [v for v in vig if qset <= set(re.sub(r"[^A-Z0-9 ]", " ", norm_texto(v["persona"])).split())]
        e["cargos"].sort(key=lambda c: c["fecha"], reverse=True)
        del e["actos"]
        empresas.append(e)
    empresas.sort(key=lambda e: e["cargos"][0]["fecha"], reverse=True)
    return {"consulta": nombre, "backend": "boe", "desde": desde.isoformat(), "hasta": hasta.isoformat(),
            "coste": {**est, "peticiones": dl.peticiones if dl else 0, "mb": round(dl.bytes / 1e6, 2) if dl else 0},
            "aviso": AVISO_VIGENTES + " Homónimos posibles: el BORME no publica DNI.", "empresas": empresas}


def buscar_empresa_apispain(nombre: str, exacta: bool = False, api_key: str | None = None, max_items: int = 20) -> dict:
    """Mismo formato de salida usando apispain.es (requiere APISPAIN_API_KEY; 1 petición)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import apispain
    q = normalizar_denominacion(nombre)
    with apispain.Apispain(api_key) as api:
        res = api.buscar_empresas(nombre=nombre, max_items=max_items)
        uso = api.last_usage
    empresas = []
    for e in res:
        if exacta and not coincide(q, normalizar_denominacion(e.get("nombre") or ""), True):
            continue
        actos = []
        for a in e.get("actos") or e.get("ultimosActos") or []:
            dp = a.get("datosParseados") or {}
            cargos = [{"accion": p.get("accion"), "cargo": p.get("cargo"),
                       "cargo_codigo": cargo_codigo(p.get("cargo") or ""), "persona": p.get("nombre")}
                      for p in a.get("personas") or []]
            # La lista «personas» de apispain omite cargos repetidos, ceses y nombres separados por «;»:
            # si el parser propio saca más del texto original, manda el parser.
            propio = parse_acto(a.get("textoOriginal") or "")
            if len(propio["cargos"]) > len(cargos):
                cargos = propio["cargos"]
            # Sin fecha de publicación, la de inscripción (I/A n (dd.mm.aa)) permite ordenar los actos
            fecha = (a.get("fechaPublicacion") or "")[:10] or propio["datos"].get("fecha_inscripcion") or ""
            actos.append({"fecha": fecha, "provincia": dp.get("provincia") or e.get("provincia"),
                          "num": None, "empresa": dp.get("empresaNombre") or e.get("nombre"), "tipo": a.get("tipoActo"),
                          "tipos": propio["tipos"] or [a.get("tipoActo")], "texto": a.get("textoOriginal"),
                          "cargos": cargos, "datos": {**propio["datos"], **dp}, "borme_id": a.get("bormeId"),
                          "url_pdf": None, "url_html": None})
        inf = inferir(actos)
        ficha = {**inf["ficha"], "nif": e.get("nif"), "denominacion": e.get("nombre"), "domicilio": e.get("domicilio")
                 or inf["ficha"]["domicilio"], "provincia": e.get("provincia"), "municipio": e.get("municipio"),
                 "codigo_postal": e.get("codigoPostal"), "capital": e.get("capitalSocial") or inf["ficha"]["capital"],
                 "fecha_constitucion": (e.get("fechaConstitucion") or "")[:10] or inf["ficha"]["fecha_constitucion"],
                 "estado": e.get("estadoRegistral") or inf["ficha"]["estado"], "fuente_apispain": e.get("fuente")}
        empresas.append({"empresa": e.get("nombre"), "ficha": ficha, "actos": actos,
                         "administradores_vigentes": inf["administradores_vigentes"]})
    return {"consulta": nombre, "normalizada": q["norm"], "backend": "apispain", "coste": {"apispain": uso},
            "aviso": AVISO_VIGENTES + " apispain sin historial pagado solo devuelve actos desde 2024.",
            "empresas": empresas}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nombre", help="denominación social (o persona con --persona)")
    ap.add_argument("--persona", action="store_true", help="buscar una persona (implica --completo)")
    ap.add_argument("--desde", help="AAAA-MM-DD o dd/mm/aaaa")
    ap.add_argument("--hasta", help="AAAA-MM-DD o dd/mm/aaaa (por defecto hoy)")
    ap.add_argument("--dias", type=int, help="días hacia atrás si no se da --desde (90; 30 con --persona)")
    ap.add_argument("--provincia", action="append", help="limitar a provincia (nombre o código INE); repetible")
    ap.add_argument("--completo", action="store_true", help="descargar todos los documentos provinciales")
    ap.add_argument("--exacta", action="store_true", help="denominación exacta (sin contar la forma si no se da)")
    ap.add_argument("--backend", choices=["auto", "boe", "apispain"], default="auto",
                    help="auto = apispain si hay APISPAIN_API_KEY, si no boe")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"caché SQLite (por defecto {DEFAULT_DB})")
    ap.add_argument("--pausa", type=float, default=0.5, help="segundos entre peticiones a boe.es")
    ap.add_argument("--max-peticiones", type=int, default=3000)
    ap.add_argument("--solo-cache", action="store_true", help="no hacer peticiones de red")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    prog = lambda m: print(m, file=sys.stderr)  # noqa: E731
    try:
        if a.persona:
            res = buscar_persona(a.nombre, a.desde, a.hasta, a.dias or 30, a.provincia, a.db, a.pausa,
                                 a.max_peticiones, a.solo_cache, prog)
        elif a.backend == "apispain" or (a.backend == "auto" and os.getenv("APISPAIN_API_KEY")):
            res = buscar_empresa_apispain(a.nombre, a.exacta)
        else:
            res = buscar_empresa(a.nombre, a.desde, a.hasta, a.dias or 90, a.provincia, a.completo, a.exacta,
                                 a.db, a.pausa, a.max_peticiones, a.solo_cache, prog)
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0 if res["empresas"] else 1
    print(f"BORME ({res['backend']}) «{a.nombre}» {res.get('desde', '')}..{res.get('hasta', '')}  coste: {res['coste']}")
    if not res["empresas"]:
        print("Sin actos encontrados en el rango.")
        return 1
    for e in res["empresas"]:
        if a.persona:
            print(f"\n== {e['empresa']}  ({e['provincia']}, hoja {e['hoja_registral']})")
            for c in e["cargos"]:
                print(f"  {c['fecha']}  {c['accion']:<12} {c['cargo']:<16} {c['persona']}  {c['url_pdf'] or ''}")
            print("  vigente (inferido): " + (", ".join(v["cargo"] for v in e["cargos_vigentes"]) or "—"))
            continue
        f = e["ficha"]
        print(f"\n== {e['empresa']}  [{f['estado']}]  {f['provincia']}  hoja {f['hoja_registral']}  NIF {f['nif'] or '¿?'}")
        for k in ("domicilio", "capital", "fecha_constitucion", "socio_unico", "pagina_web", "objeto_social"):
            if f.get(k):
                print(f"   {k}: {str(f[k])[:160]}")
        print("   administradores vigentes (inferencia parcial):")
        for v in e["administradores_vigentes"]:
            print(f"     - {v['cargo']:<16} {v['persona']}  (desde {v['desde']})")
        for ac in e["actos"]:
            print(f"  {ac['fecha']}  {', '.join(ac['tipos'])}  {ac['url_pdf'] or ''}")
            for c in ac["cargos"]:
                print(f"      {c['accion']:<12} {c['cargo']:<16} {c['persona']}")
    print(f"\n{res['aviso']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
