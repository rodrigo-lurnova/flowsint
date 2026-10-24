"""Individual -> Email: correos corporativos probables (osint-es-scripts/correos.py)."""
import os
from typing import Any, Dict, List, Optional

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_enrichers.es._contexto import nombre_de, vecinos
from flowsint_enrichers.es._scripts import load
from flowsint_types.email import Email
from flowsint_types.individual import Individual


@flowsint_enricher
class EsIndividualToEmailsEnricher(Enricher):
    """[CORREOS] Candidate corporate emails for a person (pattern + MX + web + GitHub + Gravatar)."""

    InputType = Individual
    OutputType = Email

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {"name": "dominio", "type": "string", "required": False,
             "description": "Dominio de la empresa. Si se omite, se usa el Domain/Website vecino en el grafo."},
            {"name": "min_confianza", "type": "string", "default": "0.4", "required": False,
             "description": "Confianza mínima (0-1) para crear el nodo."},
            {"name": "max_resultados", "type": "string", "default": "3", "required": False,
             "description": "Máximo de correos por persona."},
            {"name": "HUNTER_API_KEY", "type": "vaultSecret", "required": False,
             "description": "Opcional: clave de Hunter.io para domain-search / email-finder."},
        ]

    @classmethod
    def name(cls) -> str:
        return "es_individual_to_emails"

    @classmethod
    def category(cls) -> str:
        return "Individual"

    @classmethod
    def key(cls) -> str:
        return "full_name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Genera correos corporativos candidatos para una persona y los puntúa con
        evidencias públicas: patrón real de la empresa (correos de su web y commits
        públicos de GitHub del dominio), MX del dominio, Gravatar y Hunter.io si hay
        clave. No hace verificación SMTP. El dominio sale del parámetro `dominio` o
        de los nodos Domain/Website conectados a la persona o a su empresa.
        La confianza y las evidencias se registran en el log del sketch.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        correos = load("correos")
        self.mapping, out = [], []
        hk = self.params.get("HUNTER_API_KEY") or os.getenv("HUNTER_API_KEY")
        minc = float(self.params.get("min_confianza") or 0.4)
        maxr = int(self.params.get("max_resultados") or 3)
        for ind in data:
            nombre = nombre_de(ind)
            dominios = [self.params["dominio"]] if self.params.get("dominio") else \
                vecinos(self._graph_service, self.sketch_id, nombre)["dominios"]
            if not nombre or not dominios:
                Logger.info(self.sketch_id, {"message": f"[CORREOS] {nombre or '?'}: sin dominio asociado, se omite"})
                continue
            for dom in dominios[:2]:
                try:
                    res = correos.buscar_correos(nombre, dom, max_paginas=8, max_verificar=4, hunter_key=hk)
                except Exception as exc:
                    Logger.error(self.sketch_id, {"message": f"[CORREOS] {nombre} @ {dom}: {exc}"})
                    continue
                for c in [c for c in res["candidatos"] if c["confianza"] >= minc][:maxr]:
                    email = Email(email=c["email"])
                    out.append(email)
                    self.mapping.append((ind, email, c))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for ind, email, c in self.mapping:
            self.create_node(ind)
            self.create_node(email)
            self.create_relationship(ind, email, "PROBABLE_EMAIL")
            self.log_graph_message(f"{nombre_de(ind)} -> {email.email} ({c['confianza']:.0%}: "
                                   f"{'; '.join(c['evidencias'][:3])})")
        return results


InputType = EsIndividualToEmailsEnricher.InputType
OutputType = EsIndividualToEmailsEnricher.OutputType
