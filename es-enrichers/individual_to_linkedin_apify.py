"""Individual -> SocialAccount (LinkedIn) vía Apify (osint-es-scripts/apify_actores.py).

AVISO: los actores de LinkedIn rascan linkedin.com, cuyas condiciones lo prohíben;
usarlos es decisión del usuario. Limítalo a personas vinculadas a la empresa
investigada (RGPD: minimización).
"""
import os
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es._contexto import nombre_de, vecinos
from flowsint_enrichers.es._scripts import load
from flowsint_types.individual import Individual
from flowsint_types.social_account import SocialAccount
from flowsint_types.username import Username


def perfil_a_social(p: dict) -> SocialAccount:
    return SocialAccount(
        username=Username(value=p["usuario"], platform="linkedin") if p.get("usuario") else None,
        display_name=p.get("nombre"), profile_url=p.get("url"), platform="linkedin",
        bio=" · ".join(x for x in (p.get("titular"), p.get("cargo") and p.get("empresa")
                                   and f"{p['cargo']} en {p['empresa']}") if x) or None,
        location=p.get("ubicacion"), profile_picture_url=p.get("foto"),
        followers_count=p.get("seguidores"))


@flowsint_enricher
class EsIndividualToLinkedinEnricher(Enricher):
    """[APIFY] Find a person's LinkedIn profile by name + company (paid, opt-in)."""

    InputType = Individual
    OutputType = SocialAccount

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {"name": "APIFY_TOKEN", "type": "vaultSecret", "required": False,
             "description": "Token de Apify (si no está en la vault se usa la variable de entorno APIFY_TOKEN)."},
            {"name": "empresa", "type": "string", "required": False,
             "description": "Empresa actual. Si se omite, se usa la Organization vecina en el grafo."},
            {"name": "empresa_linkedin_url", "type": "string", "required": False,
             "description": "URL linkedin.com/company/... para filtrar por empresa actual (más preciso)."},
            {"name": "min_confianza", "type": "string", "default": "0.5", "required": False,
             "description": "Confianza mínima (0-1) para crear el nodo."},
            {"name": "max_resultados", "type": "string", "default": "3", "required": False,
             "description": "Perfiles por persona que se piden a Apify (coste ~0,002 $ por perfil)."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_individual_to_linkedin"

    @classmethod
    def category(cls) -> str:
        return "Individual"

    @classmethod
    def key(cls) -> str:
        return "full_name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca el perfil de LinkedIn de una persona con el actor de Apify
        harvestapi/linkedin-profile-search-by-name (sin cookies; configurable con
        APIFY_ACTOR_LINKEDIN_BUSCAR) y puntúa cada candidato por coincidencia de
        nombre y de empresa. Gasto máximo por ejecución: APIFY_MAX_USD (0,10 $).
        Requiere APIFY_TOKEN; sin token no hace ninguna petición.
        AVISO: el scraping de LinkedIn incumple sus condiciones de uso.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        apify = load("apify_actores")
        token = self.params.get("APIFY_TOKEN") or os.getenv("APIFY_TOKEN")
        if not token:
            Logger.info(self.sketch_id, {"message": "[APIFY] Falta APIFY_TOKEN (vault o entorno): se omite"})
            return []
        self.mapping, out = [], []
        minc = float(self.params.get("min_confianza") or 0.5)
        maxr = int(self.params.get("max_resultados") or 3)
        for ind in data:
            nombre = nombre_de(ind)
            ctx = vecinos(self._graph_service, self.sketch_id, nombre)
            empresa = self.params.get("empresa") or (ctx["empresas"] or [None])[0]
            url_emp = self.params.get("empresa_linkedin_url") or (ctx["linkedin"] or [None])[0]
            try:
                perfiles = apify.linkedin_buscar(nombre, empresa=empresa, empresa_url=url_emp,
                                                 max_items=maxr, token=token)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[APIFY] {nombre}: {exc}"})
                continue
            for p in [p for p in perfiles if p["confianza"] >= minc][:maxr]:
                acc = perfil_a_social(p)
                out.append(acc)
                self.mapping.append((ind, acc, p))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for ind, acc, p in self.mapping:
            self.create_node(ind)
            self.create_node(acc)
            self.create_relationship(ind, acc, "HAS_LINKEDIN")
            self.log_graph_message(f"{nombre_de(ind)} -> {p['url']} ({p['confianza']:.0%}: "
                                   f"{'; '.join(p['evidencias'])})")
        return results


InputType = EsIndividualToLinkedinEnricher.InputType
OutputType = EsIndividualToLinkedinEnricher.OutputType
