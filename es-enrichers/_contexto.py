"""Contexto de grafo para los enrichers de personas.

Un Individual llega solo con su nombre; la empresa y el dominio con los que hay
que cruzarlo están en sus vecinos del sketch (p. ej. la Organization de la que
es administrador según BORME y el Domain de esa Organization).
"""

from typing import Dict, List

# Las propiedades se guardan aplanadas ("nodeProperties.<campo>") y el tipo en nodeType.
CYPHER = """
MATCH p = (i {sketch_id: $sketch_id})-[*1..2]-(n {sketch_id: $sketch_id})
WHERE i.nodeType = 'individual'
  AND (i.`nodeProperties.full_name` = $nombre OR i.nodeLabel = $nombre)
  AND n.nodeType IN ['organization', 'domain', 'website']
  AND ALL(r IN relationships(p) WHERE NOT type(r) STARTS WITH 'POSIBLE_'
                                  AND type(r) <> 'BORME_COINCIDENCIA_PARCIAL')
WITH n, collect(DISTINCT [r IN relationships(p) | type(r)]) AS caminos
RETURN n.nodeType AS tipo, n.`nodeProperties.name` AS name, n.`nodeProperties.domain` AS domain,
       n.`nodeProperties.url` AS url, n.`nodeProperties.linkedin_url` AS linkedin_url, caminos
LIMIT 25
"""


def _camino_fiable(camino: List[str]) -> bool:
    return not any(
        t.startswith("POSIBLE_") or t == "BORME_COINCIDENCIA_PARCIAL" for t in camino
    )


def vecinos(graph_service, sketch_id: str, nombre: str) -> Dict[str, List[str]]:
    """{"empresas", "empresas_directas", "dominios", "linkedin"}: vecinos del Individual `nombre`.

    empresas_directas son las enlazadas sin nodos intermedios.
    No sigue relaciones POSIBLE_* ni BORME_COINCIDENCIA_PARCIAL: podrían llevar a la empresa o al dominio
    de un homónimo. Los errores del grafo llegan a quien llama.
    """
    out: Dict[str, List[str]] = {
        "empresas": [],
        "empresas_directas": [],
        "dominios": [],
        "linkedin": [],
    }
    if not graph_service or not nombre:
        return out
    for f in (
        graph_service.query(CYPHER, {"sketch_id": sketch_id, "nombre": nombre}) or []
    ):
        caminos = [c for c in f.get("caminos") or [] if _camino_fiable(c)]
        if not caminos:
            continue
        if f.get("tipo") == "organization" and f.get("name"):
            out["empresas"].append(str(f["name"]))
            if any(len(c) == 1 for c in caminos):
                out["empresas_directas"].append(str(f["name"]))
            if f.get("linkedin_url"):
                out["linkedin"].append(f["linkedin_url"])
        elif f.get("tipo") == "domain" and f.get("domain"):
            out["dominios"].append(f["domain"])
        elif f.get("tipo") == "website" and f.get("url"):
            host = str(f["url"]).split("//", 1)[-1].split("/", 1)[0]
            out["dominios"].append(host.removeprefix("www."))
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def nombre_de(individual) -> str:
    return (
        individual.full_name
        or " ".join(x for x in (individual.first_name, individual.last_name) if x)
        or ""
    ).strip()
