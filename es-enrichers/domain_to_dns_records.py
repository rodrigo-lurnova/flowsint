"""Domain -> DNSRecord: MX, TXT (SPF, verificaciones SaaS), DMARC, NS, CAA y SOA.

domain_to_dns solo resuelve A/AAAA; las plantillas dns-txt-saas y dns-mx-correo
guardan un único registro como Phrase. Este enricher crea un DNSRecord por valor.
"""
from typing import Any, Dict, List, Optional

import dns.exception
import dns.resolver

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.dns_record import DNSRecord
from flowsint_types.domain import Domain

TIPOS = ["MX", "TXT", "NS", "CAA", "SOA"]


def describir(tipo: str, valor: str) -> Optional[str]:
    v = valor.lower()
    if tipo == "TXT":
        if v.startswith("v=spf1"):
            return "SPF"
        for k, d in (("ms=", "Verificación Microsoft 365"), ("google-site-verification", "Verificación Google"),
                     ("facebook-domain-verification", "Verificación Meta"), ("atlassian", "Verificación Atlassian"),
                     ("hubspot", "Verificación HubSpot"), ("stripe", "Verificación Stripe"),
                     ("docusign", "Verificación DocuSign"), ("apple-domain", "Verificación Apple"),
                     ("openai-domain", "Verificación OpenAI"), ("anthropic-domain", "Verificación Anthropic")):
            if k in v:
                return d
    if tipo == "DMARC":
        return "Política DMARC"
    if tipo == "MX" and "outlook.com" in v:
        return "Correo en Microsoft 365"
    if tipo == "MX" and "google" in v:
        return "Correo en Google Workspace"
    return None


@flowsint_enricher
class EsDomainToDnsRecordsEnricher(Enricher):
    """[DNS] MX, TXT/SPF, DMARC, NS, CAA and SOA records of a domain."""

    InputType = Domain
    OutputType = DNSRecord

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []
        self.resolver = dns.resolver.Resolver()
        self.resolver.lifetime = 8.0

    @classmethod
    def name(cls) -> str:
        return "es_domain_to_dns_records"

    @classmethod
    def category(cls) -> str:
        return "Domain"

    @classmethod
    def key(cls) -> str:
        return "domain"

    def _consulta(self, nombre: str, tipo: str) -> List[tuple]:
        try:
            ans = self.resolver.resolve(nombre, tipo)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
            return []
        out = []
        for r in ans:
            if tipo == "TXT":
                out.append(("".join(s.decode(errors="replace") for s in r.strings), None))
            elif tipo == "MX":
                out.append((str(r.exchange).rstrip("."), r.preference))
            else:
                out.append((r.to_text().rstrip("."), None))
        return [(v, p, ans.rrset.ttl) for v, p in out]

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.mapping, out = [], []
        for dom in data:
            consultas = [(dom.domain, t, t) for t in TIPOS] + [(f"_dmarc.{dom.domain}", "TXT", "DMARC")]
            for nombre, tipo, etiqueta in consultas:
                try:
                    filas = self._consulta(nombre, tipo)
                except Exception as exc:
                    Logger.error(self.sketch_id, {"message": f"[DNS] {nombre} {tipo}: {exc}"})
                    continue
                for valor, prioridad, ttl in filas:
                    rec = DNSRecord(value=valor, record_type=etiqueta, ttl=ttl, priority=prioridad,
                                    description=describir(etiqueta, valor), source="dnspython", is_active=True)
                    out.append(rec)
                    self.mapping.append((dom, rec))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for dom, rec in self.mapping:
            self.create_node(dom)
            self.create_node(rec)
            self.create_relationship(dom, rec, f"HAS_{rec.record_type}_RECORD")
            self.log_graph_message(f"{dom.domain} {rec.record_type}: {rec.value[:120]}"
                                   f"{' (' + rec.description + ')' if rec.description else ''}")
        return results


InputType = EsDomainToDnsRecordsEnricher.InputType
OutputType = EsDomainToDnsRecordsEnricher.OutputType
