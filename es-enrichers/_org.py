"""Base común de los enrichers Organization → (Document | Organization) que envuelven
los scripts de osint-es-scripts/ (BDNS, CNMV, BOE, CENDOJ, PLACSP, TED, sanciones UE).

No es un enricher (el cargador ignora los ficheros que empiezan por "_"). Cada subclase
implementa `consultar(ids)` (síncrono, se ejecuta en un hilo) y devuelve una lista de
`Hallazgo`; la base se encarga del bucle, los límites, los errores y del grafo.
"""
import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_types.document import Document
from flowsint_types.organization import Organization

from flowsint_enrichers.es._scripts import load

MAX_DURO = 100  # tope absoluto de nodos por organización, se pida lo que se pida


@dataclass
class Hallazgo:
    """Un nodo principal enlazado a la organización de entrada, y relaciones secundarias opcionales."""
    nodo: Any
    relacion: str
    extra: List[tuple] = field(default_factory=list)  # (desde, hacia, etiqueta)


def org_ids(org: Organization) -> Dict[str, Optional[str]]:
    """NIF, LEI y denominación de un nodo Organization (los campos nif/lei son propiedades extra)."""
    res = load("resolver_empresa")
    extra = org.model_extra or {}
    nif = None
    for k in ("nif", "cif", "tax_id", "vat", "siren"):
        v = getattr(org, k, None) or extra.get(k)
        if v and res.parece_nif(str(v)):
            nif = res.limpiar_nif(str(v))
            break
    lei = str(extra.get("lei") or "").strip().upper() or None
    if lei and not re.fullmatch(r"[A-Z0-9]{18}\d{2}", lei):
        lei = None
    nombre = str(org.nom_raison_sociale or org.name or "").strip()
    nombre = re.sub(r"\s*[\[(][A-Z0-9]{9,11}[\])]\s*$", "", nombre)  # "EMPRESA SL (B12345678)" -> "EMPRESA SL"
    if not nif and res.parece_nif(nombre):
        nif, nombre = res.limpiar_nif(nombre), ""
    return {"nif": nif, "lei": lei, "nombre": nombre,
            "base": res.normalizar(nombre)["base"] if nombre else ""}


def nombre_por_nif(nif: str) -> str:
    """Denominación base para un NIF (vía BDNS), para las fuentes que solo buscan por texto."""
    import httpx
    res = load("resolver_empresa")
    with httpx.Client(headers=res.HEADERS, timeout=30) as c:
        cands = [x for x in res.desde_bdns(c, nif) if x["nif"] == nif]
    return res.normalizar(cands[0]["nombre"])["base"] if cands else ""


def eur(x) -> str:
    """1234567.8 -> '1.234.567,80 €'."""
    if not isinstance(x, (int, float)):
        return "importe n/d"
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " €"


def documento(titulo: str, **kw) -> Document:
    """Document con título único (el título es la clave del nodo en el grafo)."""
    kw = {k: v for k, v in kw.items() if v not in (None, "", [])}
    return Document(title=re.sub(r"\s+", " ", titulo).strip()[:240], **kw)


def recorta(s: Optional[str], n: int = 80) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class OrgEsEnricher(Enricher):
    InputType = Organization
    OutputType = Document

    FUENTE = "[ES]"
    MAX_DEFECTO = 25
    PARAMS: List[Dict[str, Any]] = []

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None, vault=None,
                 params: Optional[Dict[str, Any]] = None, graph_service=None) -> None:
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.hallazgos: List[tuple] = []

    @classmethod
    def category(cls) -> str:
        return "Organization"

    @classmethod
    def key(cls) -> str:
        return "name"

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        # build_params_model trata todos los params no-vault como str: defaults en texto.
        return [{"name": "max_resultados", "type": "string", "required": False,
                 "default": str(cls.MAX_DEFECTO),
                 "description": f"Máximo de nodos por organización (tope {MAX_DURO})."}] + cls.PARAMS

    # -- parámetros --------------------------------------------------------------------
    def p_int(self, nombre: str, defecto: int) -> int:
        try:
            v = int(str(self.params.get(nombre) or defecto).strip())
        except ValueError:
            v = defecto
        return max(1, min(v, MAX_DURO))

    def p_bool(self, nombre: str, defecto: bool) -> bool:
        v = self.params.get(nombre)
        return defecto if v in (None, "") else str(v).strip().lower() in ("true", "1", "si", "sí", "yes")

    @property
    def max_resultados(self) -> int:
        return self.p_int("max_resultados", self.MAX_DEFECTO)

    # -- a implementar -----------------------------------------------------------------
    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        raise NotImplementedError

    # -- ejecución ---------------------------------------------------------------------
    def log(self, msg: str, error: bool = False) -> None:
        (Logger.error if error else Logger.info)(self.sketch_id, {"message": f"{self.FUENTE} {msg}"})

    async def scan(self, data: List[Organization]) -> List[Any]:
        self.hallazgos = []
        results: List[Any] = []
        for org in data:
            ids = org_ids(org)
            if not ids["nif"] and len(ids["base"]) < 3:
                self.log(f"{org.name}: sin NIF ni denominación utilizable", error=True)
                continue
            try:
                items = await asyncio.to_thread(self.consultar, ids)
            except Exception as e:  # la fuente puede caer o cambiar: no tumbar el resto
                self.log(f"Error consultando {ids['nif'] or ids['nombre']}: {type(e).__name__}: {e}", error=True)
                continue
            items = items[: self.max_resultados]
            self.log(f"{ids['nif'] or ids['nombre']}: {len(items)} resultados")
            for h in items:
                self.hallazgos.append((org, h))
                results.append(h.nodo)
        return results

    def postprocess(self, results: List[Any], input_data: List[Organization] = None) -> List[Any]:
        if not self._graph_service:
            return results
        for org, h in self.hallazgos:
            self.create_node(org)
            self.create_node(h.nodo)
            self.create_relationship(org, h.nodo, h.relacion)
            for desde, hacia, etiqueta in h.extra:
                self.create_node(desde)
                self.create_node(hacia)
                self.create_relationship(desde, hacia, etiqueta)
            self.log_graph_message(f"{org.nodeLabel}: {h.relacion} -> {h.nodo.nodeLabel}")
        return results
