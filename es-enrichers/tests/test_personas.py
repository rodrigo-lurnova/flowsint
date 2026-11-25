import math
import os
import sys

import httpx
import pytest

from flowsint_enrichers import registry
from flowsint_enrichers.es import _borme_comun as bc
from flowsint_enrichers.es import _contexto, _evidencias
from flowsint_enrichers.es._scripts import load
from flowsint_enrichers.es.individual_to_emails_correos import (
    EsIndividualToEmailsEnricher as Correos,
)
from flowsint_enrichers.es.individual_to_linkedin_apify import (
    EsIndividualToLinkedinEnricher as Linkedin,
)
from flowsint_enrichers.es.individual_to_organizations_borme import (
    EsIndividualToOrgsBormeEnricher as BormePersona,
)
from flowsint_enrichers.es.individual_to_socials_perfiles import (
    EsIndividualToSocialsEnricher as Perfiles,
)
from flowsint_enrichers.es.organization_to_individuals_borme import (
    EsOrgToIndividualsBormeEnricher as BormeEmpresa,
)
from flowsint_enrichers.es.organization_to_individuals_linkedin_apify import (
    EsOrgToLinkedinEmployeesEnricher as Empleados,
)
from flowsint_types.individual import Individual
from flowsint_types.organization import Organization

ANA = Individual(
    full_name="Ana Ejemplo Ficticia", first_name="Ana", last_name="Ejemplo Ficticia"
)
ACME = Organization(name="ACME SL")
URL_PERFIL = "https://www.linkedin.com/in/ana-ejemplo"


def falla(*args, **kwargs):
    raise RuntimeError("fuente caída")


def fila_empresa(nombre, *caminos):
    return {
        "tipo": "organization",
        "name": nombre,
        "caminos": [list(c) for c in caminos],
    }


def fila_dominio(dominio, *caminos):
    return {"tipo": "domain", "domain": dominio, "caminos": [list(c) for c in caminos]}


def candidato(**kw):
    return {
        "email": "ana@acme.example.com",
        "patron": "{n}",
        "confianza": 0.7,
        "evidencias": ["patrón web"],
        "publicado_web": False,
        "github_commit": False,
        "hunter_verificacion": None,
        "url": None,
        **kw,
    }


@pytest.fixture
def emails(monkeypatch, correr):
    def _emails(*cands, errores=(), **params):
        monkeypatch.setattr(
            load("correos"),
            "buscar_correos",
            lambda *a, **k: {"candidatos": list(cands), "errores": list(errores)},
        )
        return correr(Correos, [ANA], **{"dominio": "acme.example.com", **params})

    return _emails


@pytest.fixture
def borme_persona(monkeypatch, correr, grafo):
    def _buscar(*empresas, linked=None, cese=False):
        cargo = {
            "cargo_codigo": "ADM_UNICO",
            "cargo": "Adm. Unico",
            "accion": "cese" if cese else "nombramiento",
            "fecha": "2026-01-02",
            "persona": "EJEMPLO FICTICIA ANA",
            "borme_id": "BORME-A-2026-1-28",
            "url_pdf": "https://example.com/borme.pdf",
        }
        res = {
            "coste": {},
            "empresas": [
                {
                    "empresa": e,
                    "provincia": "MADRID",
                    "hoja_registral": f"M-{i}",
                    "cargos": [cargo],
                    "cargos_vigentes": [] if cese else [cargo],
                }
                for i, e in enumerate(empresas)
            ],
        }

        async def buscar(nombre, params):
            return res

        monkeypatch.setattr(bc, "buscar_persona", buscar)
        if linked:
            grafo.filas = [fila_empresa(linked, ["ADM_UNICO"])]
        return correr(BormePersona, [ANA])

    return _buscar


@pytest.fixture
def borme_empresa(monkeypatch, correr):
    def _buscar(*empresas, accion="nombramiento", consulta=ACME, **params):
        res = {
            "coste": {},
            "empresas": [
                {
                    "empresa": e,
                    "actos": [
                        {
                            "fecha": "2026-01-02",
                            "num": 1,
                            "tipos": [],
                            "borme_id": "BORME-A-2026-1-28",
                            "url_pdf": "https://example.com/borme.pdf",
                            "cargos": [
                                {
                                    "persona": "EJEMPLO FICTICIA ANA",
                                    "cargo": "Adm. Unico",
                                    "cargo_codigo": "ADM_UNICO",
                                    "accion": accion,
                                }
                            ],
                        }
                    ],
                }
                for e in empresas
            ],
        }

        async def buscar(nombre, p, api_key):
            return res

        monkeypatch.setattr(bc, "buscar_empresa", buscar)
        return correr(BormeEmpresa, [consulta], **params)

    return _buscar


def perfil(**kw):
    return {
        "plataforma": "linkedin",
        "url": URL_PERFIL,
        "usuario": "ana-ejemplo",
        "nombre": "Ana Ejemplo",
        "titular": "Directora",
        "cargo": "CEO",
        "empresa": "ACME",
        "confianza": 0.9,
        "evidencias": ["nombre 100%"],
        **kw,
    }


# ------------------------------------------------------------------ correos


def test_email_finding_keeps_confidence_and_evidence(emails):
    f = emails(
        candidato(email="a@b.es", confianza=0.7, evidencias=["patrón web"])
    ).findings[0]
    assert (f.object, f.confidence, f.evidence) == ("a@b.es", 0.7, ["patrón web"])
    assert (f.subject, f.relation, f.source) == (
        "Ana Ejemplo Ficticia",
        "PROBABLE_EMAIL",
        "Correos",
    )


@pytest.mark.parametrize(
    "campos, certeza",
    [
        ({}, "posible"),
        (
            {
                "evidencias": [
                    "publicado en https://acme.example.com",
                    "commit público en GitHub",
                ]
            },
            "posible",
        ),
        ({"publicado_web": True, "url": "https://acme.example.com/equipo"}, "probable"),
        ({"github_commit": True, "url": "https://github.com/x/y/commit/1"}, "probable"),
        ({"hunter_verificacion": "valid"}, "probable"),
        ({"hunter_verificacion": "accept_all"}, "posible"),
        ({"hunter_verificacion": "invalid"}, "posible"),
        ({"confianza": 0.99}, "posible"),
    ],
)
def test_email_certainty_comes_from_structured_fields(emails, campos, certeza):
    f = emails(candidato(**campos)).findings[0]
    assert f.certainty == certeza
    assert f.url == campos.get("url")


@pytest.mark.parametrize("valor", [None, math.nan, "alta"])
def test_invalid_confidence_does_not_abort_the_scan(emails, valor):
    e = emails(
        candidato(confianza=valor), candidato(email="b@acme.example.com", confianza=1.4)
    )
    assert [(f.confidence, f.certainty) for f in e.findings] == [
        (None, "posible"),
        (1.0, "posible"),
    ]
    assert e.failures == [] and len(e.resultados) == 2


def test_email_finding_survives_a_graph_write_error(emails, grafo):
    grafo.create_node_from_flowsint_type = falla
    e = emails(candidato())
    assert len(e.findings) == 1 and e.source_run().status == "failed"


def test_correos_source_error_calls_add_failure(monkeypatch, correr):
    correos = load("correos")

    def buscar(nombre, *a, **k):
        if nombre.startswith("Ana"):
            falla()
        return {"candidatos": [candidato()]}

    monkeypatch.setattr(correos, "buscar_correos", buscar)
    otro = Individual(
        full_name="Luis Inventado", first_name="Luis", last_name="Inventado"
    )
    e = correr(Correos, [ANA, otro], dominio="acme.example.com")
    assert e.source_run().status == "failed"
    assert all(
        x in e.failures[0] for x in ("Correos", "Ana Ejemplo Ficticia", "fuente caída")
    )
    assert [f.subject for f in e.findings] == ["Luis Inventado"]


def test_correos_reported_errors_reach_add_failure_and_keep_findings(emails):
    e = emails(candidato(), errores=["Hunter domain-search: HTTP 500"])
    assert e.failures == [
        "Correos: Ana Ejemplo Ficticia @ acme.example.com: Hunter domain-search: HTTP 500"
    ]
    assert len(e.findings) == 1


def test_environment_keys_are_masked_in_failures_and_logs(monkeypatch, correr, entorno):
    monkeypatch.setenv("HUNTER_API_KEY", "clave-secreta")

    def buscar(*a, **k):
        raise RuntimeError("GET /?api_key=clave-secreta")

    monkeypatch.setattr(load("correos"), "buscar_correos", buscar)
    e = correr(Correos, [ANA], dominio="acme.example.com")
    assert (
        "clave-secreta" not in "".join(e.failures + entorno) and "***" in e.failures[0]
    )


def cliente(*respuestas):
    cola = list(respuestas)
    return httpx.Client(transport=httpx.MockTransport(lambda req: cola.pop(0)))


def test_hunter_reports_failures_but_not_informative_statuses():
    correos = load("correos")
    r = correos.hunter(
        "acme.example.com",
        "Ana Ejemplo",
        cliente(httpx.Response(500), httpx.Response(429)),
        "k",
    )
    assert len(correos.errores_hunter(r)) == 2
    r = correos.hunter(
        "acme.example.com",
        "Ana Ejemplo",
        cliente(httpx.Response(200, json={"data": {}}), httpx.Response(451, json={})),
        "k",
    )
    assert correos.errores_hunter(r) == []
    assert correos.errores_hunter(None) == []

    def corte(req):
        raise httpx.ConnectError("sin conexión")

    r = correos.hunter(
        "acme.example.com",
        "Ana",
        httpx.Client(transport=httpx.MockTransport(corte)),
        "k",
    )
    assert len(correos.errores_hunter(r)) == 2


def test_hunter_keeps_verification_status():
    r = load("correos").hunter(
        "acme.example.com",
        "Ana Ejemplo",
        cliente(
            httpx.Response(
                200,
                json={
                    "data": {
                        "emails": [
                            {
                                "value": "ana@acme.example.com",
                                "sources": [],
                                "verification": {"status": "valid"},
                            },
                            {"value": "b@acme.example.com", "verification": None},
                        ]
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "email": "ana@acme.example.com",
                        "score": 90,
                        "verification": {"status": "accept_all"},
                    }
                },
            ),
        ),
        "k",
    )
    assert [c["verificacion"] for c in r["correos"]] == ["valid", None]
    assert r["finder"]["verificacion"] == "accept_all"


def test_github_and_gravatar_report_errors():
    correos, errores = load("correos"), []
    assert (
        correos.github_commits("q", cliente(httpx.Response(403)), errores=errores) == []
    )
    assert (
        correos.gravatar("a@acme.example.com", cliente(httpx.Response(503)), errores)
        is None
    )
    assert (
        correos.gravatar("a@acme.example.com", cliente(httpx.Response(404)), errores)
        is None
    )
    assert errores == ["GitHub: HTTP 403", "Gravatar: HTTP 503"]


@pytest.fixture
def buscar_correos(monkeypatch):
    """correos.buscar_correos con todas sus fuentes simuladas."""
    correos = load("correos")

    def _buscar(publicos=(), commits=(), hunter=None, mx=None, **kw):
        monkeypatch.setattr(correos.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            correos, "mx", lambda *a: mx or {"tiene_mx": True, "hosts": []}
        )
        monkeypatch.setattr(
            correos,
            "correos_web",
            publicos if callable(publicos) else lambda *a: list(publicos),
        )
        monkeypatch.setattr(
            correos,
            "github_commits",
            lambda q, c, per_page=30, errores=None: list(commits),
        )
        monkeypatch.setattr(correos, "gravatar", lambda *a: None)
        if hunter is not None:
            monkeypatch.setattr(correos, "hunter", lambda *a: hunter)
        return correos.buscar_correos(
            "Ana Ejemplo",
            "acme.example.com",
            hunter_key="k" if hunter is not None else None,
            **kw,
        )

    return _buscar


def por_email(res, email):
    return next(c for c in res["candidatos"] if c["email"] == email)


def test_candidate_fields_web(buscar_correos):
    res = buscar_correos(
        publicos=[
            {
                "email": "ana@acme.example.com",
                "url": "https://acme.example.com/equipo",
                "contexto": "",
            },
            {
                "email": "direccion.ana@acme.example.com",
                "url": "https://acme.example.com/a",
                "contexto": "Ana Ejemplo",
            },
        ],
        github=False,
    )
    c = por_email(res, "ana@acme.example.com")
    assert (c["publicado_web"], c["github_commit"], c["url"]) == (
        True,
        False,
        "https://acme.example.com/equipo",
    )
    assert por_email(res, "direccion.ana@acme.example.com")["publicado_web"] is True
    otro = por_email(res, "a.ejemplo@acme.example.com")
    assert (
        otro["publicado_web"],
        otro["github_commit"],
        otro["hunter_verificacion"],
        otro["url"],
    ) == (False, False, None, None)
    assert res["errores"] == []


def test_candidate_fields_github(buscar_correos):
    res = buscar_correos(
        commits=[
            {
                "email": "ana@acme.example.com",
                "autor": "Ana Ejemplo",
                "repo": "x/y",
                "url": "https://github.com/x/y/commit/1",
                "login": "x",
            }
        ]
    )
    c = por_email(res, "ana@acme.example.com")
    assert (c["github_commit"], c["url"]) == (True, "https://github.com/x/y/commit/1")


def test_candidate_fields_hunter(buscar_correos):
    res = buscar_correos(
        github=False,
        hunter={
            "correos": [
                {
                    "email": "ana.ejemplo@acme.example.com",
                    "verificacion": "valid",
                    "nombre": "Ana Ejemplo",
                    "fuentes": ["https://example.com/f"],
                },
                {
                    "email": "aejemplo@acme.example.com",
                    "verificacion": "valid",
                    "nombre": "Alicia Distinta",
                    "fuentes": [],
                },
            ],
            "finder": {
                "email": "ana@acme.example.com",
                "score": 80,
                "verificacion": "accept_all",
            },
        },
    )
    c = por_email(res, "ana.ejemplo@acme.example.com")
    assert (c["hunter_verificacion"], c["url"]) == ("valid", "https://example.com/f")
    assert por_email(res, "ana@acme.example.com")["hunter_verificacion"] == "accept_all"
    assert por_email(res, "aejemplo@acme.example.com")["hunter_verificacion"] is None


def test_buscar_correos_collects_source_errors(buscar_correos):
    def web(*a):
        raise httpx.ConnectError("sin conexión")

    res = buscar_correos(
        publicos=web,
        hunter={"error_domain_search": "HTTP 500"},
        github=False,
        mx={"tiene_mx": False, "hosts": [], "error": "LifetimeTimeout", "fallo": True},
    )
    texto = "; ".join(res["errores"])
    assert all(
        x in texto
        for x in ("Web", "Hunter domain-search: HTTP 500", "MX: LifetimeTimeout")
    )
    assert len(res["errores"]) == 3
    assert not any(
        "no tiene MX" in ev for c in res["candidatos"] for ev in c["evidencias"]
    )


def test_domain_without_mx_is_not_an_error(buscar_correos):
    res = buscar_correos(
        github=False,
        mx={"tiene_mx": False, "hosts": [], "error": "NoAnswer", "fallo": False},
    )
    assert res["errores"] == []
    assert "¡el dominio no tiene MX!" in res["candidatos"][0]["evidencias"]


# ------------------------------------------------------------------ BORME: persona -> empresa


def test_person_search_other_company_is_possible(borme_persona, grafo):
    f = borme_persona("OTRA SL", linked="ACME SL").findings[0]
    assert (f.certainty, f.relation, f.source) == (
        "posible",
        "POSIBLE_ADM_UNICO",
        "BORME",
    )
    assert (f.subject, f.object, f.url) == (
        "Ana Ejemplo Ficticia",
        "OTRA SL",
        "https://example.com/borme.pdf",
    )
    assert [r[1] for r in grafo.relaciones] == ["POSIBLE_ADM_UNICO"]


def test_person_search_linked_company_is_confirmed(borme_persona, grafo):
    e = borme_persona("ACME SOCIEDAD LIMITADA", "ACME TRADING SL", linked="Acme, S.L.")
    assert [f.certainty for f in e.findings] == ["confirmada", "posible"]
    assert [r[1] for r in grafo.relaciones] == ["ADM_UNICO", "POSIBLE_ADM_UNICO"]


def test_person_search_past_position(borme_persona, grafo):
    borme_persona("OTRA SL", cese=True)
    assert [r[1] for r in grafo.relaciones] == ["POSIBLE_EX_ADM_UNICO"]


def test_person_search_homonymous_companies_are_possible(borme_persona):
    assert [
        f.certainty
        for f in borme_persona("ACME SL", "ACME SA", linked="ACME SL").findings
    ] == ["posible"] * 2


def test_person_search_company_linked_only_as_possible_stays_possible(
    borme_persona, grafo
):
    grafo.filas = [fila_empresa("OTRA SL", ["POSIBLE_ADM_UNICO"])]
    assert borme_persona("OTRA SL").findings[0].certainty == "posible"


def test_person_search_company_linked_through_another_node_is_possible(
    borme_persona, grafo
):
    grafo.filas = [fila_empresa("OTRA SL", ["ADM_UNICO", "SOCIO_UNICO"])]
    assert borme_persona("OTRA SL").findings[0].certainty == "posible"


def test_borme_persona_source_error_calls_add_failure(monkeypatch, correr):
    monkeypatch.setattr(bc, "buscar_persona", falla)
    e = correr(BormePersona, [ANA])
    assert (
        e.source_run().status == "failed"
        and "BORME: Ana Ejemplo Ficticia" in e.failures[0]
    )


def test_borme_persona_graph_error_keeps_searching(borme_persona, grafo):
    grafo.error = RuntimeError("grafo caído")
    e = borme_persona("ACME SL")
    assert "grafo caído" in e.failures[0]
    assert [f.certainty for f in e.findings] == ["posible"]


# ------------------------------------------------------------------ BORME: empresa -> personas


def test_company_officers_are_confirmed_on_single_exact_match(borme_empresa, grafo):
    f = borme_empresa("ACME SL").findings[0]
    assert (f.certainty, f.relation, f.subject, f.source) == (
        "confirmada",
        "ADM_UNICO",
        "ACME SL",
        "BORME",
    )
    assert (f.object, f.url) == (
        "Ana Ejemplo Ficticia",
        "https://example.com/borme.pdf",
    )
    assert "BORME-A-2026-1-28" in f.evidence[0]
    assert [r[1] for r in grafo.relaciones] == ["ADM_UNICO"]


@pytest.mark.parametrize(
    "empresas", [("ACME SL", "ACME SL"), ("ACME SL", "ACME SA"), ("ACME SA",)]
)
def test_company_officers_homonymous_companies_are_possible(
    borme_empresa, grafo, empresas
):
    e = borme_empresa(*empresas)
    assert {f.certainty for f in e.findings} == {"posible"}
    assert {r[1] for r in grafo.relaciones} == {"POSIBLE_ADM_UNICO"}


def test_company_officers_unipersonal_form_is_the_same_company(borme_empresa):
    assert borme_empresa("ACME SLU").findings[0].certainty == "confirmada"


def test_company_officers_partial_match_is_possible(borme_empresa, grafo):
    e = borme_empresa("ACME SL", "ACME TRADING SL")
    assert [(f.subject, f.certainty) for f in e.findings] == [
        ("ACME SL", "confirmada"),
        ("ACME TRADING SL", "posible"),
    ]
    assert [r[1] for r in grafo.relaciones] == [
        "ADM_UNICO",
        "BORME_COINCIDENCIA_PARCIAL",
        "POSIBLE_ADM_UNICO",
    ]


def test_company_officers_partial_past_position(borme_empresa, grafo):
    borme_empresa("ACME TRADING SL", accion="cese")
    assert "POSIBLE_EX_ADM_UNICO" in [r[1] for r in grafo.relaciones]


def test_borme_empresa_source_error_calls_add_failure(monkeypatch, correr):
    monkeypatch.setattr(bc, "buscar_empresa", falla)
    e = correr(BormeEmpresa, [ACME])
    assert e.source_run().status == "failed" and "BORME: ACME SL" in e.failures[0]


# ------------------------------------------------------------------ contexto


def test_context_ignores_possible_relations(grafo):
    assert (
        "POSIBLE_" in _contexto.CYPHER
        and "BORME_COINCIDENCIA_PARCIAL" in _contexto.CYPHER
    )
    assert _contexto.CYPHER.index("POSIBLE_") < _contexto.CYPHER.index("LIMIT")
    grafo.filas = [
        fila_dominio("otra.example.com", ["POSIBLE_ADM_UNICO", "WEB_PROBABLE"]),
        fila_empresa("OTRA SL", ["ADM_UNICO", "BORME_COINCIDENCIA_PARCIAL"]),
        {"tipo": "domain", "domain": "sin-camino.example.com"},
        fila_dominio(
            "acme.example.com",
            ["POSIBLE_CONSEJERO", "HAS_DOMAIN"],
            ["ADM_UNICO", "HAS_DOMAIN"],
        ),
    ]
    assert _contexto.vecinos(grafo, "s", "Ana") == {
        "empresas": [],
        "empresas_directas": [],
        "dominios": ["acme.example.com"],
        "linkedin": [],
    }


def test_context_errors_reach_the_caller(grafo):
    grafo.error = RuntimeError("grafo caído")
    with pytest.raises(RuntimeError):
        _contexto.vecinos(grafo, "s", "Ana")


def test_context_error_is_recorded_by_correos_and_socials(grafo, correr):
    grafo.error = RuntimeError("grafo caído")
    for clase in (Correos, Perfiles):
        e = correr(clase, [ANA, ANA])
        assert len(e.failures) == 2 and "grafo caído" in e.failures[0]


def test_linkedin_context_error(grafo, correr, monkeypatch):
    grafo.error = RuntimeError("grafo caído")
    monkeypatch.setattr(
        load("apify_actores"), "linkedin_buscar", lambda *a, **k: [perfil()]
    )
    e = correr(Linkedin, [ANA], APIFY_TOKEN="t")
    assert (
        "grafo caído" in e.failures[0] and e.findings == []
    )  # sin empresa no se paga una búsqueda a ciegas
    e = correr(Linkedin, [ANA], APIFY_TOKEN="t", empresa="ACME")
    assert "grafo caído" in e.failures[0] and len(e.findings) == 1


# ------------------------------------------------------------------ LinkedIn


@pytest.mark.parametrize(
    "conf, esperado",
    [(0.8, (0.8, "probable")), (0.79, (0.79, "posible")), (None, (None, "posible"))],
)
def test_linkedin_certainty(monkeypatch, correr, conf, esperado):
    monkeypatch.setattr(
        load("apify_actores"),
        "linkedin_buscar",
        lambda *a, **k: [perfil(confianza=conf)],
    )
    f = correr(Linkedin, [ANA], APIFY_TOKEN="t").findings[0]
    assert (f.confidence, f.certainty) == esperado
    assert (f.subject, f.relation, f.object, f.url) == (
        "Ana Ejemplo Ficticia",
        "HAS_LINKEDIN",
        URL_PERFIL,
        URL_PERFIL,
    )
    assert (f.source, f.evidence) == ("LinkedIn (Apify)", ["nombre 100%"])


def test_linkedin_source_error_calls_add_failure(monkeypatch, correr, entorno):
    def buscar(*a, **k):
        raise RuntimeError("Apify HTTP 500 Bearer token-t")

    monkeypatch.setattr(load("apify_actores"), "linkedin_buscar", buscar)
    e = correr(Linkedin, [ANA], APIFY_TOKEN="token-t")
    assert e.source_run().status == "failed"
    assert "LinkedIn (Apify): Ana Ejemplo Ficticia" in e.failures[0]
    assert "token-t" not in "".join(e.failures + entorno)


@pytest.mark.parametrize("clase, entrada", [(Linkedin, ANA), (Empleados, ACME)])
def test_missing_apify_token_calls_add_failure(correr, clase, entrada):
    e = correr(clase, [entrada])
    assert e.failures == ["APIFY_TOKEN no configurado"] and e.resultados == []


@pytest.mark.parametrize(
    "conf, esperado", [(None, (None, "posible")), (0.85, (0.85, "probable"))]
)
def test_company_employees(monkeypatch, correr, conf, esperado):
    p = perfil()
    p.pop("confianza")
    p.pop("evidencias")
    if conf:
        p["confianza"] = conf
    monkeypatch.setattr(
        load("apify_actores"), "linkedin_empleados", lambda *a, **k: [p]
    )
    e = correr(
        Empleados,
        [ACME],
        APIFY_TOKEN="t",
        empresa_linkedin_url="https://www.linkedin.com/company/acme",
    )
    assert [(f.subject, f.relation, f.object) for f in e.findings] == [
        ("ACME SL", "WORKS_AT", "Ana Ejemplo"),
        ("Ana Ejemplo", "HAS_LINKEDIN", URL_PERFIL),
    ]
    assert all(
        (f.confidence, f.certainty, f.url) == (*esperado, URL_PERFIL)
        for f in e.findings
    )
    assert e.findings[0].evidence == [
        "página de empresa https://www.linkedin.com/company/acme",
        "Directora",
    ]


def test_company_employees_source_error_calls_add_failure(monkeypatch, correr):
    monkeypatch.setattr(load("apify_actores"), "google", falla)
    e = correr(Empleados, [ACME], APIFY_TOKEN="t")
    assert (
        e.source_run().status == "failed"
        and "LinkedIn (Apify): ACME SL" in e.failures[0]
    )


def test_apify_timeout_with_partial_results_is_reported(monkeypatch, correr):
    apify = load("apify_actores")
    monkeypatch.setattr(
        apify.httpx,
        "post",
        lambda *a, **k: httpx.Response(400, text="TIMED-OUT run ID: abc"),
    )
    monkeypatch.setattr(
        apify.httpx,
        "get",
        lambda url, **k: httpx.Response(
            200,
            json={"data": {"defaultDatasetId": "d"}}
            if "actor-runs" in url
            else [{"fullName": "Ana Ejemplo"}],
        ),
    )
    e = correr(
        Empleados,
        [ACME],
        APIFY_TOKEN="t",
        empresa_linkedin_url="https://www.linkedin.com/company/acme",
    )
    assert "parciales" in e.failures[0] and len(e.findings) == 1


# ------------------------------------------------------------------ perfiles


@pytest.fixture
def socials(monkeypatch, correr):
    def _socials(*items, errores=(), avisos=(), **params):
        llamadas = []

        def buscar(*a, **k):
            llamadas.append((a, k))
            return {
                "perfiles": list(items),
                "avisos": [*avisos, *errores],
                "errores": list(errores),
            }

        monkeypatch.setattr(load("perfiles"), "buscar_perfiles", buscar)
        e = correr(Perfiles, [ANA], **{"empresa": "ACME", **params})
        e.llamadas = llamadas
        return e

    return _socials


def red(**kw):
    return {
        "red": "github",
        "usuario": "ana",
        "url": "https://github.com/ana",
        "confianza": 0.9,
        "evidencias": ["API de GitHub"],
        **kw,
    }


@pytest.mark.parametrize(
    "conf, certeza", [(0.8, "probable"), (0.7, "posible"), (None, "posible")]
)
def test_socials_certainty(socials, conf, certeza):
    f = socials(red(confianza=conf)).findings[0]
    assert (f.certainty, f.confidence, f.source) == (certeza, conf, "Perfiles públicos")
    assert (f.relation, f.object, f.url) == (
        "HAS_PROFILE",
        "https://github.com/ana",
        "https://github.com/ana",
    )


def test_apify_token_not_written_to_environ(socials):
    e = socials(APIFY_TOKEN="t", usar_apify="true")
    assert "APIFY_TOKEN" not in os.environ
    assert e.llamadas[0][1]["apify_token"] == "t" and e.llamadas[0][0][4] is True


def test_socials_without_token_still_runs_free_sources(socials):
    e = socials(red(), usar_apify="true")
    assert e.failures == ["APIFY_TOKEN no configurado"]
    assert e.llamadas[0][0][4] is False and len(e.findings) == 1


def test_socials_source_error_calls_add_failure(monkeypatch, correr):
    monkeypatch.setattr(load("perfiles"), "buscar_perfiles", falla)
    e = correr(Perfiles, [ANA], empresa="ACME")
    assert (
        e.source_run().status == "failed"
        and "Perfiles públicos: Ana Ejemplo Ficticia" in e.failures[0]
    )


def test_socials_reports_errors_but_not_informative_notices(socials):
    e = socials(red(), errores=["SearXNG: caído"], avisos=["informativo"])
    assert (
        e.failures == ["Perfiles públicos: Ana Ejemplo Ficticia: SearXNG: caído"]
        and len(e.findings) == 1
    )


def test_token_travels_as_argument_to_apify(monkeypatch):
    perfiles, apify, tokens = load("perfiles"), load("apify_actores"), []
    monkeypatch.setattr(perfiles.time, "sleep", lambda s: None)
    monkeypatch.setattr(perfiles, "searxng", lambda *a: [])
    monkeypatch.setattr(
        apify, "ejecutar", lambda a, e, token=None, **k: tokens.append(token) or []
    )
    perfiles.buscar_perfiles(
        "Ana Ejemplo", "ACME", redes=["x"], apify=True, apify_token="t"
    )
    perfiles.buscar_perfiles("Ana Ejemplo", "ACME", redes=["x"], apify=True)
    assert tokens == ["t", None] and "APIFY_TOKEN" not in os.environ


def test_apify_cli_still_uses_the_environment(monkeypatch):
    apify, cabeceras = load("apify_actores"), []
    monkeypatch.setenv("APIFY_TOKEN", "del-entorno")
    monkeypatch.setattr(
        apify.httpx,
        "post",
        lambda url, **k: cabeceras.append(k["headers"]) or httpx.Response(201, json=[]),
    )
    assert apify.google(["q"]) == []
    assert cabeceras == [{"Authorization": "Bearer del-entorno"}]


def test_perfiles_collects_source_errors(monkeypatch):
    perfiles = load("perfiles")
    monkeypatch.setattr(perfiles.time, "sleep", lambda s: None)
    monkeypatch.setattr(perfiles, "searxng", falla)
    original = httpx.Client
    monkeypatch.setattr(
        perfiles.httpx,
        "Client",
        lambda **k: original(
            transport=httpx.MockTransport(lambda req: httpx.Response(403))
        ),
    )
    res = perfiles.buscar_perfiles("Ana Ejemplo", "ACME", redes=["github"])
    assert (
        any("SearXNG" in e for e in res["errores"])
        and "GitHub: HTTP 403" in res["errores"]
    )
    assert res["avisos"] == res["errores"]


def test_searxng_blocked_engines_are_an_error():
    perfiles = load("perfiles")
    bloqueado = httpx.Response(
        200, json={"results": [], "unresponsive_engines": [["yandex", "CAPTCHA"]]}
    )
    with pytest.raises(RuntimeError, match="yandex"):
        perfiles.searxng("q", cliente(bloqueado))
    assert (
        perfiles.searxng("q", cliente(httpx.Response(200, json={"results": []}))) == []
    )


# ------------------------------------------------------------------ evidencias y carga


@pytest.mark.parametrize(
    "valor, esperado",
    [
        (0.5, 0.5),
        (-1, 0.0),
        (3, 1.0),
        (math.inf, None),
        (math.nan, None),
        ("0.5", None),
        (True, None),
        (None, None),
    ],
)
def test_confidence_is_clamped_or_skipped(valor, esperado):
    assert _evidencias.confianza(valor) == esperado


def test_test_files_are_not_loaded_as_enrichers(monkeypatch):
    antes = set(sys.modules)
    monkeypatch.setattr(registry, "_enrichers_loaded", False)
    monkeypatch.delenv("FLOWSINT_DISABLED_ENRICHERS", raising=False)
    monkeypatch.setattr(
        registry.importlib, "import_module", lambda n: importados.append(n)
    )
    importados = []
    import flowsint_enrichers

    monkeypatch.setattr(
        flowsint_enrichers, "__path__", [str(flowsint_enrichers.es.__path__[0])]
    )
    registry.load_all_enrichers()
    assert importados and not [m for m in importados if "tests" in m or "conftest" in m]
    assert set(sys.modules) == antes
