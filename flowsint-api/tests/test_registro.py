"""Closable registration and the service-account bootstrap."""

import pytest

from app import crear_usuario
from flowsint_core.core.models import Profile
from flowsint_core.core.postgre_db import get_db
from flowsint_core.core.repositories import ProfileRepository

BODY = {"email": "alguien@example.com", "password": "una-clave-larga"}
PASSWORD = "s3cret-service-pass"


def _count(db_session):
    return db_session.query(Profile).count()


def test_register_open_by_default(client, db_session, monkeypatch):
    monkeypatch.delenv("FLOWSINT_DISABLE_REGISTRATION", raising=False)
    res = client.post("/api/auth/register", json=BODY)
    assert res.status_code == 201
    assert _count(db_session) == 1


@pytest.mark.parametrize("value", ["true", "1", "yes", " TRUE ", "Yes"])
def test_register_disabled_returns_403(client, db_session, monkeypatch, value):
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", value)
    res = client.post("/api/auth/register", json=BODY)
    assert res.status_code == 403
    assert _count(db_session) == 0


@pytest.mark.parametrize("value", ["", "false", "0", "no", "maybe"])
def test_register_other_values_keep_it_open(client, monkeypatch, value):
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", value)
    assert client.post("/api/auth/register", json=BODY).status_code == 201


def test_register_flag_is_read_at_request_time(client, monkeypatch):
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", "true")
    assert client.post("/api/auth/register", json=BODY).status_code == 403
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", "false")
    assert client.post("/api/auth/register", json=BODY).status_code == 201


def test_register_disabled_does_not_touch_the_database(client, monkeypatch):
    from app.main import app

    def boom():
        raise AssertionError("the database must not be used")

    app.dependency_overrides[get_db] = boom
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", "true")
    assert client.post("/api/auth/register", json=BODY).status_code == 403
    # even with a malformed body
    assert client.post("/api/auth/register", json={}).status_code == 403


# --- crear_usuario ----------------------------------------------------------


@pytest.fixture
def factory(db_session):
    return lambda: db_session


def _env(**extra):
    return {
        "FLOWSINT_SERVICE_EMAIL": "servicio@example.com",
        "FLOWSINT_SERVICE_PASSWORD": PASSWORD,
        **extra,
    }


def test_crear_usuario_creates_the_account(factory, db_session, capsys):
    assert crear_usuario.main(_env(), factory) == 0
    user = db_session.query(Profile).one()
    assert user.email == "servicio@example.com"
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_crear_usuario_is_idempotent(factory, db_session, capsys):
    assert crear_usuario.main(_env(), factory) == 0
    first = db_session.query(Profile).one().hashed_password
    other = _env(FLOWSINT_SERVICE_PASSWORD="otra-clave-distinta")
    assert crear_usuario.main(other, factory) == 0
    assert _count(db_session) == 1
    assert db_session.query(Profile).one().hashed_password == first
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert "otra-clave-distinta" not in out.out + out.err


def test_crear_usuario_works_with_registration_disabled(
    factory, db_session, monkeypatch
):
    monkeypatch.setenv("FLOWSINT_DISABLE_REGISTRATION", "true")
    assert crear_usuario.main(_env(), factory) == 0
    assert _count(db_session) == 1


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"FLOWSINT_SERVICE_EMAIL": "servicio@example.com"},
        {"FLOWSINT_SERVICE_PASSWORD": PASSWORD},
        {"FLOWSINT_SERVICE_EMAIL": " ", "FLOWSINT_SERVICE_PASSWORD": PASSWORD},
        {
            "FLOWSINT_SERVICE_EMAIL": "servicio@example.com",
            "FLOWSINT_SERVICE_PASSWORD": "",
        },
        {
            "FLOWSINT_SERVICE_EMAIL": "no-es-email",
            "FLOWSINT_SERVICE_PASSWORD": PASSWORD,
        },
    ],
)
def test_crear_usuario_rejects_missing_or_invalid_input(
    factory, db_session, capsys, env
):
    assert crear_usuario.main(env, factory) != 0
    assert _count(db_session) == 0
    out = capsys.readouterr()
    assert out.err.strip()
    assert PASSWORD not in out.out + out.err


def test_crear_usuario_concurrent_creation_is_success(factory, db_session, monkeypatch):
    db_session.add(Profile(email="servicio@example.com", hashed_password="previo"))
    db_session.commit()
    # The lookup does not see the row (another process created it meanwhile),
    # so the insert hits the unique constraint.
    monkeypatch.setattr(ProfileRepository, "get_by_email", lambda self, email: None)
    assert crear_usuario.main(_env(), factory) == 0
    assert _count(db_session) == 1
    assert db_session.query(Profile).one().hashed_password == "previo"


def test_crear_usuario_never_prints_the_password_on_errors(
    factory, monkeypatch, capsys
):
    def explode(self, email, password):
        raise RuntimeError(f"fallo con {password}")

    from flowsint_core.core.services.auth_service import AuthService

    monkeypatch.setattr(AuthService, "register", explode)
    assert crear_usuario.main(_env(), factory) != 0
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_crear_usuario_does_not_import_the_fastapi_app():
    import subprocess
    import sys

    code = (
        "import os,sys;os.environ.setdefault('AUTH_SECRET','x');"
        "import app.crear_usuario;"
        "sys.exit(1 if 'app.main' in sys.modules or 'fastapi' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
