from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento, eur, recorta
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.organization import Organization


@flowsint_enricher
class OrgToSubvencionesBdns(OrgEsEnricher):
    """[BDNS] Subvenciones y ayudas concedidas a una empresa (infosubvenciones.es)."""

    FUENTE = "[BDNS]"
    PARAMS = [{"name": "incluir_organismos", "type": "select", "default": "true", "required": False,
               "description": "Crear el organismo concedente como Organization.",
               "options": [{"label": "Sí", "value": "true"}, {"label": "No", "value": "false"}]}]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_subvenciones_bdns"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca la organización en la Base de Datos Nacional de Subvenciones por NIF (propiedad
        `nif` del nodo) o, si no lo tiene, por denominación, y crea un Document por concesión
        (fecha, importe, convocatoria, enlace a la base reguladora) con la relación
        RECIBE_SUBVENCION. Opcionalmente enlaza el organismo concedente (CONCEDIDA_POR).
        Por nombre solo acepta beneficiarios cuya denominación coincide (no los que empiezan igual).
        API pública sin clave; script osint-es-scripts/bdns.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        bdns = load("bdns")
        res = load("resolver_empresa")
        out: List[Hallazgo] = []
        with httpx.Client(headers=bdns.HEADERS, timeout=40) as c:
            terceros = bdns.buscar_terceros(c, ids["nif"] or ids["nombre"])
            if not ids["nif"]:
                terceros = [t for t in terceros
                            if res.coincidencia(ids["nombre"], t["descripcion"].partition(" - ")[2]) != "parcial"]
                if len({t["descripcion"].partition(" - ")[0] for t in terceros}) > 1:
                    self.log(f"Homonimia: {len(terceros)} beneficiarios con NIF distinto para «{ids['nombre']}»; "
                             "fija la propiedad nif del nodo para filtrar.")
            for t in terceros[:5]:
                for i in bdns.concesiones(c, t["id"], self.max_resultados):
                    importe = i.get("importe") or 0
                    doc = documento(
                        f"BDNS {i.get('fechaConcesion')} · {eur(importe)} · {recorta(i.get('convocatoria'), 70)} "
                        f"[{i.get('codConcesion')}]",
                        doc_type="subvención", source="BDNS", created_date=i.get("fechaConcesion"),
                        source_url=i.get("urlBR") or f"https://www.infosubvenciones.es/bdnstrans/GE/es/convocatorias/{i.get('numeroConvocatoria')}",
                        author=" / ".join(x for x in (i.get("nivel2"), i.get("nivel3")) if x),
                        subject=i.get("convocatoria"), description=f"{i.get('instrumento') or ''} — {t['descripcion']}".strip(" —"),
                        importe=importe, beneficiario=t["descripcion"], cod_concesion=i.get("codConcesion"),
                        numero_convocatoria=i.get("numeroConvocatoria"))
                    extra = []
                    if self.p_bool("incluir_organismos", True) and i.get("nivel3"):
                        organo = Organization(name=f"{i['nivel3']} ({i.get('nivel2') or i.get('nivel1')})",
                                              nivel_administracion=i.get("nivel1"))
                        extra.append((doc, organo, "CONCEDIDA_POR"))
                    out.append(Hallazgo(doc, "RECIBE_SUBVENCION", extra))
                    if len(out) >= self.max_resultados:
                        return out
        return out


InputType = OrgToSubvencionesBdns.InputType
OutputType = OrgToSubvencionesBdns.OutputType
