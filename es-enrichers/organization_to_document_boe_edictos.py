from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento, recorta
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.organization import Organization


@flowsint_enricher
class OrgToEdictosBoe(OrgEsEnricher):
    """[BOE] Notificaciones (TEU) y edictos judiciales (TEJU) que mencionan a la empresa."""

    FUENTE = "[BOE]"
    MAX_DEFECTO = 20
    PARAMS = [
        {"name": "tablones", "type": "select", "default": "ambos", "required": False,
         "description": "TEU (administrativos), TEJU (judiciales) o ambos.",
         "options": [{"label": "Ambos", "value": "ambos"}, {"label": "TEU", "value": "teu"},
                     {"label": "TEJU", "value": "teju"}]},
        {"name": "desde", "type": "string", "required": False, "description": "Fecha desde (dd/mm/aaaa)."},
        {"name": "incluir_organismos", "type": "select", "default": "true", "required": False,
         "description": "Crear el organismo emisor como Organization.",
         "options": [{"label": "Sí", "value": "true"}, {"label": "No", "value": "false"}]},
    ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_edictos_boe"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca el NIF (o, si no hay, la denominación sin forma jurídica) en el Tablón Edictal
        Único (TEU, notificaciones administrativas) y en el Tablón Edictal Judicial Único
        (TEJU) del BOE. Crea un Document por edicto (referencia BOE-N, asunto, PDF) con la
        relación MENCIONADA_EN_EDICTO y, opcionalmente, el organismo emisor (PUBLICADO_POR).
        Buscar por NIF es mucho más preciso que por nombre. El texto íntegro solo es público
        3 meses. Script osint-es-scripts/boe_edictos.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        boe = load("boe_edictos")
        t = str(self.params.get("tablones") or "ambos")
        tablones = {"teu": [False], "teju": [True]}.get(t, [False, True])
        texto = ids["nif"] or ids["base"]
        out: List[Hallazgo] = []
        with httpx.Client(headers=boe.HEADERS, timeout=40, follow_redirects=True) as c:
            for judicial in tablones:
                r = boe.buscar(c, texto, judicial, str(self.params.get("desde") or ""), "", 50)
                for i in r["resultados"]:
                    doc = documento(f"{r['fuente']} {i['referencia']} · {recorta(i['asunto'], 90)}",
                                    doc_type="edicto judicial" if judicial else "notificación administrativa",
                                    source=f"BOE {r['fuente']}", source_url=i["pdf"], author=i["organismo"],
                                    subject=i["asunto"], description=i["boletin"], referencia=i["referencia"],
                                    consulta=texto)
                    extra = []
                    if self.p_bool("incluir_organismos", True) and i.get("organismo"):
                        extra.append((doc, Organization(name=i["organismo"]), "PUBLICADO_POR"))
                    out.append(Hallazgo(doc, "MENCIONADA_EN_EDICTO", extra))
        return out


InputType = OrgToEdictosBoe.InputType
OutputType = OrgToEdictosBoe.OutputType
