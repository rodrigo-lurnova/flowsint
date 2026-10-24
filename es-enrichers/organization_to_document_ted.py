from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento, eur, recorta
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.organization import Organization

SI_NO = [{"label": "Sí", "value": "true"}, {"label": "No", "value": "false"}]


@flowsint_enricher
class OrgToContratosTed(OrgEsEnricher):
    """[TED] Anuncios de contratación pública europea (DOUE S) en los que la empresa es adjudicataria."""

    FUENTE = "[TED]"
    PARAMS = [
        {"name": "solo_espana", "type": "select", "default": "true", "required": False,
         "description": "Solo compradores españoles.", "options": SI_NO},
        {"name": "incluir_compradores", "type": "select", "default": "true", "required": False,
         "description": "Crear el poder adjudicador como Organization.", "options": SI_NO},
    ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_contratos_ted"

    @classmethod
    def documentation(cls) -> str:
        return """
        Consulta la API pública v3 de TED (sin clave) por identificador del adjudicatario
        (winner-identifier, con y sin prefijo ES) si el nodo tiene NIF, o por nombre
        (winner-name ~) si no. Crea un Document por anuncio (nº de publicación, título, importe,
        CPV, enlace) con la relación ADJUDICATARIA y, opcionalmente, el comprador (LICITADO_POR).
        Por nombre descarta los anuncios cuyo adjudicatario no coincide con la denominación.
        Solo contratos por encima de umbrales UE. Script osint-es-scripts/ted.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        ted = load("ted")
        res = load("resolver_empresa")
        with httpx.Client(headers=ted.HEADERS, timeout=60) as c:
            r = ted.buscar(c, ids["nif"] or ids["nombre"], self.max_resultados, self.p_bool("solo_espana", True))
        self.log(f"{r['consulta']}: {r['total']} anuncios")
        out = []
        for i in r["resultados"]:
            if not ids["nif"] and all(res.coincidencia(ids["nombre"], n) == "parcial" for n in i["adjudicatarios"]):
                continue
            imp = (eur(i["importe"]) if (i["moneda"] or "EUR") == "EUR" else f"{i['importe']} {i['moneda']}")
            doc = documento(f"TED {i['publicacion']} · {recorta(i['titulo'], 80)}",
                            doc_type="contrato público (TED)", source="TED", source_url=i["url"],
                            created_date=i["fecha"], author=i["comprador"], subject=i["titulo"],
                            description=f"{imp} · adjudicatario: {', '.join(i['adjudicatarios'])} "
                                        f"{', '.join(i['identificadores'])}",
                            keywords=i["cpv"] or None, importe=i["importe"], moneda=i["moneda"],
                            publicacion=i["publicacion"], pdf=i["pdf"])
            extra = []
            if self.p_bool("incluir_compradores", True) and i["comprador"]:
                extra.append((doc, Organization(name=i["comprador"]), "LICITADO_POR"))
            out.append(Hallazgo(doc, "ADJUDICATARIA", extra))
        return out


InputType = OrgToContratosTed.InputType
OutputType = OrgToContratosTed.OutputType
