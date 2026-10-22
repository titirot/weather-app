"""PostgreSQL persistence for users, saved forecasts and the activity log."""

import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from auth import User


logger = logging.getLogger(__name__)

ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
SCHEMA_LOCK_ID = 727274001
MAX_SAVED_FORECASTS = 50

SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS app_users (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        external_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS saved_forecasts (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
        location_key TEXT NOT NULL,
        city_query TEXT NOT NULL,
        location JSONB NOT NULL,
        summary JSONB NOT NULL,
        forecast JSONB NOT NULL,
        saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, location_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS activity_log_user_created_idx "
    "ON activity_log (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS activity_log_created_idx ON activity_log (created_at)",
)


class StorageUnavailable(Exception):
    pass


class FavoriteLimitReached(Exception):
    pass


def location_label(location: Mapping) -> str:
    parts = [location.get("name"), location.get("state"), location.get("country")]
    return ", ".join(part for part in parts if isinstance(part, str) and part)


def forecast_summary(weather: Mapping) -> dict:
    current = weather["current"]
    main = current.get("main", {})
    conditions = current.get("weather") or [{}]
    return {
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "humidity": main.get("humidity"),
        "description": conditions[0].get("description"),
        "condition_id": conditions[0].get("id"),
        "observed_at": current.get("dt"),
    }


def _iso(value: datetime) -> str:
    return value.isoformat()


def _favorite_json(row: Mapping, include_forecast: bool = False) -> dict:
    result = {
        "id": row["id"],
        "city_query": row["city_query"],
        "location": row["location"],
        "summary": row["summary"],
        "saved_at": _iso(row["saved_at"]),
    }
    if include_forecast:
        result["forecast"] = row["forecast"]
    return result


class Storage:
    def __init__(self, pool: ConnectionPool, activity_retention_days: int = 90):
        self._pool = pool
        self._retention_days = activity_retention_days
        self._ready = False
        self._lock = threading.Lock()

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._pool.open(wait=True, timeout=10)
            with self._pool.connection() as conn, conn.transaction():
                # Serialize schema creation across workers and instances.
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                for statement in SCHEMA_SQL:
                    conn.execute(statement)
                conn.execute(
                    "DELETE FROM activity_log "
                    "WHERE created_at < now() - make_interval(days => %s)",
                    (self._retention_days,),
                )
            self._ready = True

    @contextmanager
    def _transaction(self):
        try:
            self._ensure_ready()
            with self._pool.connection() as conn:
                conn.row_factory = dict_row
                with conn.transaction():
                    yield conn
        except (psycopg.OperationalError, PoolTimeout) as error:
            # Exception text may contain host names or user names; log only the type.
            logger.warning("Database unavailable: %s", type(error).__name__)
            raise StorageUnavailable from None

    @staticmethod
    def _user_id(conn, user: User) -> int | None:
        row = conn.execute(
            "SELECT id FROM app_users WHERE external_id = %s", (user.external_id,)
        ).fetchone()
        return row["id"] if row else None

    def _upsert_user(self, conn, user: User) -> int:
        row = conn.execute(
            """
            INSERT INTO app_users (external_id, display_name) VALUES (%s, %s)
            ON CONFLICT (external_id) DO UPDATE
                SET display_name = EXCLUDED.display_name, last_seen_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """,
            (user.external_id, user.display_name),
        ).fetchone()
        if row["inserted"]:
            self._log(conn, row["id"], "account_created")
        return row["id"]

    def _log(self, conn, user_id: int, action: str, details: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO activity_log (user_id, action, details) VALUES (%s, %s, %s)",
            (user_id, action, Jsonb(details or {})),
        )
        conn.execute(
            "DELETE FROM activity_log WHERE user_id = %s "
            "AND created_at < now() - make_interval(days => %s)",
            (user_id, self._retention_days),
        )

    def list_favorites(self, user: User) -> list[dict]:
        with self._transaction() as conn:
            user_id = self._user_id(conn, user)
            if user_id is None:
                return []
            rows = conn.execute(
                "SELECT id, city_query, location, summary, saved_at FROM saved_forecasts "
                "WHERE user_id = %s ORDER BY saved_at DESC",
                (user_id,),
            ).fetchall()
        return [_favorite_json(row) for row in rows]

    def get_favorite(self, user: User, favorite_id: int) -> dict | None:
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT f.id, f.city_query, f.location, f.summary, f.forecast, f.saved_at
                FROM saved_forecasts f JOIN app_users u ON u.id = f.user_id
                WHERE f.id = %s AND u.external_id = %s
                """,
                (favorite_id, user.external_id),
            ).fetchone()
        return _favorite_json(row, include_forecast=True) if row else None

    def save_favorite(self, user: User, city_query: str, weather: Mapping) -> tuple[dict, bool]:
        location = weather["location"]
        location_key = f"{location['lat']:.2f},{location['lon']:.2f}"
        with self._transaction() as conn:
            user_id = self._upsert_user(conn, user)
            # Lock the user row so concurrent saves cannot exceed the limit.
            conn.execute("SELECT id FROM app_users WHERE id = %s FOR UPDATE", (user_id,))
            counts = conn.execute(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE location_key = %s) AS existing "
                "FROM saved_forecasts WHERE user_id = %s",
                (location_key, user_id),
            ).fetchone()
            if not counts["existing"] and counts["total"] >= MAX_SAVED_FORECASTS:
                raise FavoriteLimitReached
            row = conn.execute(
                """
                INSERT INTO saved_forecasts
                    (user_id, location_key, city_query, location, summary, forecast)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, location_key) DO UPDATE SET
                    city_query = EXCLUDED.city_query,
                    location = EXCLUDED.location,
                    summary = EXCLUDED.summary,
                    forecast = EXCLUDED.forecast,
                    saved_at = now()
                RETURNING id, city_query, location, summary, saved_at, (xmax = 0) AS inserted
                """,
                (
                    user_id,
                    location_key,
                    city_query,
                    Jsonb(location),
                    Jsonb(forecast_summary(weather)),
                    Jsonb(dict(weather)),
                ),
            ).fetchone()
            self._log(
                conn,
                user_id,
                "forecast_saved" if row["inserted"] else "forecast_updated",
                {"favorite_id": row["id"], "location": location_label(location)},
            )
        return _favorite_json(row), row["inserted"]

    def delete_favorite(self, user: User, favorite_id: int) -> bool:
        with self._transaction() as conn:
            user_id = self._user_id(conn, user)
            if user_id is None:
                return False
            row = conn.execute(
                "DELETE FROM saved_forecasts WHERE id = %s AND user_id = %s "
                "RETURNING location",
                (favorite_id, user_id),
            ).fetchone()
            if row:
                self._log(
                    conn,
                    user_id,
                    "forecast_deleted",
                    {"favorite_id": favorite_id, "location": location_label(row["location"])},
                )
        return row is not None

    def list_activity(self, user: User, limit: int = 50) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT a.action, a.details, a.created_at
                FROM activity_log a JOIN app_users u ON u.id = a.user_id
                WHERE u.external_id = %s
                ORDER BY a.created_at DESC, a.id DESC LIMIT %s
                """,
                (user.external_id, limit),
            ).fetchall()
        return [
            {"action": row["action"], "details": row["details"], "at": _iso(row["created_at"])}
            for row in rows
        ]

    def delete_user_data(self, user: User) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                "DELETE FROM app_users WHERE external_id = %s RETURNING id",
                (user.external_id,),
            ).fetchone()
        return row is not None

    def close(self) -> None:
        self._pool.close()


def _entra_kwargs_factory(base: dict, client_id: str | None) -> Callable[[], dict]:
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential(client_id=client_id)
        if client_id
        else DefaultAzureCredential()
    )

    # A fresh (cached-until-near-expiry) token is requested for every new
    # physical connection, so passwordless connections keep working.
    def kwargs() -> dict:
        return {**base, "password": credential.get_token(ENTRA_SCOPE).token}

    return kwargs


def create_storage_from_env(environ: Mapping[str, str]) -> Storage | None:
    """Create storage when PGHOST is configured; libpq reads the other PG* variables."""
    if not environ.get("PGHOST"):
        return None
    base = {
        "connect_timeout": 5,
        "application_name": "weather-app",
        "options": "-c statement_timeout=5000",
    }
    auth_method = environ.get("PG_AUTH", "password").strip().lower()
    if auth_method == "entra":
        kwargs = _entra_kwargs_factory(base, environ.get("AZURE_CLIENT_ID") or None)
    elif auth_method == "password":
        kwargs = base
    else:
        raise ValueError("PG_AUTH must be 'entra' or 'password'.")

    pool = ConnectionPool(
        conninfo="",
        kwargs=kwargs,
        min_size=1,
        max_size=int(environ.get("PG_POOL_MAX_SIZE", "4")),
        open=False,
        timeout=5,
        # Recycle connections well before Entra access tokens expire.
        max_lifetime=1800,
        check=ConnectionPool.check_connection,
        name="weather-db",
    )
    return Storage(pool, int(environ.get("ACTIVITY_RETENTION_DAYS", "90")))
