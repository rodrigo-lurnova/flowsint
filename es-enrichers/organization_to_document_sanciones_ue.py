from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher


@flowsint_enricher
class OrgToSancionesUe(OrgEsEnricher):
    """[SANCIONES UE] Coincidencias en la lista consolidada de sanciones financieras de la UE."""

    FUENTE = "[SANCIONES UE]"
    MAX_DEFECTO = 10
    PARAMS = [{"name": "incluir_parciales", "type": "select", "default": "false", "required": False,
               "description": "Incluir coincidencias parciales (todas las palabras del nombre) como POSIBLE_COINCIDENCIA_SANCIONES.",
               "options": [{"label": "No", "value": "false"}, {"label": "Sí", "value": "true"}]}]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_sanciones_ue"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca la organización en la lista consolidada de personas y entidades sujetas a
        sanciones financieras de la UE (Comisión Europea, Financial Sanctions Files; descarga
        pública diaria en caché local). Por NIF/identificador o por denominación normalizada.
        Coincidencia por identificador o denominación exacta -> SANCIONADA; coincidencia parcial
        (si se activa) -> POSIBLE_COINCIDENCIA_SANCIONES. Una coincidencia de nombre no prueba
        identidad: revisa país, dirección e identificadores del Document.
        Script osint-es-scripts/sanciones_ue.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        san = load("sanciones_ue")
        parciales = self.p_bool("incluir_parciales", False)
        out = []
        with httpx.Client(headers=san.HEADERS, timeout=60, follow_redirects=True) as c:
            consultas = [x for x in (ids["nif"], ids["nombre"]) if x]
            vistos = set()
            for q in consultas:
                for e in san.buscar(c, q, self.max_resultados)["resultados"]:
                    if e["referencia_ue"] in vistos or (e["coincidencia"] == "contiene" and not parciales):
                        continue
                    vistos.add(e["referencia_ue"])
                    url = next((r["url"] for r in e["reglamentos"] if r["url"]), None)
                    doc = documento(f"Sanciones UE {e['referencia_ue']} · {e['nombres'][0] if e['nombres'] else ''}",
                                    doc_type="sanción UE", source="EU Financial Sanctions Files", source_url=url,
                                    created_date=e["fecha_designacion"], author="Consejo de la UE",
                                    subject=", ".join(e["programas"]),
                                    description=f"Coincidencia: {e['coincidencia']}. Tipo: {e['tipo']}. "
                                                f"Nombres: {' | '.join(e['nombres'][:5])}. "
                                                f"Direcciones: {' | '.join(e['direcciones'][:3])}",
                                    keywords=e["programas"] or None, referencia_ue=e["referencia_ue"],
                                    coincidencia=e["coincidencia"],
                                    identificadores=[f"{i['tipo']}: {i['numero']}" for i in e["identificadores"]] or None)
                    rel = "POSIBLE_COINCIDENCIA_SANCIONES" if e["coincidencia"] == "contiene" else "SANCIONADA"
                    out.append(Hallazgo(doc, rel))
        return out


InputType = OrgToSancionesUe.InputType
OutputType = OrgToSancionesUe.OutputType
