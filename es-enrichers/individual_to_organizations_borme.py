"""Individual -> Organization: sociedades donde la persona figura con cargo en el BORME
(osint-es-scripts/borme.py, búsqueda por persona en los documentos provinciales).

Caro: necesita el texto completo de todas las provincias del rango (~30 peticiones por
día hábil; 30 días ≈ 650 peticiones ≈ 6 min la primera vez, luego caché). Usa PROVINCIA
para abaratarlo. El BORME no publica DNI: un nombre puede corresponder a homónimos.
"""
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es import _borme_comun as bc
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization


@flowsint_enricher
class EsIndividualToOrgsBormeEnricher(Enricher):
    """[BORME] Companies where a person holds or held a registered position (BORME)."""

    InputType = Individual
    OutputType = Organization

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {"name": "DIAS", "type": "string", "default": "30", "required": False,
             "description": "Días hacia atrás (~30 peticiones por día hábil la primera vez)."},
            {"name": "DESDE", "type": "string", "required": False, "description": "Fecha inicial AAAA-MM-DD."},
            {"name": "HASTA", "type": "string", "required": False, "description": "Fecha final AAAA-MM-DD."},
            {"name": "PROVINCIA", "type": "string", "required": False,
             "description": "Provincias separadas por comas (nombre o código INE) para abaratar."},
            {"name": "MAX_PETICIONES", "type": "string", "default": "1500", "required": False,
             "description": "Tope de peticiones a boe.es por ejecución."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_individual_to_companies_borme"

    @classmethod
    def category(cls) -> str:
        return "Individual"

    @classmethod
    def key(cls) -> str:
        return "full_name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca el nombre (en cualquier orden; el BORME usa APELLIDOS NOMBRE) en los actos
        inscritos del BORME del rango y crea Organization por cada sociedad donde aparece,
        con la relación Individual -[CARGO]-> Organization (EX_<CARGO> si consta cese).
        Requiere todos los documentos provinciales del rango (se cachean en SQLite).
        Cuidado con homónimos: el BORME no publica DNI.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.mapping, out = [], []
        for ind in data:
            nombre = (ind.full_name or " ".join(x for x in (ind.first_name, ind.last_name) if x) or "").strip()
            if not nombre:
                continue
            try:
                res = await bc.buscar_persona(nombre, self.params)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[BORME] {nombre}: {exc}"})
                continue
            Logger.info(self.sketch_id, {"message": f"[BORME] {nombre}: {len(res['empresas'])} sociedades, "
                                                    f"coste {res['coste']}"})
            for emp in res["empresas"]:
                org = Organization(name=emp["empresa"], source="BORME", siege_departement=emp["provincia"],
                                   hoja_registral=emp["hoja_registral"], registro_mercantil=emp["provincia"])
                out.append(org)
                vig = {v["cargo_codigo"] for v in emp["cargos_vigentes"]}
                vistos = set()
                for c in emp["cargos"]:
                    etiqueta = c["cargo_codigo"] if c["cargo_codigo"] in vig else f"EX_{c['cargo_codigo']}"
                    if etiqueta in vistos:
                        continue
                    vistos.add(etiqueta)
                    self.mapping.append((ind, org, etiqueta, f"{c['fecha']} {c['accion']} {c['cargo']} "
                                                             f"({c['persona']}) {c.get('url_pdf') or ''}"))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        if not self._graph_service:
            return results
        for ind, org, etiqueta, resumen in self.mapping:
            self.create_node(ind)
            self.create_node(org)
            self.create_relationship(ind, org, etiqueta)
            self.log_graph_message(f"{ind.full_name} -[{etiqueta}]-> {org.name}: {resumen}")
        return results


InputType = EsIndividualToOrgsBormeEnricher.InputType
OutputType = EsIndividualToOrgsBormeEnricher.OutputType
