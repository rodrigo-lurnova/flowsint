from typing import Any, Dict, List, Optional

from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, org_ids
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.domain import Domain
from flowsint_types.organization import Organization

SI_NO = [{"label": "Sí", "value": "true"}, {"label": "No", "value": "false"}]


@flowsint_enricher
class OrgToIdentidadEs(OrgEsEnricher):
    """[ES IDENTIDAD] Resuelve NIF/CIF, LEI y web probable de una empresa española (BDNS, GLEIF, TED, CNMV)."""

    OutputType = Organization
    FUENTE = "[ES IDENTIDAD]"
    MAX_DEFECTO = 5
    PARAMS = [
        {"name": "buscar_web", "type": "select", "default": "true", "required": False,
         "description": "Probar <nombre>.es/.com/... y crear Domain (WEB_PROBABLE).", "options": SI_NO},
        {"name": "fuentes", "type": "string", "default": "bdns,gleif,ted,cnmv", "required": False,
         "description": "Fuentes de candidatos, separadas por comas."},
    ]

    @classmethod
    def name(cls) -> str:
        return "es_org_resolver_identidad"

    @classmethod
    def documentation(cls) -> str:
        return """
        Ejecuta osint-es-scripts/resolver_empresa.py sobre la denominación (o el NIF) del nodo.
        Si hay UN único candidato con denominación exacta (o el nodo ya tenía NIF), completa el
        propio nodo con nif, nif_valido, tipo_entidad, lei, denominacion_normalizada,
        denominaciones y web; si hay homonimia NO rellena el NIF y crea cada candidato como
        Organization aparte («DENOMINACIÓN (NIF)») con la relación POSIBLE_IDENTIDAD, para que
        el analista elija. Los dominios que resuelven se crean como Domain con WEB_PROBABLE
        (propiedad confianza: alta si la portada muestra el NIF). `max_resultados` = máximo de
        candidatos creados.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        raise NotImplementedError  # este enricher sobrescribe scan

    async def scan(self, data: List[Organization]) -> List[Organization]:
        import asyncio

        res_mod = load("resolver_empresa")
        fuentes = [f.strip() for f in str(self.params.get("fuentes") or "bdns,gleif,ted,cnmv").split(",")
                   if f.strip() in res_mod.FUENTES]
        web = self.p_bool("buscar_web", True)
        self.hallazgos = []
        results: List[Organization] = []
        for org in data:
            ids = org_ids(org)
            consulta = ids["nif"] or ids["nombre"]
            if not consulta:
                continue
            try:
                r = await asyncio.to_thread(res_mod.resolver, consulta, fuentes, web, 15)
            except Exception as e:
                self.log(f"Error resolviendo {consulta}: {type(e).__name__}: {e}", error=True)
                results.append(org)
                continue
            for av in r["avisos"]:
                self.log(f"{consulta}: {av}")
            exactos = [c for c in r["candidatos"] if c["coincidencia"] == "exacta"]
            elegido = exactos[0] if len(exactos) == 1 else None

            if elegido:
                actualizado = Organization(**{**org.model_dump(exclude_unset=True), **self._props(elegido, r)})
                self.hallazgos.append((org, Hallazgo(actualizado, "")))  # "" = actualizar el nodo de entrada
                destino = actualizado
            else:
                destino = org
            for c in r["candidatos"][: self.max_resultados]:
                if c is elegido:
                    continue
                cand = self._candidato(c, r)
                self.hallazgos.append((destino, Hallazgo(cand, "POSIBLE_IDENTIDAD")))
            if web:
                for w in r["webs"]:
                    if w["confianza"] == "baja":
                        continue
                    dom = Domain(domain=w["dominio"], confianza=w["confianza"], titulo_web=w.get("titulo"),
                                 nif_en_portada=", ".join(w["nif_en_portada"]) or None)
                    self.hallazgos.append((destino, Hallazgo(dom, "WEB_PROBABLE")))
            # Aguas abajo del flow solo sigue la empresa investigada (resuelta o no), nunca los homónimos:
            # los candidatos y webs quedan en el grafo vía postprocess.
            results.append(destino)
        return results

    @staticmethod
    def _props(c: Dict[str, Any], r: Dict[str, Any]) -> Dict[str, Any]:
        webs = [w["dominio"] for w in r["webs"] if c["nif"] and c["nif"] in w["nif_en_portada"]]
        props = {"nif": c["nif"], "nif_valido": c["valido"], "tipo_entidad": c["detalle"] or c["tipo"],
                 "lei": c["lei"], "denominacion_normalizada": c["normalizada"],
                 "denominaciones": c["denominaciones"][:10], "fuentes_identidad": c["fuentes"],
                 "homonimia": r["homonimia"], "web": webs[0] if webs else None,
                 "id_bdns": c["id_bdns"][0] if c["id_bdns"] else None}
        g = c.get("gleif") or {}
        if g:
            props.update(gleif_estado=g.get("estado"), gleif_ciudad=g.get("ciudad"), gleif_url=g.get("url"))
        return {k: v for k, v in props.items() if v not in (None, [], "")}

    def _candidato(self, c: Dict[str, Any], r: Dict[str, Any]) -> Organization:
        res_mod = load("resolver_empresa")
        den = res_mod.mejor_denominacion(c) or c["nif"] or c["lei"]
        ident = c["nif"] or f"LEI {c['lei']}"
        # nom_complet distinto por candidato: el nodeLabel no colisiona entre homónimos.
        return Organization(name=den, nom_raison_sociale=den, nom_complet=f"{den} ({ident})",
                            coincidencia=c["coincidencia"], **self._props(c, r))

    def postprocess(self, results: List[Any], input_data: List[Organization] = None) -> List[Any]:
        if not self._graph_service:
            return results
        for org, h in self.hallazgos:
            self.create_node(h.nodo)
            if h.relacion:
                self.create_node(org)
                self.create_relationship(org, h.nodo, h.relacion)
                self.log_graph_message(f"{org.nodeLabel}: {h.relacion} -> {h.nodo.nodeLabel}")
            else:
                self.log_graph_message(f"{org.nodeLabel}: NIF {getattr(h.nodo, 'nif', None)} "
                                       f"LEI {getattr(h.nodo, 'lei', None)}")
        return results


InputType = OrgToIdentidadEs.InputType
OutputType = OrgToIdentidadEs.OutputType
