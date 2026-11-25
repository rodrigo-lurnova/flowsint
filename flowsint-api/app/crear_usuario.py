"""Create the service account if it does not exist yet.

Usage (from /app/flowsint-api):  python -m app.crear_usuario

Reads FLOWSINT_SERVICE_EMAIL and FLOWSINT_SERVICE_PASSWORD. Idempotent: an
existing account is left untouched (its password is not changed). It works even
when FLOWSINT_DISABLE_REGISTRATION is set, and never prints the password.
"""

import os
import sys
from typing import Callable, Mapping, Optional

from pydantic import EmailStr, TypeAdapter

EMAIL_VAR = "FLOWSINT_SERVICE_EMAIL"
PASSWORD_VAR = "FLOWSINT_SERVICE_PASSWORD"


def _default_session_factory():
    from flowsint_core.core.postgre_db import SessionLocal

    return SessionLocal()


def main(
    env: Optional[Mapping[str, str]] = None,
    session_factory: Optional[Callable] = None,
) -> int:
    env = os.environ if env is None else env
    raw_email = (env.get(EMAIL_VAR) or "").strip()
    password = env.get(PASSWORD_VAR) or ""

    if not raw_email or not password:
        print(f"Error: {EMAIL_VAR} y {PASSWORD_VAR} son obligatorias.", file=sys.stderr)
        return 1
    try:
        email = TypeAdapter(EmailStr).validate_python(raw_email)
    except ValueError:
        print(f"Error: {EMAIL_VAR} no es un email valido.", file=sys.stderr)
        return 1

    # Imported here so that a missing/invalid input fails before touching the DB
    from flowsint_core.core.services import ConflictError, create_auth_service

    db = None
    try:
        db = (session_factory or _default_session_factory)()
        create_auth_service(db).register(email, password)
    except ConflictError:
        print(f"La cuenta {email} ya existe; no se modifica.")
        return 0
    except Exception as e:  # never let the password leak through the message
        detail = str(e).replace(password, "***")
        print(
            f"Error al crear la cuenta: {type(e).__name__}: {detail}", file=sys.stderr
        )
        return 1
    finally:
        if db is not None:
            db.close()

    print(f"Cuenta {email} creada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
