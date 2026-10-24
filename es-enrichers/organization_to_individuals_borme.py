"""Organization -> Individual: administradores, consejeros, apoderados, liquidadores y socio
único según los actos inscritos del BORME (osint-es-scripts/borme.py).

Relación Individual -[CARGO]-> Organization con el código del cargo como etiqueta
(ADM_UNICO, ADM_SOLIDARIO, CONSEJERO, CONSEJERO_DELEGADO, APODERADO, LIQUIDADOR,
SOCIO_UNICO...). Los cargos cesados/revocados en el rango llevan prefijo EX_.
Las personas jurídicas que ocupan cargos se crean como Organization.
"""
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es import _borme_comun as bc
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization


@flowsint_enricher
class EsOrgToIndividualsBormeEnricher(Enricher):
    """[BORME] Company officers (administrators, directors, attorneys, sole shareholder) from BORME."""

    InputType = Organization
    OutputType = Individual

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return bc.PARAMS_COMUNES + [
            {"name": "SOLO_VIGENTES", "type": "string", "default": "false", "required": False,
             "description": "true = solo cargos sin cese posterior observado; false = también EX_ cargos."},
            {"name": "COMPLETO", "type": "string", "default": "false", "required": False,
             "description": "true = descargar todos los documentos provinciales (lento; llena la caché)."},
            {"name": "MAX_PETICIONES", "type": "string", "default": "3000", "required": False,
             "description": "Tope de peticiones a boe.es por ejecución."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_officers_borme"

    @classmethod
    def category(cls) -> str:
        return "Organization"

    @classmethod
    def key(cls) -> str:
        return "name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca la denominación en la Sección Primera del BORME (actos inscritos) usando la
        API de datos abiertos del BOE (sumario diario + índice alfabético + texto de cada
        provincia; caché SQLite en ~/.cache/osint-es/borme.sqlite) o apispain.es si hay
        APISPAIN_API_KEY. Crea un Individual por persona con cargo y la relación
        Individual -[ADM_UNICO|CONSEJERO|APODERADO|...]-> Organization (EX_<CARGO> si
        consta cese/revocación). Los cargos 'vigentes' son una inferencia parcial sobre el
        rango consultado (por defecto 90 días). El BORME no publica DNI: posibles homónimos.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.mapping, out = [], []
        solo_vig = bc.p_bool(self.params.get("SOLO_VIGENTES"))
        api_key = bc.api_key_de(self)
        mod = bc.borme()
        for org in data:
            try:
                res = await bc.buscar_empresa(str(org.name), self.params, api_key)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[BORME] {org.name}: {exc}"})
                continue
            q = mod.normalizar_denominacion(str(org.name))
            Logger.info(self.sketch_id, {"message": f"[BORME] {org.name}: {len(res['empresas'])} sociedades, "
                                                    f"coste {res['coste']}"})
            for emp in res["empresas"]:
                exacta = mod.normalizar_denominacion(emp["empresa"] or "")["base"] == q["base"]
                destino = org if exacta else Organization(name=emp["empresa"], source="BORME")
                if not exacta:
                    self.mapping.append(("org", org, destino, "BORME_COINCIDENCIA_PARCIAL", None))
                for h in bc.historial_cargos(emp["actos"]):
                    if solo_vig and not h["vigente"]:
                        continue
                    etiqueta = h["cargo_codigo"] if h["vigente"] else f"EX_{h['cargo_codigo']}"
                    resumen = (f"{h['cargo']} en {emp['empresa']} desde {h['desde'] or '¿?'}"
                               + (f" hasta {h['hasta']}" if h["hasta"] else " (vigente, inferido)")
                               + f" [BORME {', '.join(h['borme_ids'][:3])}]")
                    if bc.es_persona_juridica(h["persona"]):
                        nodo = Organization(name=h["persona"], source="BORME", borme_cargos=[resumen])
                    else:
                        nodo = bc.individuo(h["persona"], job_title=h["cargo"], employer=emp["empresa"],
                                            first_seen=h["desde"], last_seen=h["hasta"] or h["desde"],
                                            notes=resumen, borme_cargos=[resumen],
                                            borme_cargo_codigo=h["cargo_codigo"], borme_vigente=h["vigente"])
                        out.append(nodo)
                    self.mapping.append(("cargo", nodo, destino, etiqueta, resumen))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        if not self._graph_service:
            return results
        for kind, a, b, etiqueta, resumen in self.mapping:
            self.create_node(a)
            self.create_node(b)
            if kind == "org":
                self.create_relationship(a, b, etiqueta)
                self.log_graph_message(f"{a.name} ~ {b.name} ({etiqueta})")
            else:
                self.create_relationship(a, b, etiqueta)
                self.log_graph_message(f"{getattr(a, 'full_name', None) or a.name} -[{etiqueta}]-> {b.name}")
        return results


InputType = EsOrgToIndividualsBormeEnricher.InputType
OutputType = EsOrgToIndividualsBormeEnricher.OutputType
