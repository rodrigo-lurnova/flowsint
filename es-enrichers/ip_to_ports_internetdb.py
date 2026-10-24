"""Ip -> Port: puertos y software expuestos según Shodan InternetDB (pasivo, sin clave).

Sustituye a la plantilla ip-puertos-internetdb, que fusionaba mal los puertos y
etiquetaba la relación como HAS_SOCIAL_ACCOUNT, y a ip_to_ports, que en modo
pasivo exige PDCP_API_KEY. InternetDB devuelve lo último que vio Shodan; no se
escanea nada.
"""
from typing import Any, Dict, List, Optional

import requests

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types.ip import Ip
from flowsint_types.port import Port
from flowsint_types.technology import Technology

INTERNETDB = "https://internetdb.shodan.io/{}"


@flowsint_enricher
class EsIpToPortsInternetdbEnricher(Enricher):
    """[INTERNETDB] Open ports, software (CPE) and known CVEs for an IP (passive, no key)."""

    InputType = Ip
    OutputType = Port

    def __init__(self, sketch_id: Optional[str] = None, scan_id: Optional[str] = None,
                 vault=None, params: Optional[Dict[str, Any]] = None, graph_service=None):
        super().__init__(sketch_id=sketch_id, scan_id=scan_id, params_schema=self.get_params_schema(),
                         vault=vault, params=params, graph_service=graph_service)
        self.mapping: List[tuple] = []

    @classmethod
    def name(cls) -> str:
        return "es_ip_to_ports_internetdb"

    @classmethod
    def category(cls) -> str:
        return "Ip"

    @classmethod
    def key(cls) -> str:
        return "address"

    @classmethod
    def documentation(cls) -> str:
        return """
        Consulta Shodan InternetDB (https://internetdb.shodan.io/<ip>, gratuito y
        sin clave) y crea los puertos abiertos (OPEN_PORT), el software detectado
        por CPE (RUNS) y, si los hay, los CVE conocidos en el log. Es información
        pasiva: lo último que Shodan observó, no un escaneo en vivo.
        """

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        self.mapping, out = [], []
        for ip in data:
            try:
                r = requests.get(INTERNETDB.format(ip.address), timeout=15)
                if r.status_code == 404:
                    Logger.info(self.sketch_id, {"message": f"[INTERNETDB] {ip.address}: sin datos en Shodan"})
                    continue
                r.raise_for_status()
                d = r.json()
            except Exception as exc:
                Logger.error(self.sketch_id, {"message": f"[INTERNETDB] {ip.address}: {exc}"})
                continue
            ports = [Port(number=p, protocol="tcp", state="open") for p in d.get("ports", [])]
            techs = []
            for c in d.get("cpes", []):
                # cpe:/a:f5:nginx:1.26.3 -> producto "nginx", versión "1.26.3"
                partes = c.removeprefix("cpe:/").split(":")
                if len(partes) < 3:
                    continue
                nombre = f"{partes[1]} httpd" if partes[2] == "http_server" else partes[2].replace("_", " ")
                techs.append(Technology(name=nombre, version=partes[3] if len(partes) > 3 else None,
                                        category="os" if partes[0] == "o" else "software", source="Shodan InternetDB"))
            out += ports
            self.mapping.append((ip, ports, techs, d))
        return out

    def postprocess(self, results: List[OutputType], original_input: List[InputType] = None) -> List[OutputType]:
        for ip, ports, techs, d in self.mapping:
            self.create_node(ip)
            for p in ports:
                self.create_node(p)
                self.create_relationship(ip, p, "OPEN_PORT")
            for t in techs:
                self.create_node(t)
                self.create_relationship(ip, t, "RUNS")
            extra = f" · CVE: {', '.join(d['vulns'])}" if d.get("vulns") else ""
            extra += f" · etiquetas: {', '.join(d['tags'])}" if d.get("tags") else ""
            self.log_graph_message(f"{ip.address}: puertos {d.get('ports')} · {', '.join(d.get('cpes', [])) or 'sin CPE'}"
                                   f" · hostnames {d.get('hostnames')}{extra}")
        return results


InputType = EsIpToPortsInternetdbEnricher.InputType
OutputType = EsIpToPortsInternetdbEnricher.OutputType
