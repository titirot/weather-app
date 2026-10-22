import base64
import json
import unittest
from unittest.mock import Mock, patch

from app import create_app
from auth import AuthConfigurationError, load_auth_mode
from storage import FavoriteLimitReached, StorageUnavailable

LOCATION = {"name": "Stockholm", "country": "SE", "lat": 59.33, "lon": 18.07}
CURRENT = {
    "dt": 1792584000,
    "timezone": 7200,
    "main": {"temp": 10, "humidity": 60},
    "weather": [{"id": 800, "description": "clear sky"}],
}
FETCH_HEADERS = {"X-Requested-With": "fetch"}


def provider_response(payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def provider_success():
    return [
        provider_response([LOCATION]),
        provider_response(CURRENT),
        provider_response({"list": [CURRENT]}),
    ]


class FakeStorage:
    def __init__(self):
        self.favorites = {}
        self.calls = []
        self.fail = False
        self.limit = False

    def _check(self):
        if self.fail:
            raise StorageUnavailable

    def list_favorites(self, user):
        self._check()
        return [f for f in self.favorites.values() if f["owner"] == user.external_id]

    def get_favorite(self, user, favorite_id):
        self._check()
        favorite = self.favorites.get(favorite_id)
        return favorite if favorite and favorite["owner"] == user.external_id else None

    def save_favorite(self, user, city, weather):
        self._check()
        if self.limit:
            raise FavoriteLimitReached
        self.calls.append(("save", user, city, weather))
        favorite_id = len(self.favorites) + 1
        self.favorites[favorite_id] = {
            "id": favorite_id,
            "owner": user.external_id,
            "location": weather["location"],
        }
        return self.favorites[favorite_id], True

    def delete_favorite(self, user, favorite_id):
        self._check()
        if self.get_favorite(user, favorite_id):
            del self.favorites[favorite_id]
            return True
        return False

    def list_activity(self, user):
        self._check()
        return []

    def delete_user_data(self, user):
        self._check()
        self.calls.append(("delete_user", user))
        return True


def easyauth_headers(principal_id="abc-123", name="Dana"):
    claims = {"claims": [{"typ": "name", "val": name}]}
    return {
        "X-MS-CLIENT-PRINCIPAL-ID": principal_id,
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
    }


class AppFeatureTests(unittest.TestCase):
    def make_client(self, environ=None, storage=None):
        env = {"OPENWEATHER_API_KEY": "test-key", "AUTH_MODE": "local"}
        env.update(environ or {})
        self.app = create_app(environ=env, storage=storage)
        return self.app.test_client()

    @patch("app.requests.get")
    def test_weather_cache_avoids_repeated_provider_calls(self, get):
        get.side_effect = provider_success()
        client = self.make_client()
        first = client.get("/api/weather?city=Stockholm")
        second = client.get("/api/weather?city=  stockholm ")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(get.call_count, 3)
        self.assertEqual(first.get_json()["timezone_offset"], 7200)

    @patch("app.requests.get")
    def test_refresh_bypasses_and_updates_weather_cache(self, get):
        updated = {**CURRENT, "main": {"temp": 22, "humidity": 50}}
        get.side_effect = provider_success() + [
            provider_response([LOCATION]),
            provider_response(updated),
            provider_response({"list": [updated]}),
        ]
        client = self.make_client()
        client.get("/api/weather?city=Stockholm")
        fresh = client.get("/api/weather?city=Stockholm&refresh=true")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.get_json()["current"]["main"]["temp"], 22)
        cached = client.get("/api/weather?city=Stockholm")
        self.assertEqual(cached.get_json(), fresh.get_json())
        self.assertEqual(get.call_count, 6)

    @patch("app.requests.get")
    def test_cache_can_be_disabled(self, get):
        get.side_effect = provider_success() + provider_success()
        client = self.make_client({"WEATHER_CACHE_SECONDS": "0"})
        client.get("/api/weather?city=Stockholm")
        client.get("/api/weather?city=Stockholm")
        self.assertEqual(get.call_count, 6)

    def test_index_healthz_and_security_headers(self):
        client = self.make_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"SkyCast", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-cache")
        csp = page.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("max-age", page.headers["Strict-Transport-Security"])
        page.close()
        health = client.get("/healthz")
        self.assertEqual(health.get_json(), {"status": "ok"})
        self.assertEqual(health.headers["Cache-Control"], "no-store")

    def test_auth_mode_defaults_to_easyauth_and_local_is_refused_on_app_service(self):
        self.assertEqual(load_auth_mode({}), "easyauth")
        with self.assertRaises(AuthConfigurationError):
            load_auth_mode({"AUTH_MODE": "local", "WEBSITE_SITE_NAME": "prod-app"})
        with self.assertRaises(AuthConfigurationError):
            load_auth_mode({"AUTH_MODE": "none"})

    def test_easyauth_user_and_spoofed_headers(self):
        storage = FakeStorage()
        client = self.make_client(
            {"AUTH_MODE": "easyauth", "WEBSITE_AUTH_ENABLED": "True"}, storage
        )
        me = client.get("/api/me", headers=easyauth_headers()).get_json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["name"], "Dana")
        self.assertTrue(me["login_url"].startswith("/.auth/login/aad"))
        anonymous = client.get("/api/me").get_json()
        self.assertFalse(anonymous["authenticated"])
        self.assertEqual(client.get("/api/favorites").status_code, 401)

        # Without App Service authentication enabled, identity headers are untrusted.
        spoof_client = self.make_client({"AUTH_MODE": "easyauth"}, storage)
        response = spoof_client.get("/api/favorites", headers=easyauth_headers())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "authentication_required")

    @patch("app.requests.get")
    def test_save_uses_server_side_weather_and_user_isolation(self, get):
        get.side_effect = provider_success()
        storage = FakeStorage()
        env = {"AUTH_MODE": "easyauth", "WEBSITE_AUTH_ENABLED": "true"}
        client = self.make_client(env, storage)
        response = client.post(
            "/api/favorites",
            json={"city": "Stockholm", "forecast": {"forged": True}},
            headers={**FETCH_HEADERS, **easyauth_headers("user-a")},
        )
        self.assertEqual(response.status_code, 201)
        _, user, city, weather = storage.calls[0]
        self.assertEqual(user.external_id, "aad:user-a")
        self.assertEqual(city, "Stockholm")
        self.assertNotIn("forged", weather)
        self.assertEqual(weather["location"], LOCATION)

        other = easyauth_headers("user-b")
        self.assertEqual(client.get("/api/favorites", headers=other).get_json()["favorites"], [])
        self.assertEqual(client.get("/api/favorites/1", headers=other).status_code, 404)
        self.assertEqual(
            client.delete("/api/favorites/1", headers={**FETCH_HEADERS, **other}).status_code, 404
        )
        owner = {**FETCH_HEADERS, **easyauth_headers("user-a")}
        self.assertEqual(client.delete("/api/favorites/1", headers=owner).status_code, 204)

    def test_csrf_protection_on_state_changing_requests(self):
        storage = FakeStorage()
        client = self.make_client(storage=storage)
        self.assertEqual(client.post("/api/favorites", json={"city": "X"}).status_code, 403)
        cross_site = {**FETCH_HEADERS, "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(
            client.delete("/api/me", headers=cross_site).status_code, 403
        )
        self.assertEqual(storage.calls, [])
        self.assertEqual(client.delete("/api/me", headers=FETCH_HEADERS).status_code, 204)
        self.assertEqual(storage.calls[0][0], "delete_user")

    def test_invalid_save_bodies(self):
        client = self.make_client(storage=FakeStorage())
        for body in ("not-json", json.dumps([1]), json.dumps({"city": ""}), json.dumps({"city": 5})):
            with self.subTest(body=body):
                response = client.post(
                    "/api/favorites",
                    data=body,
                    content_type="application/json",
                    headers=FETCH_HEADERS,
                )
                self.assertEqual(response.status_code, 400)
        too_large = client.post(
            "/api/favorites",
            json={"city": "x" * 20000},
            headers=FETCH_HEADERS,
        )
        self.assertEqual(too_large.status_code, 413)

    @patch("app.requests.get")
    def test_storage_unavailable_and_unconfigured(self, get):
        storage = FakeStorage()
        storage.fail = True
        client = self.make_client(storage=storage)
        response = client.get("/api/favorites")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "database_unavailable")

        no_db = self.make_client(storage=None)
        self.assertEqual(no_db.get("/api/favorites").status_code, 503)
        self.assertFalse(no_db.get("/api/me").get_json()["storage_available"])
        # Weather keeps working without a database.
        get.side_effect = provider_success()
        self.assertEqual(no_db.get("/api/weather?city=Stockholm").status_code, 200)

    @patch("app.requests.get")
    def test_favorite_limit(self, get):
        get.side_effect = provider_success()
        storage = FakeStorage()
        storage.limit = True
        client = self.make_client(storage=storage)
        response = client.post("/api/favorites", json={"city": "Stockholm"}, headers=FETCH_HEADERS)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "favorite_limit_reached")


if __name__ == "__main__":
    unittest.main()
