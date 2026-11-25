import json
import re
from types import SimpleNamespace

import dns.exception
import dns.resolver
import httpx
import pytest
import requests

from flowsint_core.core import orchestrator as orq
from flowsint_core.core.types import FlowBranch, FlowStep
from flowsint_core.utils import to_json_serializable
from flowsint_enrichers.es import _borme_comun as bc
from flowsint_enrichers.es import _contexto
from flowsint_enrichers.es._org import Hallazgo, OrgEsEnricher, documento, org_ids
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.es.domain_to_dns_records import EsDomainToDnsRecordsEnricher
from flowsint_enrichers.es.domain_to_emails_web import EsDomainToWebEmailsEnricher
from flowsint_enrichers.es.individual_to_organizations_borme import (
    EsIndividualToOrgsBormeEnricher as BormePersona,
)
from flowsint_enrichers.es.ip_to_ports_internetdb import EsIpToPortsInternetdbEnricher
from flowsint_enrichers.es.org_to_asn import EsOrgToAsnEnricher
from flowsint_enrichers.es.organization_to_document_bdns import OrgToSubvencionesBdns
from flowsint_enrichers.es.organization_to_document_boe_edictos import OrgToEdictosBoe
from flowsint_enrichers.es.organization_to_document_cendoj import OrgToSentenciasCendoj
from flowsint_enrichers.es.organization_to_document_cnmv import OrgToRegistrosCnmv
from flowsint_enrichers.es.organization_to_document_placsp import OrgToContratosPlacsp
from flowsint_enrichers.es.organization_to_document_sanciones_ue import OrgToSancionesUe
from flowsint_enrichers.es.organization_to_document_ted import OrgToContratosTed
from flowsint_enrichers.es.organization_to_individuals_borme import (
    EsOrgToIndividualsBormeEnricher as BormeEmpresa,
)
from flowsint_enrichers.es.organization_to_organization_borme import (
    EsOrgToOrgBormeEnricher as BormeFicha,
)
from flowsint_enrichers.es.organization_to_organization_resolver import OrgToIdentidadEs
from flowsint_types.domain import Domain
from flowsint_types.individual import Individual
from flowsint_types.ip import Ip
from flowsint_types.organization import Organization

NIF = "B00000000"
OTRO_NIF = "B11111111"
ACME = Organization(name="ACME SL")
ACME_NIF = Organization(name="ACME SL", nif=NIF)
ACME_PROBABLE = Organization(name="ACME SL", nif=NIF, identidad_certeza="probable")


def falla(*a, **k):
    raise RuntimeError("fuente caída")


def certezas(e):
    return [f.certainty for f in e.findings]


@pytest.fixture
def http(monkeypatch):
    """http(responder): todo httpx.Client pasa por un MockTransport."""
    real = httpx.Client

    def _http(responder):
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **k: real(**{**k, "transport": httpx.MockTransport(responder)}),
        )

    monkeypatch.setattr("time.sleep", lambda s: None)
    return _http


# ------------------------------------------------------------------ base común


class Fuente(OrgEsEnricher):
    FUENTE = "[FUENTE]"
    MAX_DEFECTO = 2

    @classmethod
    def name(cls):
        return "prueba_fuente"

    def consultar(self, ids):
        if "CAIDA" in ids["nombre"]:
            falla()
        return [
            Hallazgo(
                documento(f"Doc {i}", source_url=f"https://example.com/{i}"),
                "MENCIONADA",
            )
            for i in range(3)
        ]


def test_hallazgo_defaults():
    h = Hallazgo(documento("Doc"), "REL")
    assert (h.url, h.certainty, h.extra) == (None, "confirmada", [])


def test_source_error_marks_the_run_failed_and_continues(correr):
    e = correr(Fuente, [Organization(name="CAIDA SL"), ACME_NIF])
    run = e.source_run()
    assert run.status == "failed" and run.findings == 2
    assert e.failures == ["FUENTE: CAIDA SL: RuntimeError: fuente caída"]


def test_one_finding_per_kept_result_with_url(correr):
    e = correr(Fuente, [ACME_NIF])
    assert [(f.subject, f.relation, f.object, f.source, f.url) for f in e.findings] == [
        ("ACME SL", "MENCIONADA", "Doc 0", "FUENTE", "https://example.com/0"),
        ("ACME SL", "MENCIONADA", "Doc 1", "FUENTE", "https://example.com/1"),
    ]
    assert certezas(e) == ["confirmada"] * 2 and e.source_run().status == "ok"


def test_findings_survive_a_graph_write_error(correr, grafo):
    grafo.create_node_from_flowsint_type = falla
    e = correr(Fuente, [ACME_NIF])
    assert len(e.findings) == 2 and "fuente caída" in e.failures[0]


@pytest.mark.parametrize(
    "org, certeza",
    [
        (ACME, "posible"),
        (ACME_NIF, "confirmada"),
        (ACME_PROBABLE, "probable"),
        (Organization(name="ACME SL", lei="ABCDEFGHIJKLMNOPQR12"), "confirmada"),
    ],
)
def test_certainty_is_capped_by_identity(correr, org, certeza):
    assert set(certezas(correr(Fuente, [org]))) == {certeza}


def test_skipped_org_is_a_failure(correr):
    e = correr(Fuente, [Organization(name="SL")])
    assert e.failures == ["FUENTE: SL: sin NIF ni denominación utilizable"]


def test_org_ids_exposes_identity_certainty():
    assert org_ids(ACME_PROBABLE)["identidad_certeza"] == "probable"
    assert org_ids(ACME_NIF)["identidad_certeza"] is None


# ------------------------------------------------------------------ documentos


@pytest.fixture
def fuentes(monkeypatch):
    """Cada script devuelve un resultado; los tests cambian lo que necesiten."""
    bdns = load("bdns")
    monkeypatch.setattr(
        bdns,
        "buscar_terceros",
        lambda c, t: [{"id": 1, "descripcion": f"{NIF} - ACME SL"}],
    )
    monkeypatch.setattr(
        bdns,
        "concesiones",
        lambda c, i, n: [{"codConcesion": "X1", "urlBR": "https://example.com/bdns"}],
    )
    monkeypatch.setattr(
        load("boe_edictos"),
        "buscar",
        lambda c, t, judicial, *a: {
            "fuente": "TEJU" if judicial else "TEU",
            "resultados": [
                {
                    "referencia": f"BOE-N-{int(judicial)}",
                    "asunto": "asunto",
                    "pdf": "https://example.com/boe",
                    "organismo": "Organismo",
                    "boletin": "BOE 1",
                }
            ],
        },
    )
    cnmv = load("cnmv")
    monkeypatch.setattr(
        cnmv, "buscar", lambda c, t: [{"nif": "B-00000000", "nombre": "ACME SL"}]
    )
    monkeypatch.setattr(
        cnmv,
        "ficha",
        lambda c, nif: {
            "nombre": "ACME SL",
            "url": "https://example.com/ficha",
            "secciones": [{"seccion": "Cuentas", "url": "https://example.com/cnmv"}],
        },
    )
    monkeypatch.setattr(
        load("placsp"),
        "buscar",
        lambda c, t, n: {
            "total": 1,
            "resultados": [
                {
                    "expediente": "E1",
                    "objeto": "objeto",
                    "importe": 1,
                    "url": "https://example.com/placsp",
                    "organo": "Órgano",
                    "tipo": "t",
                    "estado": "e",
                }
            ],
            "errores": [],
        },
    )
    monkeypatch.setattr(
        load("ted"),
        "buscar",
        lambda c, t, n, es: {
            "consulta": t,
            "total": 1,
            "resultados": [
                {
                    "publicacion": "1-2026",
                    "titulo": "titulo",
                    "url": "https://example.com/ted",
                    "fecha": "2026-01-01",
                    "comprador": "Comprador",
                    "adjudicatarios": ["ACME SL"],
                    "identificadores": [f"ES{NIF}"],
                    "importe": 1,
                    "moneda": "EUR",
                    "cpv": [],
                    "pdf": None,
                }
            ],
        },
    )
    monkeypatch.setattr(
        load("sanciones_ue"), "buscar", lambda c, t, n: sancion(t == NIF)
    )
    monkeypatch.setattr(
        load("cendoj"),
        "pagina",
        lambda c, t, s: (
            1,
            [
                {
                    "titulo": "Sentencia",
                    "roj": "STS 1",
                    "fecha": "2026-01-01",
                    "url": "https://example.com/cendoj",
                }
            ],
        ),
    )


def sancion(por_id, coincidencia="exacta"):
    e = {
        "referencia_ue": "EU.1",
        "coincidencia": "identificador" if por_id else coincidencia,
        "nombres": ["ACME SL"],
        "reglamentos": [{"url": "https://example.com/ue"}],
        "fecha_designacion": None,
        "programas": [],
        "tipo": "enterprise",
        "direcciones": [],
        "identificadores": [],
    }
    return {"resultados": [e]}


POR_NIF = [
    (OrgToSubvencionesBdns, "BDNS", "bdns"),
    (OrgToEdictosBoe, "BOE", "boe"),
    (OrgToRegistrosCnmv, "CNMV", "cnmv"),
    (OrgToContratosTed, "TED", "ted"),
    (OrgToSancionesUe, "Sanciones UE", "ue"),
]
POR_NOMBRE = [
    (OrgToContratosPlacsp, "PLACSP", "placsp"),
    (OrgToSentenciasCendoj, "CENDOJ", "cendoj"),
]


@pytest.mark.parametrize("clase, source, ruta", POR_NIF)
@pytest.mark.parametrize(
    "org, certeza",
    [(ACME_NIF, "confirmada"), (ACME_PROBABLE, "probable"), (ACME, "posible")],
)
def test_documents_matched_by_nif(correr, fuentes, clase, source, ruta, org, certeza):
    e = correr(clase, [org])
    assert e.failures == []
    assert set(certezas(e)) == {certeza}
    assert {(f.source, f.url) for f in e.findings} == {
        (source, f"https://example.com/{ruta}")
    }


@pytest.mark.parametrize("clase, source, ruta", POR_NOMBRE)
def test_documents_matched_by_name_are_possible(
    correr, fuentes, http, clase, source, ruta
):
    http(lambda req: httpx.Response(200))
    e = correr(clase, [ACME_NIF])
    assert e.failures == []
    assert [(f.certainty, f.source, f.url) for f in e.findings] == [
        ("posible", source, f"https://example.com/{ruta}")
    ]


def test_result_whose_identifier_is_another_nif_is_possible(
    correr, fuentes, monkeypatch
):
    monkeypatch.setattr(
        load("bdns"),
        "buscar_terceros",
        lambda c, t: [{"id": 1, "descripcion": f"{OTRO_NIF} - OTRA SL"}],
    )
    assert certezas(correr(OrgToSubvencionesBdns, [ACME_NIF])) == ["posible"]


@pytest.mark.parametrize("org", [ACME, ACME_NIF])
@pytest.mark.parametrize("coincidencia", ["exacta", "contiene"])
def test_sanctions_by_name_are_always_possible(correr, monkeypatch, org, coincidencia):
    monkeypatch.setattr(
        load("sanciones_ue"),
        "buscar",
        lambda c, t, n: (
            {"resultados": []} if t == NIF else sancion(False, coincidencia)
        ),
    )
    e = correr(OrgToSancionesUe, [org], incluir_parciales="true")
    assert certezas(e) == ["posible"]


def test_sanction_identifier_found_through_the_name_is_possible(correr, monkeypatch):
    monkeypatch.setattr(
        load("sanciones_ue"),
        "buscar",
        lambda c, t, n: {"resultados": []} if t == NIF else sancion(True),
    )
    assert certezas(correr(OrgToSancionesUe, [ACME_NIF])) == ["posible"]


def test_one_failed_query_keeps_the_other_results(correr, fuentes, monkeypatch):
    boe = load("boe_edictos")
    buena = boe.buscar
    monkeypatch.setattr(
        boe, "buscar", lambda c, t, j, *a: buena(c, t, j) if j else falla()
    )
    e = correr(OrgToEdictosBoe, [ACME_NIF])
    assert len(e.findings) == 1
    assert e.failures == [f"BOE: {NIF} (TEU): RuntimeError: fuente caída"]

    san = load("sanciones_ue")
    monkeypatch.setattr(
        san, "buscar", lambda c, t, n: falla() if t == NIF else sancion(False)
    )
    e = correr(OrgToSancionesUe, [ACME_NIF])
    assert certezas(e) == ["posible"]
    assert e.failures == [f"Sanciones UE: {NIF}: RuntimeError: fuente caída"]

    monkeypatch.setattr(
        load("bdns"),
        "buscar_terceros",
        lambda c, t: [
            {"id": 1, "descripcion": f"{NIF} - ACME SL"},
            {"id": 2, "descripcion": f"{NIF} - ACME S.L."},
        ],
    )
    monkeypatch.setattr(
        load("bdns"),
        "concesiones",
        lambda c, i, n: falla() if i == 1 else [{"codConcesion": "X2"}],
    )
    e = correr(OrgToSubvencionesBdns, [ACME_NIF])
    assert len(e.findings) == 1 and len(e.failures) == 1
    assert e.findings[0].url is None  # sin bases ni nº de convocatoria no hay enlace

    cnmv = load("cnmv")
    monkeypatch.setattr(
        cnmv,
        "buscar",
        lambda c, t: [{"nif": x, "nombre": "ACME SL"} for x in ("B-1", "B-2")],
    )
    ficha = cnmv.ficha
    monkeypatch.setattr(
        cnmv, "ficha", lambda c, nif: falla() if nif == "B-1" else ficha(c, nif)
    )
    e = correr(OrgToRegistrosCnmv, [ACME])
    assert len(e.findings) == 1 and len(e.failures) == 1


@pytest.mark.parametrize("clase, source, _", POR_NOMBRE)
def test_name_only_sources_without_name_are_failures(
    correr, monkeypatch, clase, source, _
):
    org = Organization(name=NIF)
    monkeypatch.setattr(load("resolver_empresa"), "desde_bdns", lambda c, t: [])
    e = correr(clase, [org])
    assert len(e.failures) == 1 and e.failures[0].startswith(f"{source}: {NIF}: ")
    monkeypatch.setattr(load("resolver_empresa"), "desde_bdns", falla)
    e = correr(clase, [org])
    assert e.failures == [f"BDNS: {NIF}: RuntimeError: fuente caída"]


def test_cendoj_reports_unreadable_pages_and_keeps_results(
    correr, fuentes, monkeypatch, http
):
    http(lambda req: httpx.Response(200))
    cendoj = load("cendoj")
    primera = cendoj.pagina(None, "", 1)[1]

    def paginas(segunda):
        return lambda c, t, start: (20, primera) if start == 1 else segunda()

    for segunda in (lambda: (20, []), falla):
        monkeypatch.setattr(cendoj, "pagina", paginas(segunda))
        e = correr(OrgToSentenciasCendoj, [ACME])
        assert len(e.findings) == 1 and len(e.failures) == 1
        assert e.failures[0].startswith("CENDOJ: ")
    http(lambda req: httpx.Response(503))
    e = correr(OrgToSentenciasCendoj, [ACME])
    assert e.findings == [] and "503" in e.failures[0]


def test_placsp_reported_errors_reach_failures(correr, fuentes, monkeypatch):
    placsp = load("placsp")
    r = placsp.buscar(None, "", 1)
    monkeypatch.setattr(
        placsp, "buscar", lambda c, t, n: {**r, "errores": ["página 2: HTTP 500"]}
    )
    e = correr(OrgToContratosPlacsp, [ACME])
    assert len(e.findings) == 1 and e.failures == ["PLACSP: ACME: página 2: HTTP 500"]


def placsp_web(segunda, inicio=200):
    """PLACSP falso: portada, formulario, 1ª página (1 de 2 filas) y la respuesta `segunda`."""
    form = (
        '<form id="v:form1" action="/x"><input type="hidden" name="a" value="b"></form>'
    )
    fila = (
        "<tr><td><div>obj</div></td><td></td><td></td><td></td><td></td><td></td></tr>"
    )
    tabla = f'<table id="myTablaBusquedaCustom"><tbody>{fila}</tbody></table>'
    boton = '<input type="submit" name="sig" value="Siguiente &gt;&gt;">'
    respuestas = [
        httpx.Response(inicio, text=form),
        httpx.Response(200, text=form),
        httpx.Response(200, text=form + tabla + "Total: 2" + boton),
        segunda,
    ]
    return lambda req: respuestas.pop(0)


def test_placsp_script_reports_lost_pages(http):
    placsp = load("placsp")
    for segunda in (httpx.Response(500), httpx.Response(200, text="<html>")):
        http(placsp_web(segunda))
        with httpx.Client() as c:
            r = placsp.buscar(c, "ACME", 5)
        assert len(r["resultados"]) == 1 and len(r["errores"]) == 1
    http(placsp_web(None, inicio=503))
    with httpx.Client() as c, pytest.raises(httpx.HTTPStatusError):
        placsp.buscar(c, "ACME", 5)


def test_cnmv_script_raises_on_http_error(http):
    http(lambda req: httpx.Response(503))
    with httpx.Client() as c, pytest.raises(httpx.HTTPStatusError):
        load("cnmv").buscar(c, "ACME")


def test_sanctions_list_without_entities_is_an_error(tmp_path, monkeypatch):
    san = load("sanciones_ue")
    xml = tmp_path / "lista.xml"
    xml.write_text("<html></html>")
    monkeypatch.setattr(san, "descargar", lambda c: str(xml))
    with pytest.raises(RuntimeError, match="sin entidades"):
        san.buscar(None, "ACME")


# ------------------------------------------------------------------ resolver


def cand(nif=NIF, nombre="ACME SL", coincidencia="exacta", **kw):
    return {
        "nif": nif,
        "valido": True,
        "tipo": "persona jurídica (CIF)",
        "detalle": None,
        "denominaciones": [nombre],
        "fuentes": ["BDNS", "TED"],
        "lei": None,
        "gleif": None,
        "id_bdns": [],
        "normalizada": nombre,
        "coincidencia": coincidencia,
        **kw,
    }


@pytest.fixture
def resolver(monkeypatch, correr):
    def _resolver(*cands, org=ACME, webs=(), avisos=(), errores=(), **params):
        monkeypatch.setattr(
            load("resolver_empresa"),
            "resolver",
            lambda *a, **k: {
                "candidatos": list(cands),
                "homonimia": len(cands) > 1,
                "avisos": list(avisos),
                "errores": list(errores),
                "webs": list(webs),
            },
        )
        return correr(OrgToIdentidadEs, [org], **params)

    return _resolver


def resumen(e):
    return [(f.relation, f.object, f.certainty) for f in e.findings]


def test_single_exact_candidate_by_name_is_probable(resolver):
    e = resolver(cand())
    assert resumen(e) == [("NIF", NIF, "probable")]
    assert e.findings[0].source == "BDNS, TED"
    org = e.resultados[0]
    assert (org.nif, org.identidad_certeza) == (NIF, "probable")


def test_node_with_nif_is_confirmed_without_cap(resolver):
    e = resolver(cand(), org=ACME_NIF)
    assert resumen(e) == [("NIF", NIF, "confirmada")]
    assert org_ids(e.resultados[0])["identidad_certeza"] is None


def test_rerunning_the_resolver_does_not_promote_a_probable_nif(resolver):
    e = resolver(cand(), org=resolver(cand()).resultados[0])
    assert resumen(e) == [("NIF", NIF, "probable")]
    assert e.resultados[0].identidad_certeza == "probable"


def test_several_candidates_are_possible_and_assign_no_nif(resolver):
    e = resolver(cand(), cand(OTRO_NIF))
    assert [(r, c) for r, _, c in resumen(e)] == [("POSIBLE_IDENTIDAD", "posible")] * 2
    assert len({f.object for f in e.findings}) == 2
    assert org_ids(e.resultados[0])["nif"] is None


def test_candidate_findings_respect_max_resultados(resolver):
    assert len(resolver(cand(), cand(OTRO_NIF), max_resultados="1").findings) == 1


def test_candidate_with_another_legal_form_is_not_chosen(resolver):
    e = resolver(cand("A00000000", "ACME SA"))
    assert [(r, c) for r, _, c in resumen(e)] == [("POSIBLE_IDENTIDAD", "posible")]
    assert org_ids(e.resultados[0])["nif"] is None


def test_lei_certainty_depends_on_how_it_matched(resolver):
    lei = "ABCDEFGHIJKLMNOPQR12"
    g = {"nombre": "ACME SL", "url": "https://example.com/lei", "lei": lei}
    por_nombre = cand(lei=lei, gleif=g)
    por_nif = cand(lei=lei, gleif={**g, "por_nif": True})
    solo_lei = cand(None, lei=lei, gleif=g)
    assert ("LEI", lei, "posible") in resumen(resolver(por_nombre, org=ACME_NIF))
    assert ("LEI", lei, "confirmada") in resumen(resolver(por_nif, org=ACME_NIF))
    assert ("LEI", lei, "probable") in resumen(resolver(solo_lei))
    f = resolver(solo_lei).findings[0]
    assert (f.url, f.source) == ("https://example.com/lei", "GLEIF")


def test_probable_web_domains(resolver):
    def web(dominio, confianza, nifs=()):
        return {
            "dominio": dominio,
            "confianza": confianza,
            "nif_en_portada": list(nifs),
            "url": f"https://{dominio}/",
        }

    webs = [
        web("a.example.com", "alta", [NIF]),
        web("b.example.com", "media"),
        web("c.example.com", "baja"),
        web("d.example.com", "alta", [OTRO_NIF]),
    ]
    e = resolver(cand(), org=ACME_NIF, webs=webs)
    assert resumen(e)[1:] == [
        ("WEB_PROBABLE", "a.example.com", "probable"),
        ("WEB_PROBABLE", "b.example.com", "posible"),
        ("WEB_PROBABLE", "d.example.com", "posible"),  # NIF de otra empresa
    ]
    assert e.findings[1].url == "https://a.example.com/"


def test_resolver_source_errors_reach_failures_but_notices_do_not(resolver):
    e = resolver(
        cand(),
        avisos=["HOMONIMIA: dos entidades", "gleif: ConnectError: x"],
        errores=[{"fuente": "GLEIF", "error": "ConnectError: x"}],
    )
    assert e.failures == ["GLEIF: ACME SL: ConnectError: x"]
    assert e.source_run().findings == 1
    assert resolver(cand(), avisos=["HOMONIMIA: dos entidades"]).failures == []


def test_resolver_exception_and_empty_name_are_failures(correr, monkeypatch):
    monkeypatch.setattr(load("resolver_empresa"), "resolver", falla)
    e = correr(OrgToIdentidadEs, [ACME, Organization(name="")])
    assert len(e.failures) == 2 and "fuente caída" in e.failures[0]
    assert "denominación" in e.failures[1]


def test_resolver_script_separates_errors_from_notices(monkeypatch):
    res = load("resolver_empresa")
    monkeypatch.setattr(res, "desde_bdns", falla)
    monkeypatch.setattr(
        res,
        "desde_ted",
        lambda *a, **k: [{"fuente": "TED", "nif": NIF, "nombre": "OTRA SL"}],
    )
    r = res.resolver("ACME SL", ("bdns", "ted"), False, client=httpx.Client())
    assert r["errores"] == [{"fuente": "BDNS", "error": "RuntimeError: fuente caída"}]
    assert "bdns: RuntimeError: fuente caída" in r["avisos"]  # la CLI no cambia
    assert len(r["avisos"]) > 1


def test_resolver_script_reports_dns_down_but_not_missing_domains(monkeypatch):
    import socket

    res = load("resolver_empresa")
    fallo_dns = [socket.EAI_AGAIN]

    def resuelve(*a):
        raise socket.gaierror(fallo_dns[0], "x")

    monkeypatch.setattr(socket, "getaddrinfo", resuelve)
    r = res.resolver("ACME SL", (), True, client=httpx.Client())
    assert [x["fuente"] for x in r["errores"]] == ["DNS"]
    fallo_dns[0] = socket.EAI_NONAME
    r = res.resolver("ACME SL", (), True, client=httpx.Client())
    assert r["errores"] == []


def test_probable_identity_survives_json(resolver, fuentes, correr):
    salida = resolver(cand()).resultados
    entrada = json.loads(json.dumps(to_json_serializable(salida)))
    assert certezas(correr(OrgToContratosTed, entrada)) == ["probable"]


def test_probable_identity_survives_the_orchestrator(
    resolver, fuentes, monkeypatch, tmp_path, grafo
):
    resolver(cand())  # deja el script simulado
    monkeypatch.chdir(tmp_path)
    clases = {"es_resolver": OrgToIdentidadEs, "es_ted": OrgToContratosTed}

    class Registro:
        def enricher_exists(self, nombre):
            return nombre in clases

        def get_enricher(self, nombre, sketch_id, scan_id, vault=None, params=None):
            return clases[nombre](
                sketch_id=sketch_id, scan_id=scan_id, graph_service=grafo, params=params
            )

    monkeypatch.setattr(orq, "ENRICHER_REGISTRY", Registro())
    monkeypatch.setattr(
        orq,
        "Logger",
        SimpleNamespace(
            **{n: (lambda *a: None) for n in ("info", "error", "pending", "completed")}
        ),
    )
    monkeypatch.setattr(
        "flowsint_core.core.enricher_base.create_graph_service", lambda **k: grafo
    )
    pasos = [
        FlowStep(
            nodeId=f"{n}-1",
            type="enricher",
            inputs={},
            outputs={},
            status="pending",
            branchId="b",
            depth=0,
        )
        for n in clases
    ]
    flujo = orq.FlowOrchestrator("s", "t", [FlowBranch(id="b", name="b", steps=pasos)])
    r = flujo.scan([{"name": "ACME SL"}])
    assert [(f["relation"], f["certainty"]) for f in r["findings"]] == [
        ("NIF", "probable"),
        ("ADJUDICATARIA", "probable"),
    ]


# ------------------------------------------------------------------ BORME


def emp_borme(nombre="ACME SL", nif=None, hoja="M 1"):
    ficha = {
        "denominacion": nombre,
        "forma_juridica": "SL",
        "domicilio": "Calle Falsa 1",
        "nif": nif,
        "hoja_registral": hoja,
        "estado": "activa",
    }
    acto = {
        "fecha": "2026-01-02",
        "num": 1,
        "tipos": [],
        "borme_id": "BORME-A-2026-1-28",
        "url_pdf": "https://example.com/borme.pdf",
        "cargos": [],
    }
    return {"empresa": nombre, "ficha": ficha, "actos": [acto]}


def res_boe(*empresas, desde="2026-01-01", hasta="2026-03-31"):
    return {
        "backend": "boe",
        "desde": desde,
        "hasta": hasta,
        "coste": {},
        "empresas": list(empresas),
    }


@pytest.fixture
def borme(monkeypatch, correr):
    """borme(empresas…, apispain=f): BOE simulado y apispain que falla o responde con f."""
    mod = load("borme")
    llamadas = []

    def _borme(*empresas, org=ACME, clase=BormeFicha, apispain=falla, boe=None, **p):
        def buscar(*a):
            llamadas.append("boe")
            return boe(*a) if boe else res_boe(*empresas)

        def api(nombre, exacta, clave):
            llamadas.append("apispain")
            return apispain(nombre, exacta, clave)

        llamadas.clear()
        monkeypatch.setattr(mod, "buscar_empresa", buscar)
        monkeypatch.setattr(mod, "buscar_empresa_apispain", api)
        e = correr(clase, org if isinstance(org, list) else [org], **p)
        e.llamadas = list(llamadas)
        return e

    return _borme


def test_single_exact_company_updates_the_node(borme, grafo):
    e = borme(emp_borme())
    f = e.findings[0]
    assert (f.subject, f.object, f.certainty, f.source, f.url) == (
        "ACME SL",
        "ACME SL",
        "confirmada",
        "BORME",
        "https://example.com/borme.pdf",
    )
    assert [n.nodeLabel for n in grafo.nodos] == ["ACME SL"]
    assert grafo.nodos[0].siege_adresse == "Calle Falsa 1"


@pytest.mark.parametrize(
    "empresas",
    [
        [emp_borme(hoja="M 1"), emp_borme(hoja="M 2")],
        [emp_borme("ACME SA")],
        [emp_borme("ACME TRADING SL")],
        [emp_borme(hoja=None), emp_borme(hoja=None)],
    ],
)
def test_uncertain_companies_do_not_update_the_node(borme, grafo, empresas):
    e = borme(*empresas)
    assert certezas(e) == ["posible"] * len(empresas)
    for n in grafo.nodos:
        assert n.nodeLabel != "ACME SL" or n.siege_adresse is None
    etiquetas = {n.nodeLabel for n in grafo.nodos} - {"ACME SL"}
    assert len(etiquetas) == len(empresas)
    assert {r[1] for r in grafo.relaciones} == {"BORME_COINCIDENCIA_PARCIAL"}


def test_uncertain_company_does_not_update_another_input_node(borme, grafo):
    grupo = Organization(name="ACME SL")
    borme(
        emp_borme("ACME SL"),
        emp_borme("ACME SA"),
        org=[Organization(name="ACME"), grupo],
    )
    assert all(n.siege_adresse is None for n in grafo.nodos if n.nodeLabel == "ACME SL")


def test_company_with_a_different_nif_is_possible(borme, grafo):
    e = borme(emp_borme(nif=OTRO_NIF), org=ACME_NIF)
    assert certezas(e) == ["posible"]
    assert all(
        getattr(n, "nif", None) != OTRO_NIF or n.nodeLabel != "ACME SL"
        for n in grafo.nodos
    )
    assert (
        org_ids(next(n for n in grafo.nodos if n.nodeLabel == "ACME SL"))["nif"] == NIF
    )


def test_nif_obtained_by_name_from_borme_is_probable(borme):
    e = borme(emp_borme(nif=NIF))
    assert ("NIF", NIF, "probable") in resumen(e)
    assert org_ids(e.resultados[0])["identidad_certeza"] == "probable"
    e = borme(emp_borme(nif=NIF), org=ACME_NIF)
    assert ("NIF", NIF, "confirmada") in resumen(e)
    assert org_ids(e.resultados[0])["identidad_certeza"] is None


def test_borme_website_finding(borme):
    emp = emp_borme()
    emp["ficha"]["pagina_web"] = "www.acme.example.com"
    e = borme(emp)
    assert e.findings[-1].relation == "HAS_WEBSITE"
    assert e.findings[-1].certainty == "confirmada"


def test_borme_ficha_source_error(borme):
    e = borme(boe=falla)
    assert e.failures == ["BORME: ACME SL: RuntimeError: fuente caída"]
    assert e.coverage is None


@pytest.mark.parametrize("clase", [BormeFicha, BormeEmpresa])
def test_no_key_uses_boe_without_failure(borme, clase):
    e = borme(emp_borme(), clase=clase)
    assert e.llamadas == ["boe"] and e.failures == []
    assert e.source_run().coverage == "BORME BOE 2026-01-01 a 2026-03-31"


@pytest.mark.parametrize("clase", [BormeFicha, BormeEmpresa])
def test_apispain_coverage(borme, clase):
    vacio = {"backend": "apispain", "coste": {}, "empresas": []}
    e = borme(clase=clase, apispain=lambda *a: vacio, APISPAIN_API_KEY="clave-secreta")
    assert e.llamadas == ["apispain"] and e.failures == []  # vacío no es un error
    assert e.coverage == "BORME apispain desde 2024-01-01"


@pytest.mark.parametrize("clase", [BormeFicha, BormeEmpresa])
def test_apispain_error_falls_back_to_boe(borme, clase):
    def api(*a):
        raise RuntimeError("401 clave-secreta no válida")

    e = borme(emp_borme(), clase=clase, apispain=api, APISPAIN_API_KEY="clave-secreta")
    assert e.llamadas == ["apispain", "boe"]
    assert e.failures == ["BORME apispain: ACME SL: RuntimeError: 401 *** no válida"]
    assert e.coverage.startswith("BORME BOE")


def test_environment_key_is_masked(borme, monkeypatch, entorno):
    monkeypatch.setenv("APISPAIN_API_KEY", "clave-entorno")

    def api(nombre, exacta, clave):
        raise RuntimeError(clave)

    e = borme(apispain=api, boe=api_boe_con_clave)
    assert len(e.failures) == 2
    assert "clave-entorno" not in str(e.failures) + str(entorno)


def api_boe_con_clave(*a):
    raise RuntimeError("proxy clave-entorno")


@pytest.mark.parametrize("clase", [BormeFicha, BormeEmpresa])
def test_both_backends_failing_are_both_recorded(borme, clase):
    e = borme(clase=clase, boe=falla, APISPAIN_API_KEY="k1234")
    assert [f.split(":")[0] for f in e.failures] == ["BORME apispain", "BORME"]
    assert e.coverage is None


def test_explicit_apispain_does_not_fall_back(borme):
    e = borme(BACKEND="apispain", APISPAIN_API_KEY="k1234")
    assert e.llamadas == ["apispain"] and len(e.failures) == 1
    assert e.failures[0].startswith("BORME apispain: ")


def test_explicit_boe_ignores_the_key(borme):
    assert borme(BACKEND="boe", APISPAIN_API_KEY="k1234").llamadas == ["boe"]


@pytest.mark.parametrize(
    "respuesta, cae",
    [
        (httpx.Response(401), True),
        (httpx.Response(403, text="no"), True),
        (httpx.Response(429), True),
        (httpx.Response(500), True),
        (httpx.ConnectError("sin red"), True),
        (httpx.ReadTimeout("lento"), True),
        (httpx.Response(200, text="<html>"), True),
        (httpx.Response(200, json={"data": []}), False),
        (httpx.Response(200, json={"data": None}), False),
        (httpx.Response(404), False),  # apispain.get devuelve data None: sin resultados
    ],
)
def test_real_apispain_client_errors(borme, http, respuesta, cae):
    real = load("borme").buscar_empresa_apispain

    def responder(req):
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    http(responder)
    e = borme(emp_borme(), apispain=real, APISPAIN_API_KEY="k1234")
    assert e.llamadas == (["apispain", "boe"] if cae else ["apispain"])
    assert bool(e.failures) is cae
    assert e.coverage.startswith("BORME BOE" if cae else "BORME apispain")


@pytest.fixture
def boe_sin_red(monkeypatch, tmp_path):
    mod = load("borme")
    monkeypatch.setattr(mod, "DEFAULT_DB", str(tmp_path / "borme.sqlite"))
    rangos = []
    monkeypatch.setattr(
        mod, "cubrir", lambda db, dl, d, h, *a, **k: rangos.append((d, h)) or {}
    )
    return rangos


@pytest.mark.parametrize(
    "clase, entrada",
    [
        (BormeFicha, ACME),
        (BormeEmpresa, ACME),
        (BormePersona, Individual(full_name="Ana Ejemplo Ficticia")),
    ],
)
def test_coverage_is_the_range_actually_queried(correr, boe_sin_red, clase, entrada):
    for params, texto in (
        ({"HASTA": "31/01/2026", "DIAS": "30"}, "BORME BOE 2026-01-01 a 2026-01-31"),
        (
            {"DESDE": "20260105", "HASTA": "2026-01-06"},
            "BORME BOE 2026-01-05 a 2026-01-06",
        ),
        ({}, None),
    ):
        e = correr(clase, [entrada], **params)
        d, h = boe_sin_red[-1]
        assert e.failures == []
        assert e.coverage == f"BORME BOE {d} a {h}"
        assert texto in (None, e.coverage)
    e = correr(clase, [entrada], DESDE="ayer")
    assert e.coverage is None and len(e.failures) == 1


def test_different_coverages_are_joined(borme):
    def api(nombre, *a):
        return (
            falla()
            if "OTRA" in nombre
            else {"backend": "apispain", "coste": {}, "empresas": []}
        )

    orgs = [ACME, Organization(name="OTRA SL"), Organization(name="TERCERA SL")]
    e = borme(org=orgs, apispain=api, APISPAIN_API_KEY="k1234")
    assert e.coverage == (
        "BORME apispain desde 2024-01-01; BORME BOE 2026-01-01 a 2026-03-31"
    )


def test_org_officers_coverage_and_failures_without_backend_data(monkeypatch, correr):
    async def buscar(*a):
        return {"coste": {}, "empresas": []}

    monkeypatch.setattr(bc, "buscar_empresa", buscar)
    assert correr(BormeEmpresa, [ACME]).coverage is None


# ------------------------------------------------------------------ dominios


def test_web_emails_are_confirmed(correr, monkeypatch):
    monkeypatch.setattr(
        load("correos"),
        "correos_web",
        lambda *a, **k: [
            {"email": "info@acme.example.com", "url": "https://x/c", "contexto": "c"}
        ],
    )
    e = correr(EsDomainToWebEmailsEnricher, [Domain(domain="acme.example.com")])
    assert [(f.relation, f.object, f.certainty, f.url) for f in e.findings] == [
        ("PUBLISHES_EMAIL", "info@acme.example.com", "confirmada", "https://x/c")
    ]


@pytest.mark.parametrize(
    "responder, fallos",
    [
        (lambda r: httpx.Response(503, html="<p>x</p>"), ["HTTP 503"]),
        (lambda r: httpx.Response(200, json={}), ["sin páginas HTML"]),
        (falla, ["fuente caída"]),
        (lambda r: httpx.Response(200, html="<p>hola</p>"), []),
    ],
)
def test_web_emails_failures(correr, http, responder, fallos):
    http(responder)
    e = correr(EsDomainToWebEmailsEnricher, [Domain(domain="acme.example.com")])
    assert len(e.failures) == len(fallos)
    for texto, fallo in zip(fallos, e.failures):
        assert texto in fallo and fallo.startswith("Web: acme.example.com: ")


def respuesta_dns(*valores):
    class R(list):
        rrset = SimpleNamespace(ttl=60)

    return R(SimpleNamespace(to_text=lambda v=v: v) for v in valores)


def test_dns_records_and_failures(correr, monkeypatch):
    dom = [Domain(domain="acme.example.com")]
    respuestas = {
        "MX": dns.resolver.NXDOMAIN(),
        "TXT": dns.resolver.NoAnswer.__new__(dns.resolver.NoAnswer),
        "NS": respuesta_dns("ns1.example.com."),
        "CAA": dns.resolver.NoNameservers(),
        "SOA": dns.exception.Timeout(),
    }

    def resolve(self, nombre, tipo):
        if isinstance(respuestas[tipo], Exception):
            raise respuestas[tipo]
        return respuestas[tipo]

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    e = correr(EsDomainToDnsRecordsEnricher, dom)
    assert [(f.relation, f.object, f.source, f.certainty) for f in e.findings] == [
        ("HAS_NS_RECORD", "ns1.example.com", "DNS", "confirmada")
    ]
    assert [re.match(r"DNS: acme\.example\.com (\w+): ", f)[1] for f in e.failures] == [
        "CAA",
        "SOA",
    ]


# ------------------------------------------------------------------ InternetDB


def peticion(estado, datos=None):
    r = requests.Response()
    r.status_code = estado
    r._content = json.dumps(datos).encode()
    return lambda *a, **k: r


IP = Ip(address="192.0.2.1")


def test_internetdb_findings(correr, monkeypatch):
    datos = {"ports": [443], "cpes": ["cpe:/a:f5:nginx:1.26.3"]}
    monkeypatch.setattr(requests, "get", peticion(200, datos))
    e = correr(EsIpToPortsInternetdbEnricher, [IP])
    assert [(f.relation, f.object) for f in e.findings] == [
        ("OPEN_PORT", "443/tcp"),
        ("RUNS", "nginx 1.26.3"),
    ]
    assert {(f.source, f.certainty, f.url) for f in e.findings} == {
        (
            "Shodan InternetDB",
            "confirmada",
            "https://internetdb.shodan.io/192.0.2.1",
        )
    }


@pytest.mark.parametrize(
    "estado, datos, texto",
    [(404, {}, None), (429, {}, "429"), (200, {"ports": [999999]}, "Validation")],
)
def test_internetdb_failures(correr, monkeypatch, estado, datos, texto):
    monkeypatch.setattr(requests, "get", peticion(estado, datos))
    e = correr(EsIpToPortsInternetdbEnricher, [IP])
    assert e.findings == [] and len(e.failures) == bool(texto)
    if texto:
        assert e.failures[0].startswith("Shodan InternetDB: 192.0.2.1: ")
        assert texto in e.failures[0]


# ------------------------------------------------------------------ ASN


@pytest.fixture
def asn(monkeypatch, correr):
    def _asn(peeringdb=(), ripestat=(), cymru=None, org=ACME, **params):
        def get(self, url, p):
            datos = peeringdb if "peeringdb" in url else ripestat
            return datos() if callable(datos) else datos

        def resolve(self, *a):
            raise cymru or dns.resolver.NXDOMAIN()

        monkeypatch.setattr(EsOrgToAsnEnricher, "_get", get)
        monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
        return correr(EsOrgToAsnEnricher, [org], **params)

    return _asn


PDB = {"data": [{"asn": 64500, "name": "ACME SL"}]}
RIPE = {
    "data": {
        "categories": [
            {
                "category": "ASNs",
                "suggestions": [{"value": "AS64500", "description": "ACME SL"}],
            }
        ]
    }
}


def test_asn_by_name_is_possible_and_names_its_sources(asn, grafo):
    e = asn(PDB, RIPE)
    assert [(f.relation, f.object, f.certainty, f.source) for f in e.findings] == [
        ("BELONGS_TO", "AS64500", "posible", "PeeringDB, RIPEstat")
    ]
    assert e.failures == [] and grafo.relaciones


def test_asn_source_failures(asn, entorno, monkeypatch):
    monkeypatch.setenv("PDCP_API_KEY", "clave-asnmap")

    def asnmap(self, nombre, clave):
        raise RuntimeError(f"error con {clave}")

    monkeypatch.setattr(EsOrgToAsnEnricher, "_asnmap", asnmap)
    e = asn(falla, {"status": "error", "messages": [["error", "mal"]]})
    assert [f.split(":")[0] for f in e.failures] == ["PeeringDB", "RIPEstat", "asnmap"]
    assert "clave-asnmap" not in str(e.failures) + str(entorno)


def test_asn_cymru_failures_keep_the_result(asn):
    for error in (dns.resolver.NoNameservers(), dns.exception.Timeout()):
        e = asn(PDB, {}, cymru=error)
        assert len(e.findings) == 1 and len(e.failures) == 1
        assert e.failures[0].startswith("Team Cymru: AS64500: ")


def test_asn_without_usable_name_is_a_failure(asn):
    assert len(asn(org=Organization(name="···")).failures) == 1


# ------------------------------------------------------------------ contexto


def test_context_dudosas_keeps_its_filter():
    cypher = " ".join(_contexto.CYPHER.split())
    bloque = cypher[cypher.index("OPTIONAL MATCH") : cypher.index("AS dudosas")]
    assert (
        "(type(d) STARTS WITH 'POSIBLE_' OR type(d) = 'BORME_COINCIDENCIA_PARCIAL')"
        in bloque
    )
    assert "o.nodeType = 'organization' AND d.deleted_at IS NULL" in bloque
    assert "NOT x IN dudosas" in cypher
