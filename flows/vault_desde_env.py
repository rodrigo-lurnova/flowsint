"""Copia las claves de servicios de .env al vault de Flowsint del usuario.

Los enrichers resuelven cada parámetro vaultSecret buscando en el vault una
clave con el mismo nombre, así que aquí se crean con el nombre del parámetro.
Si la clave ya existe se reemplaza (borrar + crear). Nunca imprime valores.

Uso:
  python flows/vault_desde_env.py --token <JWT> [--env .env] [--api http://localhost:5001]
"""
import argparse
import json
import urllib.request

# nombre en el vault (= parámetro vaultSecret) -> variables de .env, por orden de preferencia
CLAVES = {
    "HUNTER_API_KEY": ["HUNTER_API_KEY"],
    "APIFY_TOKEN": ["APIFY_TOKEN"],
    "APISPAIN_API_KEY": ["APISPAIN_API_KEY", "SPAIN_API_KEY"],
    "BRAVE_API_KEY": ["BRAVE_API_KEY"],
    "PDCP_API_KEY": ["PDCP_API_KEY"],
    "HIBP_API_KEY": ["HIBP_API_KEY"],
    "DEHASHED_API_KEY": ["DEHASHED_API_KEY"],
    "WHOXY_API_KEY": ["WHOXY_API_KEY"],
    "WHOISXML_API_KEY": ["WHOISXML_API_KEY"],
    "ETHERSCAN_API_KEY": ["ETHERSCAN_API_KEY"],
}


def leer_env(ruta: str) -> dict:
    env = {}
    for linea in open(ruta, encoding="utf-8"):
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def api(base: str, token: str, path: str, body=None, method=None):
    req = urllib.request.Request(base + path, method=method or ("POST" if body is not None else "GET"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r) if r.status != 204 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--api", default="http://localhost:5001")
    a = ap.parse_args()

    env = leer_env(a.env)
    existentes = {k["name"]: k["id"] for k in api(a.api, a.token, "/api/keys")}
    for nombre, variables in CLAVES.items():
        var = next((v for v in variables if env.get(v)), None)
        if not var:
            continue
        if nombre in existentes:
            api(a.api, a.token, f"/api/keys/{existentes[nombre]}", method="DELETE")
        api(a.api, a.token, "/api/keys/create", {"name": nombre, "key": env[var]})
        print(f"{'reemplazada' if nombre in existentes else 'creada':11}  {nombre}  (desde {var})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
