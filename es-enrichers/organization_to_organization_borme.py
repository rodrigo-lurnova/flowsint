"""Organization -> Organization: ficha registral desde el BORME (osint-es-scripts/borme.py).

Actualiza el propio nodo (mismo nodeLabel) con domicilio, objeto social, capital,
fecha de constitución, estado inferido, forma jurídica, hoja registral, socio único,
web y NIF (este último solo con apispain). Crea Website si el BORME publica la web
corporativa. Coincidencias no exactas se crean como Organization aparte.
"""
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es import _borme_comun as bc
from flowsint_types.organization import Organization
from flowsint_types.website import Website


@flowsint_enricher
class EsOrgToOrgBormeEnricher(Enricher):
    """[BORME] Company registry record (address, purpose, capital, status) from BORME."""

    InputType = Organization
    OutputType = Organization

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return bc.PARAMS_COMUNES + [
            {"name": "COMPLETO", "type": "string", "default": "false", "required": False,
             "description": "true = descargar todos los documentos provinciales (lento; llena la caché)."},
            {"name": "MAX_PETICIONES", "type": "string", "default": "3000", "required": False,
             "description": "Tope de peticiones a boe.es por ejecución."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_registry_borme"

    @classmethod
    def category(cls) -> str:
        return "Organization"

    @classmethod
    def key(cls) -> str:
        return "name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Ficha mercantil a partir de los actos del BORME Sección Primera en el rango
        (DIAS, por defecto 90): domicilio (último cambio o constitución), objeto social,
        capital (constitución o 'Resultante Suscrito' de ampliaciones/reducciones),
        fecha de constitución, estado inferido (activa / disuelta / extinguida / concurso),
        forma jurídica, hoja registral, socio único y web corporativa. El NIF no se publica
        en el BORME: solo se rellena con el backend apispain (APISPAIN_API_KEY).
        """

    def _ficha(self, org: Organization, emp: dict) -> Organization:
        f = emp["ficha"]
        return bc.org_actualizada(
            org, denominacion_borme=f.get("denominacion"), nature_juridique=f.get("forma_juridica"),
            siege_adresse=f.get("domicilio"), siege_libelle_commune=f.get("municipio") or bc.municipio_de(f.get("domicilio")),
            siege_departement=f.get("provincia"), siege_code_postal=f.get("codigo_postal"),
            date_creation=f.get("fecha_constitucion"), etat_administratif=f.get("estado"),
            objeto_social=f.get("objeto_social"), capital_social=f.get("capital"), nif=f.get("nif"),
            hoja_registral=f.get("hoja_registral"), registro_mercantil=f.get("provincia"),
            socio_unico=f.get("socio_unico"), pagina_web=f.get("pagina_web"),
            comienzo_operaciones=f.get("comienzo_operaciones"), borme_ultimo_acto=f.get("ultimo_acto"),
            borme_denominaciones=f.get("denominaciones"),
            borme_pdfs=list(dict.fromkeys(a["url_pdf"] for a in emp["actos"] if a.get("url_pdf")))[:20],
            source="BORME")

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.mapping, out = [], []
        api_key = bc.api_key_de(self)
        mod = bc.borme()
        for org in data:
            try:
                res = await bc.buscar_empresa(str(org.name), self.params, api_key)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[BORME] {org.name}: {exc}"})
                continue
            q = mod.normalizar_denominacion(str(org.name))
            exactas = [e for e in res["empresas"] if mod.normalizar_denominacion(e["empresa"] or "")["base"] == q["base"]]
            if not res["empresas"]:
                Logger.info(self.sketch_id, {"message": f"[BORME] {org.name}: sin actos en el rango ({res['coste']})"})
            for i, emp in enumerate(res["empresas"]):
                principal = bool(exactas) and emp is exactas[0]
                nodo = self._ficha(org, emp) if principal else self._ficha(Organization(name=emp["empresa"]), emp)
                out.append(nodo)
                web = None
                if nodo.model_extra and nodo.model_extra.get("pagina_web"):
                    url = nodo.model_extra["pagina_web"]
                    try:
                        web = Website(url=url if url.startswith("http") else f"https://{url}")
                    except Exception:
                        web = None
                self.mapping.append((org, nodo, principal, web))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        if not self._graph_service:
            return results
        for org, nodo, principal, web in self.mapping:
            self.create_node(nodo)
            if not principal:
                self.create_node(org)
                self.create_relationship(nodo, org, "BORME_COINCIDENCIA_PARCIAL")
            ex = nodo.model_extra or {}
            self.log_graph_message(f"{nodo.name}: {ex.get('denominacion_borme')} [{nodo.etat_administratif}] "
                                   f"{nodo.siege_adresse or ''} capital {ex.get('capital_social')}")
            if web:
                self.create_node(web)
                self.create_relationship(nodo, web, "HAS_WEBSITE")
        return results


InputType = EsOrgToOrgBormeEnricher.InputType
OutputType = EsOrgToOrgBormeEnricher.OutputType
