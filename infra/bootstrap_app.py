"""Release-only WSGI app; never included in the customer application ZIP."""

import hmac
import logging
import os
from pathlib import Path
import time
import uuid

from azure.identity import ManagedIdentityCredential
from azure.core.exceptions import AzureError
from flask import Flask, jsonify, request
import psycopg
from psycopg import sql


def run_bootstrap():
    role = os.environ["RUNTIME_ROLE"]
    object_id = str(uuid.UUID(os.environ["RUNTIME_OBJECT_ID"]))
    if not role or len(role.encode("utf-8")) > 63:
        raise ValueError("Invalid runtime role name")
    source = Path(__file__).with_name("bootstrap-database.sql").read_text(encoding="utf-8")
    source = source.replace("\\set ON_ERROR_STOP on\n", "")
    parts = source.split("\\connect weather")
    if len(parts) != 2 or any(line.startswith("\\") for part in parts for line in part.splitlines()):
        raise ValueError("Unsupported bootstrap SQL command")
    credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    try:
        for attempt in range(10):
            try:
                password = credential.get_token(
                    "https://ossrdbms-aad.database.windows.net/.default"
                ).token
                for database, text in zip(("postgres", "weather"), parts):
                    text = text.replace(":'runtime_role'", "{role_literal}")
                    text = text.replace(":'runtime_object_id'", "{object_literal}")
                    text = text.replace(':"runtime_role"', "{role_identifier}")
                    statement = sql.SQL(text).format(
                        role_literal=sql.Literal(role),
                        object_literal=sql.Literal(object_id),
                        role_identifier=sql.Identifier(role),
                    )
                    with psycopg.connect(
                        host=os.environ["PGHOST"], user=os.environ["PGUSER"],
                        dbname=database, password=password, sslmode="verify-full",
                        sslrootcert="/etc/ssl/certs/ca-certificates.crt",
                        connect_timeout=10, autocommit=True,
                    ) as connection:
                        connection.execute(statement, prepare=False)
                        if database == "weather":
                            granted = connection.execute(
                                "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
                                "has_schema_privilege(%s, 'public', 'USAGE'), "
                                "has_schema_privilege(%s, 'public', 'CREATE')", (role, role, role)
                            ).fetchone()
                            if granted != (True, True, True):
                                raise RuntimeError("Runtime database grants were not applied")
                return
            except psycopg.OperationalError:
                logging.warning("Bootstrap connection attempt %d/10 failed", attempt + 1)
                if attempt == 9:
                    raise
                time.sleep(10)
    finally:
        credential.close()


def create_app(bootstrap=run_bootstrap):
    token = os.environ["BOOTSTRAP_STATUS_TOKEN"]
    if len(token) < 64:
        raise ValueError("Bootstrap status token must contain at least 64 characters")
    app = Flask(__name__)
    state = {"status": "succeeded"}
    try:
        bootstrap()
    except (psycopg.Error, AzureError, ValueError, RuntimeError, OSError) as error:
        # Database exceptions can contain connection details; log only their class.
        app.logger.error("Private database bootstrap failed (%s; SQLSTATE %s)",
                         type(error).__name__, getattr(error, "sqlstate", None))
        state = {"status": "failed", "error": type(error).__name__,
                 "sqlstate": getattr(error, "sqlstate", None)}

    @app.get("/status")
    def status():
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + token):
            return jsonify(error="unauthorized"), 401
        response = jsonify(state)
        response.headers["Cache-Control"] = "no-store"
        return response, 200 if state["status"] == "succeeded" else 500

    return app
