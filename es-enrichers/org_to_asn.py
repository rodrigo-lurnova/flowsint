"""Organization -> ASN sin clave de API (alternativa funcional a org_to_asn).

El enricher oficial ``org_to_asn`` depende de asnmap, que exige PDCP_API_KEY
(sin ella falla con "Could not read input from terminal") y además construye
``ASN(number=...)`` sin ``asn_str`` (campo obligatorio), por lo que nunca
devuelve resultados. Este enricher reproduce el comportamiento documentado
("Finds ASNs associated with an organization name") con fuentes públicas:

- PeeringDB  (``/api/net?name__contains=``) - redes registradas por nombre.
- RIPEstat   (``searchcomplete``)           - aut-num cuyo as-name/descr coincide.
- asnmap     (opcional, solo si hay PDCP_API_KEY en la vault o en el entorno).
- Team Cymru (DNS ``AS<n>.asn.cymru.com``)   - país, RIR y nombre registrado.
"""

import os
import re
import unicodedata
from typing import Any, Dict, List, Optional

import dns.exception
import dns.resolver
import requests

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.asn import ASN
from flowsint_types.organization import Organization

PEERINGDB_URL = "https://www.peeringdb.com/api/net"
RIPESTAT_URL = "https://stat.ripe.net/data/searchcomplete/data.json"
CYMRU_ZONE = "asn.cymru.com"
HTTP_TIMEOUT = 15
USER_AGENT = "flowsint-es-enrichers/1.0"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@flowsint_enricher
class EsOrgToAsnEnricher(Enricher):
    """[PeeringDB/RIPEstat] Find ASNs associated with an organization name (no API key)."""

    InputType = Organization
    OutputType = ASN

    def __init__(
        self,
        sketch_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        vault=None,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            sketch_id=sketch_id,
            scan_id=scan_id,
            params_schema=self.get_params_schema(),
            vault=vault,
            params=params,
        )

    @classmethod
    def get_params_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "PDCP_API_KEY",
                "type": "vaultSecret",
                "description": "Opcional. Clave de ProjectDiscovery Cloud para añadir resultados de asnmap.",
                "required": False,
            },
            {
                "name": "max_results",
                "type": "number",
                "description": "Máximo de ASNs por organización. Default: 50",
                "required": False,
                "default": 50,
            },
        ]

    @classmethod
    def name(cls) -> str:
        return "es_org_to_asn"

    @classmethod
    def category(cls) -> str:
        return "Organization"

    @classmethod
    def key(cls) -> str:
        return "name"

    @classmethod
    def documentation(cls) -> str:
        return """
        Busca los sistemas autónomos (ASN) asociados al nombre de una organización.

        Fuentes (sin clave de API):
        - PeeringDB: redes cuyo nombre contiene el de la organización.
        - RIPEstat searchcomplete: aut-num cuyo as-name/descr coincide.
        - Team Cymru DNS: completa país y registro (RIR) de cada ASN.

        Si existe PDCP_API_KEY en la vault, también se consulta asnmap
        (ProjectDiscovery). Solo se conservan coincidencias que contienen todas
        las palabras del nombre buscado (ignorando acentos y mayúsculas).
        """

    # ------------------------------------------------------------------ scan
    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.org_asn_mapping = []
        asns: Dict[int, ASN] = {}
        try:
            limit = int(self.params.get("max_results") or 50)
        except (TypeError, ValueError):
            limit = 50
        pdcp_key = self.get_secret("PDCP_API_KEY", os.getenv("PDCP_API_KEY"))

        for org in data:
            tokens = _norm(org.name).split()
            if not tokens:
                continue
            found: Dict[int, Dict[str, Any]] = {}
            for source, fn in (
                ("PeeringDB", self._peeringdb),
                ("RIPEstat", self._ripestat),
            ):
                try:
                    for number, label in fn(org.name):
                        found.setdefault(number, {"label": label, "sources": set()})
                        found[number]["sources"].add(source)
                except Exception as exc:
                    Logger.warn(self.sketch_id, {"message": f"[{source}] {org.name}: {exc}"})
            if pdcp_key:
                try:
                    for number, label in self._asnmap(org.name, pdcp_key):
                        found.setdefault(number, {"label": label, "sources": set()})
                        found[number]["sources"].add("asnmap")
                except Exception as exc:
                    Logger.warn(self.sketch_id, {"message": f"[asnmap] {org.name}: {exc}"})

            matched = [
                (n, info)
                for n, info in found.items()
                # asnmap ya filtra por organización; el resto se exige coincidencia
                if "asnmap" in info["sources"]
                or all(t in _norm(info["label"]) for t in tokens)
            ][:limit]

            for number, info in matched:
                if number not in asns:
                    cymru = self._cymru(number)
                    asns[number] = ASN(
                        asn_str=f"AS{number}",
                        number=number,
                        name=info["label"] or cymru.get("name"),
                        country=cymru.get("country"),
                        description=(
                            f"{cymru.get('name') or info['label']}"
                            f" | RIR: {cymru.get('registry', '?')}"
                            f" | fuentes: {', '.join(sorted(info['sources']))}"
                        ),
                    )
                self.org_asn_mapping.append((org, asns[number]))
            Logger.info(
                self.sketch_id,
                {"message": f"[ES_ORG_TO_ASN] {org.name}: {len(matched)} ASN(s)"},
            )
        return list(asns.values())

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType] = None
    ) -> List[OutputType]:
        if not self._graph_service:
            return results
        created = set()
        for org, asn in getattr(self, "org_asn_mapping", []):
            self.create_node(org)
            if asn.asn_str not in created:
                self.create_node(asn)
                created.add(asn.asn_str)
            self.create_relationship(org, asn, "BELONGS_TO")
            self.log_graph_message(f"{org.name} -> {asn.asn_str} ({asn.name or '?'})")
        return results

    # --------------------------------------------------------------- sources
    def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.get(
            url, params=params, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        r.raise_for_status()
        return r.json()

    def _peeringdb(self, name: str):
        data = self._get(PEERINGDB_URL, {"name__contains": name, "fields": "asn,name,aka"})
        for net in data.get("data", []):
            if net.get("asn"):
                label = net.get("name") or ""
                if net.get("aka"):
                    label = f"{label} ({net['aka']})"
                yield int(net["asn"]), label

    def _ripestat(self, name: str):
        data = self._get(
            RIPESTAT_URL, {"resource": name, "limit": 100, "sourceapp": "flowsint"}
        )
        for cat in data.get("data", {}).get("categories", []):
            if cat.get("category") != "ASNs":
                continue
            for sug in cat.get("suggestions", []):
                m = re.match(r"(?i)^AS(\d+)$", str(sug.get("value", "")).strip())
                if m:
                    yield int(m.group(1)), sug.get("description") or ""

    def _asnmap(self, name: str, api_key: str):
        from tools.network.asnmap import AsnmapTool

        tool = AsnmapTool()
        # Lista de argumentos para que nombres con espacios no se partan.
        out = super(AsnmapTool, tool).launch(
            ["-org", name, "-silent", "-json"], environment={"PDCP_API_KEY": api_key}
        )
        import json

        for line in (out or "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            m = re.match(r"(?i)^AS(\d+)$", str(row.get("as_number", "")).strip())
            if m:
                yield int(m.group(1)), row.get("as_name") or ""

    def _cymru(self, number: int) -> Dict[str, str]:
        """'3352 | ES | ripencc | 1994-08-11 | TELEFONICA_DE_ESPANA, ES'"""
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0
            answer = resolver.resolve(f"AS{number}.{CYMRU_ZONE}", "TXT")
            txt = b"".join(answer[0].strings).decode(errors="ignore")
            parts = [p.strip() for p in txt.split("|")]
            return {
                "country": parts[1] if len(parts) > 1 and parts[1] else None,
                "registry": parts[2] if len(parts) > 2 else None,
                "name": parts[4] if len(parts) > 4 else None,
            }
        except (dns.exception.DNSException, IndexError, ValueError):
            return {}


InputType = EsOrgToAsnEnricher.InputType
OutputType = EsOrgToAsnEnricher.OutputType
