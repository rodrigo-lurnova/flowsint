"""Publica un informe HTML como Analysis de una investigación de Flowsint.

El editor de análisis (TipTap con StarterKit) no tiene tablas: cada fila se
convierte en un elemento de lista «celda 1: celda 2 · celda 3». Se conservan
títulos, párrafos, listas, enlaces, negritas y código. Si ya existe un análisis
con el mismo título en la investigación, se actualiza.

Uso:
  python flows/publicar_analisis.py informes/x.html --investigacion <uuid> --token <JWT>
"""
import argparse
import json
import urllib.request

from bs4 import BeautifulSoup


def a_tiptap(html_txt: str) -> tuple[str, str]:
    soup = BeautifulSoup(html_txt, "html.parser")
    titulo = soup.title.get_text(strip=True) if soup.title else "Informe"
    cuerpo = soup.select_one(".wrap") or soup.body or soup
    for t in cuerpo.select("style, script"):
        t.decompose()
    for t in cuerpo.select(".kicker, .meta"):
        t.name = "p"
    # Cifras clave (.kpi: <span>etiqueta</span><b>valor</b>) -> lista
    for grid in cuerpo.select(".grid"):
        ul = soup.new_tag("ul")
        for div in grid.select(".kpi"):
            li = soup.new_tag("li")
            b = soup.new_tag("strong")
            b.string = div.find("span").get_text(strip=True)
            li.append(b)
            li.append(f": {div.find('b').get_text(strip=True)}")
            ul.append(li)
        grid.replace_with(ul)
    for tabla in cuerpo.find_all("table"):
        ul = soup.new_tag("ul")
        filas = tabla.find_all("tr")
        cab = [th.get_text(" ", strip=True) for th in filas[0].find_all("th")] if filas and filas[0].find("th") else []
        for tr in filas:
            celdas = tr.find_all("td")
            if not celdas:
                continue
            li = soup.new_tag("li")
            b = soup.new_tag("strong")
            b.string = celdas[0].get_text(" ", strip=True)
            li.append(b)
            for i, td in enumerate(celdas[1:], 1):
                li.append(": " if i == 1 else " · ")
                if cab and len(cab) > 2 and i > 1:
                    li.append(f"{cab[i]}: ")
                for hijo in list(td.contents):
                    li.append(hijo)
            ul.append(li)
        (tabla.find_parent(class_="tbl") or tabla).replace_with(ul)
    for el in cuerpo.find_all(["div", "section", "header", "footer", "span", "time"]):
        el.unwrap()
    for el in cuerpo.find_all(True):
        el.attrs = {k: v for k, v in el.attrs.items() if k == "href"}
    return titulo, "".join(str(c) for c in cuerpo.contents).strip()


def api(base: str, token: str, path: str, body=None, method=None):
    req = urllib.request.Request(base + path, method=method or ("POST" if body is not None else "GET"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html")
    ap.add_argument("--investigacion", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--api", default="http://localhost:5001")
    ap.add_argument("--descripcion", default="")
    a = ap.parse_args()

    titulo, contenido = a_tiptap(open(a.html, encoding="utf-8").read())
    body = {"title": titulo, "description": a.descripcion, "content": contenido, "investigation_id": a.investigacion}
    # GET /api/analyses da 500 (DISTINCT sobre columna json en upstream): se lista por investigación.
    existentes = [x for x in api(a.api, a.token, f"/api/analyses/investigation/{a.investigacion}")
                  if x.get("title") == titulo]
    if existentes:
        r = api(a.api, a.token, f"/api/analyses/{existentes[0]['id']}", body, method="PUT")
        print(f"actualizado  {titulo}  {r['id']}")
    else:
        r = api(a.api, a.token, "/api/analyses/create", body)
        print(f"creado       {titulo}  {r['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
