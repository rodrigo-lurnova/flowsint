"""Contexto de grafo para los enrichers de personas.

Un Individual llega solo con su nombre; la empresa y el dominio con los que hay
que cruzarlo están en sus vecinos del sketch (p. ej. la Organization de la que
es administrador según BORME y el Domain de esa Organization).
"""
from typing import Dict, List

# Las propiedades se guardan aplanadas ("nodeProperties.<campo>") y el tipo en nodeType.
CYPHER = """
MATCH (i {sketch_id: $sketch_id})-[*1..2]-(n {sketch_id: $sketch_id})
WHERE i.nodeType = 'individual'
  AND (i.`nodeProperties.full_name` = $nombre OR i.nodeLabel = $nombre)
  AND n.nodeType IN ['organization', 'domain', 'website']
RETURN DISTINCT n.nodeType AS tipo, n.`nodeProperties.name` AS name, n.`nodeProperties.domain` AS domain,
       n.`nodeProperties.url` AS url, n.`nodeProperties.linkedin_url` AS linkedin_url
LIMIT 25
"""


def vecinos(graph_service, sketch_id: str, nombre: str) -> Dict[str, List[str]]:
    """{"empresas": [...], "dominios": [...], "linkedin": [...]} vecinos del Individual `nombre`."""
    out: Dict[str, List[str]] = {"empresas": [], "dominios": [], "linkedin": []}
    if not graph_service or not nombre:
        return out
    try:
        filas = graph_service.query(CYPHER, {"sketch_id": sketch_id, "nombre": nombre}) or []
    except Exception:
        return out
    for f in filas:
        if f.get("tipo") == "organization" and f.get("name"):
            out["empresas"].append(str(f["name"]))
            if f.get("linkedin_url"):
                out["linkedin"].append(f["linkedin_url"])
        elif f.get("tipo") == "domain" and f.get("domain"):
            out["dominios"].append(f["domain"])
        elif f.get("tipo") == "website" and f.get("url"):
            host = str(f["url"]).split("//", 1)[-1].split("/", 1)[0]
            out["dominios"].append(host.removeprefix("www."))
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def nombre_de(individual) -> str:
    return (individual.full_name or " ".join(
        x for x in (individual.first_name, individual.last_name) if x) or "").strip()
