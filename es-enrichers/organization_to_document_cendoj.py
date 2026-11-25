import time
from typing import Dict, List, Optional

import httpx

from flowsint_enrichers.es._org import (
    Hallazgo,
    OrgEsEnricher,
    documento,
    recorta,
)
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.registry import flowsint_enricher

TOPE_CENDOJ = 50  # aviso legal del CENDOJ: sin descarga masiva


@flowsint_enricher
class OrgToSentenciasCendoj(OrgEsEnricher):
    """[CENDOJ] Resoluciones judiciales que mencionan a la empresa (poderjudicial.es)."""

    FUENTE = "[CENDOJ]"
    MAX_DEFECTO = 10

    @classmethod
    def name(cls) -> str:
        return "es_org_to_sentencias_cendoj"

    @classmethod
    def documentation(cls) -> str:
        return f"""
        Busca la denominación (sin forma jurídica, entre comillas si tiene varias palabras) en
        el texto de la jurisprudencia del CENDOJ y crea un Document por resolución (ROJ, ECLI,
        fecha, ponente, resumen automático) con la relación MENCIONADA_EN_SENTENCIA: el
        buscador es de texto libre, así que una coincidencia indica mención, no que sea parte.
        Aviso legal del CENDOJ: prohibidos el uso comercial y la descarga masiva; este enricher
        hace consultas puntuales con pausa y un tope de {TOPE_CENDOJ} resultados.
        Script osint-es-scripts/cendoj.py.
        """

    def consultar(self, ids: Dict[str, Optional[str]]) -> List[Hallazgo]:
        cendoj = load("cendoj")
        base = self.base_o_fallo(ids)
        if not base:
            return []
        texto = f'"{base}"' if " " in base else base
        tope = min(self.max_resultados, TOPE_CENDOJ)
        with httpx.Client(
            headers=cendoj.HEADERS, timeout=40, follow_redirects=True
        ) as c:
            c.get(cendoj.BASE + "indexAN.jsp").raise_for_status()
            total, items = cendoj.pagina(c, texto, 1)
            while len(items) < min(tope, total):
                time.sleep(1.5)
                try:
                    _, more = cendoj.pagina(c, texto, len(items) + 1)
                except Exception as e:  # se conservan las páginas ya leídas
                    self.fallo(texto, e)
                    break
                if not more:
                    self.fallo(
                        texto,
                        f"página sin resultados legibles tras {len(items)} de {total}",
                    )
                    break
                items.extend(more)
        self.log(
            f"«{texto}»: {total} resoluciones en total (se toman {min(tope, len(items))})"
        )
        out = []
        for i in items[:tope]:
            doc = documento(
                f"CENDOJ {i.get('roj') or recorta(i['titulo'], 80)} ({i['fecha']})",
                doc_type="resolución judicial",
                source="CENDOJ",
                source_url=i["url"],
                created_date=i["fecha"],
                author=i.get("Órgano") or i.get("Municipio"),
                subject=i["titulo"],
                description=recorta(i.get("resumen"), 600),
                ecli=i.get("ecli"),
                roj=i.get("roj"),
                ponente=i.get("Ponente"),
                recurso=i.get("Nº Recurso"),
                consulta=texto,
            )
            out.append(Hallazgo(doc, "MENCIONADA_EN_SENTENCIA", certainty="posible"))
        return out


InputType = OrgToSentenciasCendoj.InputType
OutputType = OrgToSentenciasCendoj.OutputType
