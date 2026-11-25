from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import (
    Hallazgo,
    OrgEsEnricher,
    documento,
    eur,
    recorta,
)
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.organization import Organization

SI_NO = [{"label": "Sí", "value": "true"}, {"label": "No", "value": "false"}]


@flowsint_enricher
class OrgToContratosPlacsp(OrgEsEnricher):
    """[PLACSP] Licitaciones de la Plataforma de Contratación del Sector Público adjudicadas a la empresa."""

    FUENTE = "[PLACSP]"
    MAX_DEFECTO = 20
    PARAMS = [
        {
            "name": "incluir_organos",
            "type": "select",
            "default": "true",
            "required": False,
            "description": "Crear el órgano de contratación como Organization.",
            "options": SI_NO,
        }
    ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_contratos_placsp"

    @classmethod
    def documentation(cls) -> str:
        return """
        Usa el campo «Adjudicatario» del buscador de licitaciones de contrataciondelestado.es
        con la denominación sin forma jurídica (el buscador no admite NIF) y crea un Document por
        licitación (expediente, objeto, estado, importe, enlace permanente) con la relación
        ADJUDICATARIA y, opcionalmente, el órgano de contratación (LICITADO_POR).
        La búsqueda es por palabras del nombre: con denominaciones genéricas revisa los
        resultados. Solo perfiles alojados en la Plataforma. Script osint-es-scripts/placsp.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        base = self.base_o_fallo(ids)  # el buscador no admite NIF
        if not base:
            return []
        placsp = load("placsp")
        with httpx.Client(
            headers=placsp.HEADERS, timeout=60, follow_redirects=True
        ) as c:
            r = placsp.buscar(c, base, self.max_resultados)
        for error in r.get("errores") or []:
            self.fallo(base, error)
        self.log(f"«{base}»: {r['total']} licitaciones en total")
        out = []
        for i in r["resultados"]:
            imp = eur(i["importe"])
            doc = documento(
                f"PLACSP {i['expediente']} · {recorta(i['objeto'], 80)}",
                doc_type="contrato público",
                source="PLACSP",
                source_url=i["url"],
                created_date=i.get("fecha_presentacion"),
                author=i["organo"],
                subject=i["objeto"],
                description=f"{i['tipo']} · {i['estado']} · {imp}",
                importe=i["importe"],
                estado=i["estado"],
                expediente=i["expediente"],
                consulta_adjudicatario=base,
            )
            extra = []
            if self.p_bool("incluir_organos", True) and i.get("organo"):
                extra.append(
                    (
                        doc,
                        Organization(
                            name=i["organo"], perfil_contratante=i.get("url_organo")
                        ),
                        "LICITADO_POR",
                    )
                )
            out.append(Hallazgo(doc, "ADJUDICATARIA", extra, certainty="posible"))
        return out


InputType = OrgToContratosPlacsp.InputType
OutputType = OrgToContratosPlacsp.OutputType
