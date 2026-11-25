"""Contexto de grafo para los enrichers de personas.

Un Individual llega solo con su nombre; la empresa y el dominio con los que hay
que cruzarlo están en sus vecinos del sketch (p. ej. la Organization de la que
es administrador según BORME y el Domain de esa Organization).
"""

from typing import Any, Dict, List

from flowsint_enrichers.es import _borme_comun as bc
from flowsint_enrichers.es._scripts import load

# Las propiedades se guardan aplanadas ("nodeProperties.<campo>") y el tipo en nodeType.
# Sin etiqueta de nodo: al fusionar nodos, la etiqueta la pone quien llama y puede no ser el nodeType.
# verificado: dominios con el NIF de la empresa en la portada o web inscrita en el BORME; van primero para
# que LIMIT no los deje fuera.
# dudosas: organizaciones con una relación viva POSIBLE_*/parcial con la persona, aunque además tengan otra normal.
CYPHER = """
MATCH (i {sketch_id: $sketch_id})
WHERE i.nodeType = 'individual' AND i.deleted_at IS NULL
  AND (i.`nodeProperties.full_name` = $nombre OR i.nodeLabel = $nombre)
OPTIONAL MATCH (i)-[d]-(o {sketch_id: $sketch_id})
WHERE o.nodeType = 'organization' AND d.deleted_at IS NULL
  AND (type(d) STARTS WITH 'POSIBLE_' OR type(d) = 'BORME_COINCIDENCIA_PARCIAL')
WITH collect(DISTINCT i) AS personas, collect(DISTINCT o) AS dudosas
UNWIND personas AS i
MATCH p = (i)-[*1..2]-(n {sketch_id: $sketch_id})
WHERE n.nodeType IN ['organization', 'domain', 'website']
  AND ALL(x IN nodes(p) WHERE x.deleted_at IS NULL AND NOT x IN dudosas)
  AND ALL(r IN relationships(p) WHERE r.deleted_at IS NULL
                                  AND NOT type(r) STARTS WITH 'POSIBLE_'
                                  AND type(r) <> 'BORME_COINCIDENCIA_PARCIAL')
WITH n, dudosas, min(length(p)) AS largo,
     max(CASE WHEN n.nodeType <> 'organization'
               AND any(x IN nodes(p) WHERE x.nodeType = 'organization'
                       AND (type(last(relationships(p))) = 'HAS_WEBSITE'
                            OR n.`nodeProperties.nif_en_portada` CONTAINS x.`nodeProperties.nif`))
              THEN 1 ELSE 0 END) AS verificado, collect(DISTINCT {
       relaciones: [r IN relationships(p) | type(r)],
       nodos: [x IN nodes(p) | {id: elementId(x), tipo: x.nodeType, name: x.`nodeProperties.name`,
                                nif: x.`nodeProperties.nif`, certeza: x.`nodeProperties.identidad_certeza`}],
       borrado: any(x IN nodes(p) WHERE x.deleted_at IS NOT NULL)
                OR any(r IN relationships(p) WHERE r.deleted_at IS NOT NULL)}) AS caminos
RETURN n.nodeType AS tipo, n.`nodeProperties.name` AS name, n.`nodeProperties.domain` AS domain,
       n.`nodeProperties.url` AS url, n.`nodeProperties.linkedin_url` AS linkedin_url,
       n.`nodeProperties.nif_en_portada` AS nif_en_portada, caminos,
       [o IN dudosas | {id: elementId(o), name: o.`nodeProperties.name`,
                        nif: o.`nodeProperties.nif`, certeza: o.`nodeProperties.identidad_certeza`}] AS dudosas
ORDER BY verificado DESC, largo, n.nodeType, n.nodeLabel, elementId(n)
LIMIT 25
"""


def _es_dudosa(org: Dict[str, Any], dudosas: List[Dict[str, Any]]) -> bool:
    """Por nodo o por denominación: «OTRA SL» y «Otra, S.L.» son nodos distintos de la misma empresa,
    salvo que los dos tengan NIF propio y sea distinto."""
    return any(
        org.get("id") == d.get("id")
        or bc.misma_empresa(org.get("name") or "", d.get("name") or "")
        and not _otro_nif(org, d)
        for d in dudosas
    )


def _otro_nif(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    # Un NIF deducido por nombre no distingue a dos homónimas
    nifs = {"" if n.get("certeza") else _nif(n) for n in (a, b)}
    return "" not in nifs and len(nifs) == 2


def _nif(nodo: Dict[str, Any]) -> str:
    return load("resolver_empresa").limpiar_nif(str(nodo.get("nif") or ""))


def _verificado(fila: Dict[str, Any], caminos: List[Dict[str, Any]]) -> bool:
    """Dominio con el NIF de la empresa en la portada, o web inscrita en el BORME."""
    portada = str(fila.get("nif_en_portada") or "")
    for c in caminos:
        orgs = [n for n in c.get("nodos") or [] if n.get("tipo") == "organization"]
        if orgs and c["relaciones"][-1] == "HAS_WEBSITE":
            return True
        if any(_nif(o) and _nif(o) in portada for o in orgs):
            return True
    return False


def _camino_fiable(camino: Dict[str, Any], dudosas: List[Dict[str, Any]]) -> bool:
    """Repite en Python el filtro de CYPHER; un camino sin datos para comprobarlo se descarta."""
    relaciones = camino.get("relaciones")
    if not relaciones or camino.get("borrado") is not False:
        return False
    if any(
        t.startswith("POSIBLE_") or t == "BORME_COINCIDENCIA_PARCIAL"
        for t in relaciones
    ):
        return False
    return not any(
        n.get("tipo") == "organization" and _es_dudosa(n, dudosas)
        for n in camino.get("nodos") or []
    )


def vecinos(graph_service, sketch_id: str, nombre: str) -> Dict[str, List[str]]:
    """{"empresas", "empresas_directas", "dominios", "linkedin"}: vecinos del Individual `nombre`.

    empresas_directas son las enlazadas sin nodos intermedios y por algo más que WORKS_AT.
    No sigue relaciones POSIBLE_* ni BORME_COINCIDENCIA_PARCIAL, ni pasa por empresas que tengan una con
    la persona: podrían llevar a la empresa o al dominio de un homónimo. Tampoco usa datos borrados.
    Los dominios verificados van primero. Los errores del grafo llegan a quien llama.
    """
    out: Dict[str, List[str]] = {
        "empresas": [],
        "empresas_directas": [],
        "dominios": [],
        "linkedin": [],
    }
    dominios: List[tuple] = []
    if not graph_service or not nombre:
        return out
    for f in (
        graph_service.query(CYPHER, {"sketch_id": sketch_id, "nombre": nombre}) or []
    ):
        dudosas = f.get("dudosas") or []
        caminos = [c for c in f.get("caminos") or [] if _camino_fiable(c, dudosas)]
        if not caminos:
            continue
        if f.get("tipo") == "organization" and f.get("name"):
            out["empresas"].append(str(f["name"]))
            if any(
                len(c["relaciones"]) == 1 and c["relaciones"] != ["WORKS_AT"]
                for c in caminos
            ):
                out["empresas_directas"].append(str(f["name"]))
            if f.get("linkedin_url"):
                out["linkedin"].append(f["linkedin_url"])
        elif f.get("tipo") == "domain" and f.get("domain"):
            dominios.append((not _verificado(f, caminos), f["domain"]))
        elif f.get("tipo") == "website" and f.get("url"):
            host = str(f["url"]).split("//", 1)[-1].split("/", 1)[0]
            dominios.append((not _verificado(f, caminos), host.removeprefix("www.")))
    # Los verificados, primero; entre iguales, el orden de la consulta
    out["dominios"] = [d for _, d in sorted(dominios, key=lambda x: x[0])]
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def nombre_de(individual) -> str:
    return (
        individual.full_name
        or " ".join(x for x in (individual.first_name, individual.last_name) if x)
        or ""
    ).strip()
