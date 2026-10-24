"""Genera e importa los flows OSINT España en una instancia de Flowsint.

Uso:
  python flows/build_flows.py --token <JWT> [--api http://localhost:5001] [--solo-json]

Lee los enrichers registrados (/api/flows/raw_materials), construye los flows
definidos en FLOWS, guarda cada uno en flows/<slug>.json y los crea vía
POST /api/flows/create (si ya existe uno con el mismo nombre, lo actualiza).
"""
import argparse
import json
import pathlib
import re
import unicodedata
import urllib.request

FIELDS = ["id", "class_name", "module", "name", "category", "type", "inputs", "outputs", "documentation",
          "description", "required_params", "params", "params_schema", "icon"]

# Cada flow: (nombre, descripción, tipo de entrada, [(id, enricher, padre)]). Columna = profundidad.
FLOWS = [
    ("Ficha de empresa (España)",
     "Nombre de empresa → identidad (NIF/LEI/web), registro mercantil y administradores (BORME), "
     "subvenciones (BDNS), CNMV, edictos BOE, sentencias CENDOJ, contratos PLACSP/TED y sanciones UE.",
     "Organization",
     [("resolver", "es_org_resolver_identidad", None),
      ("borme_ficha", "es_org_to_registry_borme", "resolver"),
      ("borme_cargos", "es_org_to_officers_borme", "resolver"),
      ("bdns", "es_org_to_subvenciones_bdns", "resolver"),
      ("cnmv", "es_org_to_registros_cnmv", "resolver"),
      ("boe", "es_org_to_edictos_boe", "resolver"),
      ("cendoj", "es_org_to_sentencias_cendoj", "resolver"),
      ("placsp", "es_org_to_contratos_placsp", "resolver"),
      ("ted", "es_org_to_contratos_ted", "resolver"),
      ("sanciones", "es_org_to_sanciones_ue", "resolver")]),
    ("Empresa → personas y huella digital",
     "Administradores (BORME) y empleados (LinkedIn vía Apify) de la empresa → correos probables "
     "(+Gravatar) y perfil de LinkedIn. Correos públicos de la web. Apify es de pago y opt-in.",
     "Organization",
     [("resolver", "es_org_resolver_identidad", None),
      ("borme_cargos", "es_org_to_officers_borme", "resolver"),
      ("empleados", "es_org_to_linkedin_employees", "resolver"),
      ("correos_adm", "es_individual_to_emails", "borme_cargos"),
      ("linkedin_adm", "es_individual_to_linkedin", "borme_cargos"),
      ("correos_emp", "es_individual_to_emails", "empleados"),
      ("gravatar_adm", "email_to_gravatar", "correos_adm"),
      ("gravatar_emp", "email_to_gravatar", "correos_emp")]),
    ("Persona vinculada: huella profesional",
     "Persona (administrador/empleado) → otras empresas donde tiene cargo (BORME), correos corporativos "
     "probables (+Gravatar) y LinkedIn (Apify). Úsalo solo con personas vinculadas a la empresa investigada.",
     "Individual",
     [("empresas", "es_individual_to_companies_borme", None),
      ("correos", "es_individual_to_emails", None),
      ("linkedin", "es_individual_to_linkedin", None),
      ("gravatar", "email_to_gravatar", "correos")]),
    ("Persona vinculada: perfiles sociales (opcional)",
     "OPCIONAL. Persona vinculada a la empresa → perfiles públicos en LinkedIn, X, Instagram, Facebook, TikTok, "
     "YouTube, GitHub y Medium (búsquedas site: en SearXNG + API de GitHub). Sin empresa vinculada no busca. "
     "Los usuarios encontrados pueden encadenarse con maigret/sherlock.",
     "Individual",
     [("perfiles", "es_individual_to_socials", None)]),
]


def api(base: str, token: str, path: str, body=None, method=None):
    req = urllib.request.Request(base + path, method=method or ("POST" if body is not None else "GET"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def construir(mats: dict, entrada: str, pasos: list) -> dict:
    def nodo(nid, name, x, y):
        m = mats[name]
        data = {f: m.get(f) for f in FIELDS}
        data.update(module=data["module"] or "", key=name, color="#94a3b8", params=data["params"] or {})
        return {"id": nid, "type": "type" if m["type"] == "type" else "enricher",
                "position": {"x": x, "y": y}, "data": data}

    raiz = f"{entrada}-1"
    # El orquestador toma el enricher del id del nodo (id.split("-")[0]): id = "<enricher>-<n>".
    ids = {None: raiz, **{alias: f"{enr}-{i}" for i, (alias, enr, _) in enumerate(pasos, 1)}}
    nodes, prof, filas = [nodo(raiz, entrada, 0, 0)], {None: 0}, {}
    for alias, enr, padre in pasos:
        d = prof[padre] + 1
        prof[alias] = d
        filas[d] = filas.get(d, -1) + 1
        nodes.append(nodo(ids[alias], enr, 380 * d, 110 * filas[d]))
    por_id = {n["id"]: n for n in nodes}
    edges = []
    for alias, _, padre in pasos:
        s, t = por_id[ids[padre]], por_id[ids[alias]]
        nid = t["id"]
        edges.append({"id": f"{s['id']}-{nid}", "source": s["id"], "target": nid,
                      "sourceHandle": s["data"]["outputs"]["type"], "targetHandle": t["data"]["inputs"]["type"],
                      "style": {"stroke": "#64748b"},
                      "markerEnd": {"type": "arrowclosed", "width": 15, "height": 15, "color": "#64748b"}})
    return {"nodes": nodes, "edges": edges}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True)
    ap.add_argument("--api", default="http://localhost:5001")
    ap.add_argument("--solo-json", action="store_true", help="no crear los flows, solo escribir los JSON")
    a = ap.parse_args()

    raw = api(a.api, a.token, "/api/flows/raw_materials")["items"]
    mats = {m["name"]: m for v in raw.values() if isinstance(v, list) for m in v}
    existentes = {} if a.solo_json else {f["name"]: f["id"] for f in api(a.api, a.token, "/api/flows")}
    out_dir = pathlib.Path(__file__).parent
    for nombre, desc, entrada, pasos in FLOWS:
        body = {"name": nombre, "description": desc, "category": [entrada],
                "flow_schema": construir(mats, entrada, pasos)}
        ascii_ = unicodedata.normalize("NFKD", nombre.lower()).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
        (out_dir / f"{slug}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1))
        if a.solo_json:
            continue
        if nombre in existentes:
            api(a.api, a.token, f"/api/flows/{existentes[nombre]}", body, method="PUT")
            print(f"actualizado  {nombre}  {existentes[nombre]}")
        else:
            print(f"creado       {nombre}  {api(a.api, a.token, '/api/flows/create', body)['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
