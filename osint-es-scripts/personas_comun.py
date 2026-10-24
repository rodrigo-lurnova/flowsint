"""Utilidades compartidas por correos.py, perfiles.py y empleados_web.py.

No es un CLI: solo funciones puras (sin efectos al importar) y un crawl ligero
de la web de una empresa. Dependencias: httpx + stdlib.

Contenido:
  - normalizar(), partir_nombre(): nombres españoles (tildes, ñ, dos apellidos,
    partículas «de/del/la», formato BORME «APELLIDO1 APELLIDO2, NOMBRE»).
  - crawl_web(): portada + hasta N páginas internas con palabras clave
    (contacto, aviso legal, privacidad, equipo, nosotros...). Mismo host, sin
    seguir enlaces externos, con pausa entre peticiones.
  - bloques_texto(): HTML -> lista de bloques de texto (por elementos de bloque),
    útil para emparejar «Nombre Apellido» con el cargo del bloque contiguo.
  - extraer_emails(): regex + mailto + data-cfemail (ofuscación de Cloudflare).
"""
import html as _html
import re
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 (osint-es-scripts)"
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

PARTICULAS = {"de", "del", "la", "las", "los", "y", "i", "e", "da", "dos", "san", "santa", "van", "von", "di"}
# Nombres compuestos frecuentes cuyo primer token suele ir acompañado
NOMBRES_COMPUESTOS_1 = {"jose", "maria", "juan", "ana", "luis", "francisco", "miguel", "jesus", "pedro",
                        "antonio", "manuel", "carlos", "javier", "angel", "rosa", "pilar", "mari", "fco"}

PALABRAS_WEB = ("contact", "aviso", "legal", "privac", "equipo", "team", "nosotros", "about", "quienes",
                "quiénes", "sobre", "people", "staff", "direccion", "dirección", "management", "leadership",
                "consejo", "board", "imprint", "impressum", "cookies", "socios", "partners", "fundador",
                "founder", "empresa", "company")
PALABRAS_EQUIPO = ("equipo", "team", "nosotros", "about", "quienes", "quiénes", "sobre", "people", "staff",
                   "direccion", "dirección", "management", "leadership", "consejo", "board", "socios",
                   "fundador", "founder", "empresa", "company", "who-we-are", "conocenos", "conócenos")

EMAIL_RE = re.compile(r"(?<![\w.+-])([a-zA-Z0-9][a-zA-Z0-9._%+-]{0,63}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24})(?![\w-])")
GENERICOS = {"info", "contacto", "contact", "hola", "hello", "hi", "admin", "administracion", "administration",
             "rrhh", "hr", "jobs", "empleo", "careers", "trabajo", "ventas", "sales", "comercial", "soporte",
             "support", "help", "ayuda", "prensa", "press", "media", "marketing", "legal", "privacy",
             "privacidad", "dpo", "dpd", "lopd", "rgpd", "gdpr", "protecciondedatos", "datos", "noreply", "no-reply",
             "donotreply", "webmaster", "postmaster", "abuse", "office", "oficina", "recepcion", "reception",
             "facturacion", "billing", "factura", "facturas", "accounts", "contabilidad", "compras", "pedidos",
             "orders", "clientes", "atencionalcliente", "customer", "customerservice", "booking", "reservas",
             "team", "equipo", "general", "mail", "email", "correo", "secretaria", "direccion", "gerencia",
             "partners", "socios", "proveedores", "seguridad", "security", "it", "sistemas", "tech", "dev",
             "hello", "talento", "talent", "recruiting", "seleccion", "cv", "inversores", "investors", "ir"}


def normalizar(s: str) -> str:
    """'Martínez Ñúñez' -> 'martinez nunez' (minúsculas, sin tildes; ñ -> n; ç -> c)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s-]", " ", s.lower())).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalizar(s))


def partir_nombre(nombre: str, apellidos_primero: bool | None = None) -> list[dict]:
    """Devuelve interpretaciones posibles {nombre, apellido1, apellido2} (ya normalizadas).

    - «MARTINEZ GARCIA, RODRIGO» (formato BORME) -> apellidos primero.
    - 2 tokens: nombre + apellido.  3 tokens: nombre + ap1 + ap2 (y alternativa nombre
      compuesto + ap1 si el primero es típico de compuestos, p. ej. «José Luis Pérez»).
    - 4+ tokens: dos nombres + dos apellidos. Las partículas se pegan al apellido
      («de la Fuente» -> «delafuente» para el correo).
    """
    if "," in nombre:
        ap, nom = nombre.split(",", 1)
        nombre, apellidos_primero = f"{nom} {ap}", False
    toks = normalizar(nombre).split()
    # pegar partículas al token siguiente
    grupos, buf = [], []
    for t in toks:
        buf.append(t)
        if t not in PARTICULAS:
            grupos.append(buf)
            buf = []
    if buf and grupos:
        grupos[-1].extend(buf)
    pal = ["".join(g) for g in grupos]
    pal = [p.replace("-", "") for p in pal if p]
    if not pal:
        return []
    if apellidos_primero and len(pal) >= 2:
        pal = pal[-1:] + pal[:-1] if len(pal) <= 3 else pal[-2:] + pal[:-2]
    out = []
    if len(pal) == 1:
        out.append({"nombre": pal[0], "apellido1": "", "apellido2": ""})
    elif len(pal) == 2:
        out.append({"nombre": pal[0], "apellido1": pal[1], "apellido2": ""})
    elif len(pal) == 3:
        out.append({"nombre": pal[0], "apellido1": pal[1], "apellido2": pal[2]})
        if pal[0] in NOMBRES_COMPUESTOS_1:
            out.append({"nombre": pal[0], "nombre2": pal[1], "apellido1": pal[2], "apellido2": ""})
    else:
        out.append({"nombre": pal[0], "nombre2": pal[1], "apellido1": pal[2], "apellido2": pal[3]})
    return out


def tokens_nombre(nombre: str) -> list[str]:
    """Tokens significativos (sin partículas) para comparar nombres en texto libre."""
    return [t for t in normalizar(nombre.replace(",", " ")).split() if t not in PARTICULAS and len(t) > 1]


def coincidencia_nombre(nombre: str, texto: str) -> float:
    """Fracción de tokens del nombre presentes en el texto (0..1), con bonus si nombre+apellido1 seguidos."""
    toks = tokens_nombre(nombre)
    if not toks:
        return 0.0
    t = " " + normalizar(texto) + " "
    tc = " " + t.replace("-", " ") + " "
    hits = sum(1 for k in toks if f" {k} " in tc or f"-{k}" in t)
    frac = hits / len(toks)
    if len(toks) >= 2 and f" {toks[0]} {toks[1]} " in tc:
        frac = min(1.0, frac + 0.15)
    return round(frac, 3)


# --------------------------------------------------------------------------- HTML

_BLOQUE = {"p", "div", "li", "td", "th", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
           "header", "footer", "br", "figcaption", "figure", "dt", "dd", "blockquote", "address",
           "ul", "ol", "table", "main", "aside", "nav", "form", "label", "button"}


class _Bloques(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bloques: list[dict] = []
        self.buf: list[str] = []
        self.links: list[str] = []
        self.skip = 0
        self.tag_actual = None
        self.cf: list[str] = []
        self.jsonld: list[str] = []
        self._in_jsonld = False

    def _flush(self):
        txt = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if txt:
            self.bloques.append({"texto": txt, "tag": self.tag_actual, "links": self.links})
        self.buf, self.links = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip += 1
            if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
                self._in_jsonld = True
            return
        if a.get("data-cfemail"):
            self.cf.append(a["data-cfemail"])
        if tag in _BLOQUE:
            self._flush()
            self.tag_actual = tag
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip = max(0, self.skip - 1)
            self._in_jsonld = False
            return
        if tag in _BLOQUE:
            self._flush()

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parsear_html(html_txt: str) -> dict:
    """-> {"bloques": [{texto, tag, links}], "cfemail": [...], "jsonld": [...], "links": [...]}"""
    p = _Bloques()
    try:
        p.feed(html_txt)
        p.close()
    except Exception:
        pass
    links = [h for b in p.bloques for h in b["links"]]
    links += re.findall(r'href=["\']([^"\']+)["\']', html_txt)
    return {"bloques": p.bloques, "cfemail": p.cf, "jsonld": p.jsonld, "links": list(dict.fromkeys(links))}


def bloques_texto(html_txt: str) -> list[dict]:
    return parsear_html(html_txt)["bloques"]


def _decode_cf(hexs: str) -> str:
    try:
        k = int(hexs[:2], 16)
        return "".join(chr(int(hexs[i:i + 2], 16) ^ k) for i in range(2, len(hexs), 2))
    except ValueError:
        return ""


def extraer_emails(html_txt: str, parsed: dict | None = None) -> list[str]:
    parsed = parsed or parsear_html(html_txt)
    txt = _html.unescape(html_txt)
    txt = re.sub(r"\s*(\[at\]|\(at\)|\{at\}| arroba )\s*", "@", txt, flags=re.I)
    txt = re.sub(r"\s*(\[dot\]|\(dot\)|\{dot\}| punto )\s*", ".", txt, flags=re.I)
    found = [m.group(1) for m in EMAIL_RE.finditer(txt)]
    found += [_decode_cf(h) for h in parsed["cfemail"]]
    found += [_html.unescape(l[7:].split("?")[0]) for l in parsed["links"] if l.lower().startswith("mailto:")]
    out = []
    for e in found:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", e) \
                and "example." not in e and "sentry" not in e and "wixpress" not in e:
            out.append(e)
    return list(dict.fromkeys(out))


def contexto_email(bloques: list[dict], email: str, ventana: int = 2) -> str:
    """Texto de los bloques alrededor de donde aparece el email (para adivinar a quién pertenece)."""
    for i, b in enumerate(bloques):
        if email in b["texto"].lower() or any(email in (l or "").lower() for l in b["links"]):
            return " | ".join(x["texto"] for x in bloques[max(0, i - ventana): i + ventana + 1])
    return ""


# --------------------------------------------------------------------------- crawl

def url_base(dominio_o_url: str) -> str:
    s = dominio_o_url.strip()
    if not re.match(r"^https?://", s):
        s = "https://" + s
    u = urlparse(s)
    return f"{u.scheme}://{u.netloc}{u.path or '/'}"


def host_raiz(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def crawl_web(dominio_o_url: str, max_paginas: int = 12, palabras=PALABRAS_WEB, pausa: float = 0.4,
              client: httpx.Client | None = None, timeout: float = 15) -> list[dict]:
    """Descarga la portada y hasta `max_paginas` internas cuyo enlace contenga alguna palabra clave.

    Devuelve [{url, status, html}] (solo text/html). No sale del host raíz.
    """
    propio = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
    paginas: list[dict] = []
    try:
        inicio = url_base(dominio_o_url)
        try:
            r = client.get(inicio)
        except httpx.HTTPError:
            if inicio.startswith("https://"):
                inicio = "http://" + inicio[8:]
                r = client.get(inicio)
            else:
                raise
        raiz = host_raiz(urlparse(str(r.url)).netloc)
        if "html" in r.headers.get("content-type", ""):
            paginas.append({"url": str(r.url), "status": r.status_code, "html": r.text})
        else:
            return paginas
        cola, vistos = [], {str(r.url).rstrip("/")}
        def _encolar(html_txt, base):
            for h in parsear_html(html_txt)["links"]:
                if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                u = urljoin(base, h).split("#")[0]
                pu = urlparse(u)
                if host_raiz(pu.netloc) != raiz or re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?|svg|webp|mp4)$", pu.path, re.I):
                    continue
                low = (pu.path + "?" + pu.query).lower()
                if any(p in low for p in palabras) and u.rstrip("/") not in vistos:
                    vistos.add(u.rstrip("/"))
                    cola.append(u)
        _encolar(r.text, str(r.url))
        # rutas típicas aunque no estén enlazadas en la portada
        for ruta in ("/contacto", "/aviso-legal", "/equipo", "/nosotros", "/quienes-somos", "/about", "/team"):
            u = urljoin(str(r.url), ruta)
            if any(p in ruta for p in palabras) and u.rstrip("/") not in vistos:
                vistos.add(u.rstrip("/"))
                cola.append(u)
        while cola and len(paginas) < max_paginas + 1:
            u = cola.pop(0)
            time.sleep(pausa)
            try:
                rr = client.get(u)
            except httpx.HTTPError:
                continue
            if rr.status_code != 200 or "html" not in rr.headers.get("content-type", ""):
                continue
            if host_raiz(urlparse(str(rr.url)).netloc) != raiz:
                continue
            paginas.append({"url": str(rr.url), "status": rr.status_code, "html": rr.text})
            if len(paginas) < 4:  # segundo nivel solo desde las primeras páginas
                _encolar(rr.text, str(rr.url))
    finally:
        if propio:
            client.close()
    # dedupe por URL final
    out, seen = [], set()
    for p in paginas:
        if p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    return out
