import io
import logging
import unittest
from xml.etree import ElementTree
from unittest.mock import Mock, patch

import requests

from app import create_app


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            environ={"OPENWEATHER_API_KEY": "test-key-not-real", "AUTH_MODE": "local"},
            storage=None,
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.location = {
            "name": "London",
            "country": "GB",
            "lat": 51.507,
            "lon": -0.128,
        }
        self.current = {
            "dt": 1792584000,
            "main": {"temp": 15, "humidity": 70},
            "weather": [{"description": "clear sky"}],
        }

    def response(self, payload, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_custom_favicon_is_linked_and_served_as_svg(self):
        with self.client.get("/") as page:
            self.assertEqual(page.status_code, 200)
            self.assertIn(
                '<link rel="icon" type="image/svg+xml" sizes="any" href="/static/favicon.svg">',
                page.get_data(as_text=True),
            )
        with self.client.get("/static/favicon.svg") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/svg+xml")
            icon = ElementTree.fromstring(response.data)
            self.assertEqual(icon.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertEqual(icon.attrib["viewBox"], "0 0 64 64")
            self.assertNotIn(b"<script", response.data)

    def successful_responses(self):
        return [
            self.response([self.location]),
            self.response(self.current),
            self.response({"list": [self.current]}),
        ]

    @patch("app.requests.get")
    def test_success_and_secure_provider_calls(self, get):
        get.side_effect = self.successful_responses()
        response = self.client.get("/api/weather?city=London,GB")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["location"], self.location)
        self.assertEqual(data["current"], self.current)
        self.assertIsInstance(data["fetched_at"], int)
        self.assertEqual(data["units"], "metric")
        self.assertEqual(data["forecast"]["interval_hours"], 3)
        self.assertEqual(data["forecast"]["items"], [self.current])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["q"], "London,GB")
        for call in get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://api.openweathermap.org/"))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], (3.05, 8))
            self.assertEqual(call.kwargs["params"]["appid"], "test-key-not-real")
        self.assertNotIn("test-key-not-real", response.get_data(as_text=True))

    @patch("app.requests.get")
    def test_invalid_inputs_do_not_call_provider(self, get):
        for query in (
            "",
            "?city=",
            "?city=%20%20",
            "?city=A&city=B",
            "?city=" + "a" * 101,
            "?city=London%0A",
            "?city=%09London",
            "?city=Lon%00don",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/weather" + query).status_code, 400)
        get.assert_not_called()

    @patch("app.requests.get")
    def test_unicode_city(self, get):
        get.side_effect = self.successful_responses()
        response = self.client.get(
            "/api/weather", query_string={"city": "\u05d7\u05d9\u05e4\u05d4"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["q"], "\u05d7\u05d9\u05e4\u05d4")

    @patch("app.requests.get")
    def test_unknown_city(self, get):
        get.return_value = self.response([])
        response = self.client.get("/api/weather?city=UnknownCity")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "city_not_found")
        self.assertEqual(get.call_count, 1)

    @patch("app.requests.get")
    def test_missing_key(self, get):
        client = create_app(environ={"AUTH_MODE": "local"}, storage=None).test_client()
        response = client.get("/api/weather?city=London")
        self.assertEqual(response.status_code, 503)
        get.assert_not_called()

    @patch("app.requests.get")
    def test_provider_statuses(self, get):
        for upstream, expected, code in (
            (401, 503, "weather_configuration_error"),
            (403, 503, "weather_configuration_error"),
            (404, 404, "city_not_found"),
            (429, 503, "weather_rate_limit"),
            (500, 502, "weather_provider_error"),
            (302, 502, "weather_provider_error"),
        ):
            with self.subTest(status=upstream):
                get.return_value = self.response({"message": "private-provider-body"}, upstream)
                response = self.client.get("/api/weather?city=London")
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.get_json()["error"]["code"], code)
                self.assertNotIn("private-provider-body", response.get_data(as_text=True))

    @patch("app.requests.get")
    def test_network_failures_do_not_leak_secrets_or_city_to_logs(self, get):
        for exception, expected in (
            (requests.exceptions.Timeout, 504),
            (requests.exceptions.ConnectionError, 502),
            (requests.exceptions.SSLError, 502),
        ):
            with self.subTest(exception=exception):
                get.side_effect = exception("appid=test-key-not-real&q=PrivateCity")
                output = io.StringIO()
                handler = logging.StreamHandler(output)
                self.app.logger.addHandler(handler)
                try:
                    response = self.client.get("/api/weather?city=PrivateCity")
                finally:
                    self.app.logger.removeHandler(handler)
                self.assertEqual(response.status_code, expected)
                combined = output.getvalue() + response.get_data(as_text=True)
                self.assertNotIn("test-key-not-real", combined)
                self.assertNotIn("PrivateCity", combined)

    @patch("app.requests.get")
    def test_invalid_json(self, get):
        get.return_value = self.response(None)
        get.return_value.json.side_effect = ValueError("private-provider-body")
        response = self.client.get("/api/weather?city=London")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["code"], "weather_invalid_response")

    @patch("app.requests.get")
    def test_invalid_provider_shapes(self, get):
        for payload in (None, "invalid", {}, [None], [{"name": "London"}]):
            with self.subTest(payload=payload):
                get.side_effect = None
                get.return_value = self.response(payload)
                self.assertEqual(
                    self.client.get("/api/weather?city=London").status_code, 502
                )

    @patch("app.requests.get")
    def test_invalid_current_and_forecast(self, get):
        for responses in (
            [self.response([self.location]), self.response({})],
            [self.response([self.location]), self.response(self.current), self.response({})],
            [self.response([self.location]), self.response(self.current), self.response({"list": []})],
            [self.response([self.location]), self.response(self.current), self.response({"list": [None]})],
        ):
            with self.subTest(responses=responses):
                get.side_effect = responses
                response = self.client.get("/api/weather?city=London")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.get_json()["error"]["code"], "weather_invalid_response")

    @patch("app.requests.get")
    def test_quota_exhausted_during_forecast(self, get):
        get.side_effect = [
            self.response([self.location]),
            self.response(self.current),
            self.response({}, 429),
        ]
        response = self.client.get("/api/weather?city=London")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("current", response.get_json())

    def test_headers_and_json_http_errors(self):
        for method, path, expected in (
            ("get", "/api/weather", 400),
            ("get", "/missing", 404),
            ("post", "/api/weather", 405),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, expected)
                self.assertTrue(response.is_json)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertFalse(self.app.debug)

    @patch("app.requests.get")
    def test_trimmed_city_at_length_limit(self, get):
        get.side_effect = self.successful_responses()
        response = self.client.get(
            "/api/weather", query_string={"city": " " + "a" * 100 + " "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["q"], "a" * 100)


if __name__ == "__main__":
    unittest.main()
