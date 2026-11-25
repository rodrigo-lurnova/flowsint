"""Individual -> SocialAccount: perfiles públicos en redes (osint-es-scripts/perfiles.py). OPCIONAL.

Solo se ejecuta sobre personas vinculadas a una empresa (Organization vecina en el
grafo o parámetro `empresa`): due diligence con minimización RGPD, no perfilado de
particulares. No está en los flows por defecto.
"""

from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.es import _evidencias as ev
from flowsint_enrichers.es._contexto import nombre_de, vecinos
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.individual import Individual
from flowsint_types.social_account import SocialAccount
from flowsint_types.username import Username

FUENTE = "Perfiles públicos"


@flowsint_enricher
class EsIndividualToSocialsEnricher(Enricher):
    """[PERFILES] Public social profiles of a company-linked person (opt-in)."""

    InputType = Individual
    OutputType = SocialAccount

    def __init__(
        self,
        sketch_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        vault=None,
        params: Optional[Dict[str, Any]] = None,
        graph_service=None,
    ):
        super().__init__(
            sketch_id=sketch_id,
            scan_id=scan_id,
            params_schema=self.get_params_schema(),
            vault=vault,
            params=params,
            graph_service=graph_service,
        )
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "empresa",
                "type": "string",
                "required": False,
                "description": "Empresa vinculada. Si se omite se usa la Organization vecina; sin empresa no se busca.",
            },
            {
                "name": "redes",
                "type": "string",
                "default": "linkedin,x,instagram,facebook,tiktok,youtube,github,medium",
                "required": False,
                "description": "Redes a buscar, separadas por comas.",
            },
            {
                "name": "min_confianza",
                "type": "string",
                "default": "0.6",
                "required": False,
                "description": "Confianza mínima (0-1).",
            },
            {
                "name": "usar_apify",
                "type": "string",
                "default": "false",
                "required": False,
                "description": "Añadir Google vía Apify (de pago, ~0,005 $ por consulta).",
            },
            {
                "name": "BRAVE_API_KEY",
                "type": "vaultSecret",
                "required": False,
                "description": "Brave Search (si no está en la vault ni en el entorno se usa SearXNG).",
            },
            {
                "name": "APIFY_TOKEN",
                "type": "vaultSecret",
                "required": False,
                "description": "Solo si usar_apify=true (si no está en la vault se usa la variable de entorno).",
            },
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
        con búsquedas `site:` en Brave (BRAVE_API_KEY; sin clave, SearXNG local) y la
        API de GitHub; Google vía Apify si usar_apify=true. No entra en las redes.
        El nombre debe aparecer en el título del resultado; la empresa o el dominio
        suben la confianza. Sin empresa vinculada no hace nada.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        perfiles = load("perfiles")
        token = ev.clave(self, "APIFY_TOKEN")
        brave_key = ev.clave(self, "BRAVE_API_KEY")
        redes = [
            r.strip() for r in (self.params.get("redes") or "").split(",") if r.strip()
        ] or None
        minc = float(self.params.get("min_confianza") or 0.6)
        apify = str(self.params.get("usar_apify")).lower() == "true"
        if apify and not token:
            ev.sin_token(self)  # las fuentes gratuitas se consultan igual
            apify = False
        self.mapping, out = [], []
        omitidas: List[str] = []
        for ind in data:
            nombre, sin_contexto = nombre_de(ind), False
            if not nombre:
                omitidas.append("sin nombre utilizable")
                continue
            try:
                ctx = vecinos(self._graph_service, self.sketch_id, nombre)
            except Exception as exc:
                ev.fallo(self, FUENTE, nombre, exc)
                sin_contexto = True  # ya está anotado como fallo
                ctx = {"empresas": [], "dominios": [], "linkedin": []}
            empresa = self.params.get("empresa") or (ctx["empresas"] or [None])[0]
            if not empresa:
                if not sin_contexto:
                    omitidas.append("sin empresa vinculada")
                continue
            try:
                res = perfiles.buscar_perfiles(
                    nombre,
                    empresa,
                    (ctx["dominios"] or [None])[0],
                    redes,
                    apify,
                    minc,
                    apify_token=token,
                    brave_key=brave_key,
                )
            except Exception as exc:
                ev.fallo(self, FUENTE, nombre, exc, token)
                continue
            errores = res.get("errores") or []
            for error in errores:
                ev.error_de_fuente(self, FUENTE, nombre, error, token)
            for av in [a for a in res["avisos"] if a not in errores]:
                Logger.info(self.sketch_id, {"message": f"[PERFILES] {av}"})
            for p in res["perfiles"]:
                acc = SocialAccount(
                    username=Username(value=p["usuario"], platform=p["red"]),
                    platform=p["red"],
                    profile_url=p["url"],
                    display_name=p.get("nombre"),
                    bio=p.get("titulo"),
                    location=p.get("ubicacion"),
                    profile_picture_url=p.get("foto"),
                )
                out.append(acc)
                certeza = ev.certeza(p.get("confianza"))
                relacion = ev.relacion("HAS_PROFILE", certeza)
                self.mapping.append((ind, acc, p, relacion))
                ev.anotar(
                    self,
                    FUENTE,
                    nombre,
                    relacion,
                    p["url"],
                    certeza,
                    p.get("confianza"),
                    p.get("evidencias"),
                    p["url"],
                )
        ev.sin_buscar(self, FUENTE, "personas", len(data), omitidas)
        return out

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType] = None
    ) -> List[OutputType]:
        for ind, acc, p, relacion in self.mapping:
            self.create_node(ind)
            self.create_node(acc)
            self.create_relationship(ind, acc, relacion)
            self.log_graph_message(
                f"{nombre_de(ind)} -> {p['red']} {p['url']} ({ev.pct(p.get('confianza'))}: "
                f"{'; '.join((p.get('evidencias') or [])[:2])})"
            )
        return results


InputType = EsIndividualToSocialsEnricher.InputType
OutputType = EsIndividualToSocialsEnricher.OutputType
