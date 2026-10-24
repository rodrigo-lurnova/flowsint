"""Individual -> SocialAccount: perfiles públicos en redes (osint-es-scripts/perfiles.py). OPCIONAL.

Solo se ejecuta sobre personas vinculadas a una empresa (Organization vecina en el
grafo o parámetro `empresa`): due diligence con minimización RGPD, no perfilado de
particulares. No está en los flows por defecto.
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


@flowsint_enricher
class EsIndividualToSocialsEnricher(Enricher):
    """[PERFILES] Public social profiles of a company-linked person (opt-in)."""

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
            {"name": "empresa", "type": "string", "required": False,
             "description": "Empresa vinculada. Si se omite se usa la Organization vecina; sin empresa no se busca."},
            {"name": "redes", "type": "string", "default": "linkedin,x,instagram,facebook,tiktok,youtube,github,medium",
             "required": False, "description": "Redes a buscar, separadas por comas."},
            {"name": "min_confianza", "type": "string", "default": "0.6", "required": False,
             "description": "Confianza mínima (0-1)."},
            {"name": "usar_apify", "type": "string", "default": "false", "required": False,
             "description": "Añadir Google vía Apify (de pago, ~0,005 $ por consulta)."},
            {"name": "APIFY_TOKEN", "type": "vaultSecret", "required": False,
             "description": "Solo si usar_apify=true (si no está en la vault se usa la variable de entorno)."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_individual_to_socials"

    @classmethod
    def category(cls) -> str:
        return "Individual"

    @classmethod
    def key(cls) -> str:
        return "full_name"

    @classmethod
    def documentation(cls) -> str:
        return """
        OPCIONAL. Busca perfiles públicos (LinkedIn, X, Instagram, Facebook, TikTok,
        YouTube, GitHub, Medium) de una persona vinculada a la empresa investigada,
        con búsquedas `site:` en el SearXNG local (yandex/seznam; SEARXNG_URL) y la
        API de GitHub; Google vía Apify si usar_apify=true. No entra en las redes.
        El nombre debe aparecer en el título del resultado; la empresa o el dominio
        suben la confianza. Sin empresa vinculada no hace nada.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        perfiles = load("perfiles")
        if self.params.get("APIFY_TOKEN"):
            os.environ.setdefault("APIFY_TOKEN", self.params["APIFY_TOKEN"])
        redes = [r.strip() for r in (self.params.get("redes") or "").split(",") if r.strip()] or None
        minc = float(self.params.get("min_confianza") or 0.6)
        apify = str(self.params.get("usar_apify")).lower() == "true"
        self.mapping, out = [], []
        for ind in data:
            nombre = nombre_de(ind)
            ctx = vecinos(self._graph_service, self.sketch_id, nombre)
            empresa = self.params.get("empresa") or (ctx["empresas"] or [None])[0]
            if not nombre or not empresa:
                Logger.info(self.sketch_id, {"message": f"[PERFILES] {nombre or '?'}: sin empresa vinculada, se omite"})
                continue
            try:
                res = perfiles.buscar_perfiles(nombre, empresa, (ctx["dominios"] or [None])[0], redes, apify, minc)
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[PERFILES] {nombre}: {exc}"})
                continue
            for av in res["avisos"]:
                Logger.info(self.sketch_id, {"message": f"[PERFILES] {av}"})
            for p in res["perfiles"]:
                acc = SocialAccount(username=Username(value=p["usuario"], platform=p["red"]), platform=p["red"],
                                    profile_url=p["url"], display_name=p.get("nombre"), bio=p.get("titulo"),
                                    location=p.get("ubicacion"), profile_picture_url=p.get("foto"))
                out.append(acc)
                self.mapping.append((ind, acc, p))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for ind, acc, p in self.mapping:
            self.create_node(ind)
            self.create_node(acc)
            self.create_relationship(ind, acc, "HAS_PROFILE")
            self.log_graph_message(f"{nombre_de(ind)} -> {p['red']} {p['url']} ({p['confianza']:.0%}: "
                                   f"{'; '.join(p['evidencias'][:2])})")
        return results


InputType = EsIndividualToSocialsEnricher.InputType
OutputType = EsIndividualToSocialsEnricher.OutputType
