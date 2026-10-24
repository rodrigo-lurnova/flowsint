"""Domain -> Email: correos publicados en la propia web del dominio (osint-es-scripts/correos.py)."""
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es._scripts import load
from flowsint_types.domain import Domain
from flowsint_types.email import Email


@flowsint_enricher
class EsDomainToWebEmailsEnricher(Enricher):
    """[WEB] Emails published on the domain's own website (contact, legal, team pages)."""

    InputType = Domain
    OutputType = Email

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [{"name": "max_paginas", "type": "string", "default": "12", "required": False,
                 "description": "Páginas del sitio a visitar (contacto, aviso legal, privacidad, equipo...)."}]

    @classmethod
    def name(cls) -> str:
        return "es_domain_to_web_emails"

    @classmethod
    def category(cls) -> str:
        return "Domain"

    @classmethod
    def key(cls) -> str:
        return "domain"

    @classmethod
    def documentation(cls) -> str:
        return """
        Crawl ligero (sin salir del dominio) de las páginas de contacto, aviso
        legal, privacidad y equipo, y extrae los correos @dominio que aparecen
        (mailto, texto, «[at]», ofuscación de Cloudflare). La página y el contexto
        de cada correo se registran en el log del sketch.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        correos = load("correos")
        self.mapping, out = [], []
        for dom in data:
            try:
                encontrados = correos.correos_web(dom.domain, max_paginas=int(self.params.get("max_paginas") or 12))
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[WEB] {dom.domain}: {exc}"})
                continue
            for c in encontrados:
                email = Email(email=c["email"])
                out.append(email)
                self.mapping.append((dom, email, c))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for dom, email, c in self.mapping:
            self.create_node(dom)
            self.create_node(email)
            self.create_relationship(dom, email, "PUBLISHES_EMAIL")
            self.log_graph_message(f"{dom.domain} -> {email.email} ({c['url']})")
        return results


InputType = EsDomainToWebEmailsEnricher.InputType
OutputType = EsDomainToWebEmailsEnricher.OutputType
