import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

import requests
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from auth import auth_links, current_user, load_auth_mode
from storage import FavoriteLimitReached, StorageUnavailable, create_storage_from_env


OPENWEATHER_BASE_URL = "https://api.openweathermap.org"
REQUEST_TIMEOUT = (3.05, 8)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
_UNSET = object()


class WeatherError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def invalid_provider_response() -> WeatherError:
    return WeatherError(
        "weather_invalid_response", "Invalid response from weather provider.", 502
    )


def fetch_openweather(path: str, params: dict, api_key: str) -> dict | list:
    try:
        # Never follow a redirect that could send the API key to another host.
        with requests.get(
            f"{OPENWEATHER_BASE_URL}{path}",
            params={**params, "appid": api_key},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        ) as response:
            if response.status_code == 429:
                raise WeatherError(
                    "weather_rate_limit",
                    "Weather provider quota exceeded. Please try again later.",
                    503,
                )
            if response.status_code in (401, 403):
                raise WeatherError(
                    "weather_configuration_error",
                    "Weather provider authentication is unavailable.",
                    503,
                )
            if response.status_code == 404:
                raise WeatherError("city_not_found", "City not found.", 404)
            if response.status_code != 200:
                raise WeatherError(
                    "weather_provider_error",
                    "Weather provider is temporarily unavailable.",
                    502,
                )
            data = response.json()
    except requests.exceptions.Timeout:
        raise WeatherError(
            "weather_timeout", "Weather provider timed out.", 504
        ) from None
    except requests.exceptions.RequestException:
        # Request exception strings can contain the URL, including appid.
        raise WeatherError(
            "weather_connection_error", "Cannot reach weather provider.", 502
        ) from None
    except ValueError:
        raise invalid_provider_response() from None

    if not isinstance(data, (dict, list)):
        raise invalid_provider_response()
    return data


def valid_weather_item(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("dt"), (int, float))
        and isinstance(item.get("main"), dict)
        and isinstance(item.get("weather"), list)
    )


def validate_city(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeatherError(
            "invalid_city", "Provide exactly one non-empty city parameter.", 400
        )
    city = value.strip()
    if len(city) > 100 or any(not char.isprintable() for char in value):
        raise WeatherError(
            "invalid_city",
            "City must be at most 100 characters without control characters.",
            400,
        )
    return city


class TTLCache:
    """Small thread-safe in-memory cache (per worker process)."""

    def __init__(self, ttl_seconds: float, max_entries: int):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


class WeatherService:
    def __init__(self, api_key: str, cache_seconds: float = 600, max_entries: int = 500):
        self._api_key = api_key
        # OpenWeather refreshes data roughly every 10 minutes; caching protects
        # the shared free-tier quota and reduces latency for popular cities.
        self._cache = TTLCache(cache_seconds, max_entries)

    def get(self, city: str, *, refresh: bool = False) -> dict:
        if not self._api_key:
            raise WeatherError(
                "weather_not_configured", "Weather service is not configured.", 503
            )
        cache_key = city.casefold()
        cached = None if refresh else self._cache.get(cache_key)
        if cached is not None:
            return cached

        locations = fetch_openweather(
            "/geo/1.0/direct", {"q": city, "limit": 1}, self._api_key
        )
        if not isinstance(locations, list):
            raise invalid_provider_response()
        if not locations:
            raise WeatherError("city_not_found", "City not found.", 404)
        location = locations[0]
        if (
            not isinstance(location, dict)
            or not isinstance(location.get("lat"), (int, float))
            or not isinstance(location.get("lon"), (int, float))
            or not isinstance(location.get("name"), str)
            or not isinstance(location.get("country"), str)
        ):
            raise invalid_provider_response()

        params = {"lat": location["lat"], "lon": location["lon"], "units": "metric"}
        current = fetch_openweather("/data/2.5/weather", params, self._api_key)
        if not valid_weather_item(current):
            raise invalid_provider_response()
        forecast = fetch_openweather("/data/2.5/forecast", params, self._api_key)
        if (
            not isinstance(forecast, dict)
            or not isinstance(forecast.get("list"), list)
            or not forecast["list"]
            or not all(valid_weather_item(item) for item in forecast["list"])
        ):
            raise invalid_provider_response()

        timezone_offset = current.get("timezone")
        result = {
            "provider": "OpenWeather",
            "units": "metric",
            "fetched_at": int(time.time()),
            "timezone_offset": timezone_offset if isinstance(timezone_offset, int) else 0,
            "location": {
                key: location[key]
                for key in ("name", "country", "state", "lat", "lon")
                if key in location
            },
            "current": current,
            "forecast": {"interval_hours": 3, "items": forecast["list"]},
        }
        self._cache.set(cache_key, result)
        return result


def create_app(environ: Mapping[str, str] | None = None, storage=_UNSET) -> Flask:
    environ = os.environ if environ is None else environ
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    auth_mode = load_auth_mode(environ)
    weather_service = WeatherService(
        environ.get("OPENWEATHER_API_KEY", "").strip(),
        float(environ.get("WEATHER_CACHE_SECONDS", "600")),
    )
    if storage is _UNSET:
        storage = create_storage_from_env(environ)
    app.extensions["weather_service"] = weather_service
    app.extensions["storage"] = storage

    def get_user():
        return current_user(auth_mode, request.headers, environ)

    def require_user():
        user = get_user()
        if user is None:
            raise WeatherError(
                "authentication_required", "Sign in to use saved forecasts.", 401
            )
        return user

    def require_storage():
        if storage is None:
            raise WeatherError(
                "storage_not_configured", "Saved forecasts are not available.", 503
            )
        return storage

    @app.before_request
    def reject_cross_site_writes():
        # State-changing calls require a custom header. Browsers cannot send it
        # cross-origin without a CORS preflight, which this app never allows.
        if request.url_rule is None or request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        site = request.headers.get("Sec-Fetch-Site", "same-origin")
        if request.headers.get("X-Requested-With") != "fetch" or site not in (
            "same-origin",
            "none",
        ):
            raise WeatherError("csrf_rejected", "Cross-site request rejected.", 403)
        return None

    @app.after_request
    def security_headers(response):
        is_page = request.path == "/" or request.path.startswith("/static/")
        response.headers["Cache-Control"] = "no-cache" if is_page else "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        return response

    @app.errorhandler(WeatherError)
    def handle_weather_error(error: WeatherError):
        # Only fixed internal codes are logged, never user input or provider bodies.
        app.logger.warning("Request failed: %s", error.code)
        return jsonify(error={"code": error.code, "message": error.message}), error.status

    @app.errorhandler(StorageUnavailable)
    def handle_storage_unavailable(_error):
        return handle_weather_error(
            WeatherError(
                "database_unavailable",
                "Saved forecasts are temporarily unavailable. Weather search still works.",
                503,
            )
        )

    @app.errorhandler(FavoriteLimitReached)
    def handle_favorite_limit(_error):
        return handle_weather_error(
            WeatherError(
                "favorite_limit_reached",
                "You can save up to 50 forecasts. Delete one before saving another.",
                409,
            )
        )

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        response = error.get_response()
        response.data = app.json.dumps(
            {"error": {"code": f"http_{error.code}", "message": error.name}}
        )
        response.content_type = "application/json"
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/healthz")
    def healthz():
        # Liveness only: a database outage must not take healthy web instances
        # out of rotation, because weather search works without the database.
        return jsonify(status="ok")

    @app.get("/api/weather")
    def weather():
        cities = request.args.getlist("city")
        if len(cities) != 1:
            raise WeatherError(
                "invalid_city", "Provide exactly one non-empty city parameter.", 400
            )
        return jsonify(weather_service.get(
            validate_city(cities[0]), refresh=request.args.get("refresh") == "true"
        ))

    @app.get("/api/me")
    def me():
        user = get_user()
        return jsonify(
            authenticated=user is not None,
            name=user.display_name if user else None,
            auth_mode=auth_mode,
            storage_available=storage is not None,
            **auth_links(auth_mode),
        )

    @app.delete("/api/me")
    def delete_me():
        user = require_user()
        require_storage().delete_user_data(user)
        return "", 204

    @app.get("/api/favorites")
    def list_favorites():
        user = require_user()
        return jsonify(favorites=require_storage().list_favorites(user))

    @app.post("/api/favorites")
    def save_favorite():
        user = require_user()
        db = require_storage()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise WeatherError("invalid_body", "Expected a JSON object.", 400)
        city = validate_city(body.get("city"))
        # The snapshot is fetched server-side so clients cannot store forged data.
        favorite, created = db.save_favorite(user, city, weather_service.get(city))
        return jsonify(favorite=favorite), 201 if created else 200

    @app.get("/api/favorites/<int:favorite_id>")
    def get_favorite(favorite_id: int):
        user = require_user()
        favorite = require_storage().get_favorite(user, favorite_id)
        if favorite is None:
            raise WeatherError("favorite_not_found", "Saved forecast not found.", 404)
        return jsonify(favorite=favorite)

    @app.delete("/api/favorites/<int:favorite_id>")
    def delete_favorite(favorite_id: int):
        user = require_user()
        if not require_storage().delete_favorite(user, favorite_id):
            raise WeatherError("favorite_not_found", "Saved forecast not found.", 404)
        return "", 204

    @app.get("/api/activity")
    def activity():
        user = require_user()
        return jsonify(activity=require_storage().list_activity(user))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
