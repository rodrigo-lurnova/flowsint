from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher


@flowsint_enricher
class OrgToRegistrosCnmv(OrgEsEnricher):
    """[CNMV] Secciones de los Registros Oficiales de la CNMV con información de la entidad."""

    FUENTE = "[CNMV]"

    @classmethod
    def name(cls) -> str:
        return "es_org_to_registros_cnmv"

    @classmethod
    def documentation(cls) -> str:
        return """
        Abre la ficha de la entidad en la CNMV (DatosEntidad?nif=A-48010615) y crea un Document
        por sección del registro con información (folletos, hechos relevantes, participaciones
        significativas, cuentas anuales...), con la relación REGISTRO_CNMV. Sin NIF, busca por
        denominación y solo usa entidades cuya denominación coincide. Solo cubre emisores,
        cotizadas y entidades supervisadas. Script osint-es-scripts/cnmv.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        cnmv = load("cnmv")
        res = load("resolver_empresa")
        out: List[Hallazgo] = []
        with httpx.Client(headers=cnmv.HEADERS, timeout=40, follow_redirects=True) as c:
            if ids["nif"]:
                nifs = [f"{ids['nif'][0]}-{ids['nif'][1:]}"]  # la ficha solo responde con guion
            else:
                nifs = [e["nif"] for e in cnmv.buscar(c, ids["nombre"])
                        if res.coincidencia(ids["nombre"], e["nombre"]) != "parcial"][:3]
            for nif in nifs:
                f = cnmv.ficha(c, nif)
                for s in f["secciones"]:
                    doc = documento(f"CNMV {nif} · {s['seccion']}", doc_type="registro CNMV", source="CNMV",
                                    source_url=s["url"], author="Comisión Nacional del Mercado de Valores",
                                    description=f"{f['nombre']} — {s['seccion']}", ficha_url=f["url"])
                    out.append(Hallazgo(doc, "REGISTRO_CNMV"))
        return out


InputType = OrgToRegistrosCnmv.InputType
OutputType = OrgToRegistrosCnmv.OutputType
