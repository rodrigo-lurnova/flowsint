"""Organization -> Individual: empleados en LinkedIn vía Apify (osint-es-scripts/apify_actores.py).

AVISO: los actores de LinkedIn rascan linkedin.com, cuyas condiciones lo prohíben;
usarlos es decisión del usuario.
"""
import os
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.es.individual_to_linkedin_apify import perfil_a_social
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization


@flowsint_enricher
class EsOrgToLinkedinEmployeesEnricher(Enricher):
    """[APIFY] List a company's employees on LinkedIn (paid, opt-in)."""

    InputType = Organization
    OutputType = Individual

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
            {"name": "empresa_linkedin_url", "type": "string", "required": False,
             "description": "URL linkedin.com/company/...; si se omite se busca con Google (Apify, ~0,005 $)."},
            {"name": "cargos", "type": "string", "required": False,
             "description": "Filtro de cargos separados por comas (p. ej. CEO,CTO,Director)."},
            {"name": "max_resultados", "type": "string", "default": "15", "required": False,
             "description": "Empleados a pedir (0,02 $ por arranque + ~0,004 $ por perfil)."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_linkedin_employees"

    @classmethod
    def category(cls) -> str:
        return "Organization"

    @classmethod
    def key(cls) -> str:
        return "name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Lista empleados actuales de la empresa en LinkedIn con el actor de Apify
        harvestapi/linkedin-company-employees (sin cookies). Si no se da la URL de
        la página de empresa, la busca con apify/google-search-scraper
        (`site:linkedin.com/company "<nombre>"`). Crea Individual + SocialAccount
        con la relación WORKS_AT / HAS_LINKEDIN. Requiere APIFY_TOKEN.
        AVISO: el scraping de LinkedIn incumple sus condiciones de uso.
        """

    def _url_empresa(self, apify, org: Organization, token: str) -> Optional[str]:
        hits = apify.google([f'site:linkedin.com/company "{org.name}"'], token=token)
        for h in hits:
            if "linkedin.com/company/" in (h.get("url") or ""):
                return h["url"].split("?", 1)[0]
        return None

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        apify = load("apify_actores")
        token = self.params.get("APIFY_TOKEN") or os.getenv("APIFY_TOKEN")
        if not token:
            Logger.info(self.sketch_id, {"message": "[APIFY] Falta APIFY_TOKEN (vault o entorno): se omite"})
            return []
        self.mapping, out = [], []
        maxr = int(self.params.get("max_resultados") or 15)
        cargos = [c.strip() for c in (self.params.get("cargos") or "").split(",") if c.strip()] or None
        for org in data:
            try:
                url = self.params.get("empresa_linkedin_url") or self._url_empresa(apify, org, token)
                if not url:
                    Logger.info(self.sketch_id, {"message": f"[APIFY] {org.name}: sin página de LinkedIn"})
                    continue
                empleados = apify.linkedin_empleados(url, max_items=maxr, cargos=cargos, token=token)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[APIFY] {org.name}: {exc}"})
                continue
            for p in empleados:
                if not p.get("nombre"):
                    continue
                ind = Individual(full_name=p["nombre"], first_name=p["nombre"].split()[0],
                                 last_name=" ".join(p["nombre"].split()[1:]) or p["nombre"],
                                 social_media_profiles=[p["url"]] if p.get("url") else None)
                out.append(ind)
                self.mapping.append((org, ind, perfil_a_social(p) if p.get("url") else None, p, url))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for org, ind, acc, p, url in self.mapping:
            self.create_node(org)
            self.create_node(ind)
            self.create_relationship(ind, org, "WORKS_AT")
            if acc:
                self.create_node(acc)
                self.create_relationship(ind, acc, "HAS_LINKEDIN")
            self.log_graph_message(f"{org.name} <- {p['nombre']} ({p.get('cargo') or p.get('titular') or '?'})")
        return results


InputType = EsOrgToLinkedinEmployeesEnricher.InputType
OutputType = EsOrgToLinkedinEmployeesEnricher.OutputType
