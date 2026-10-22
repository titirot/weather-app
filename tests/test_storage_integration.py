"""Integration tests against a real PostgreSQL (e.g. docker compose up -d).

Set TEST_DATABASE_URL, for example:
postgresql://weather_app:<password>@localhost:5432/weather_test
The tests create and drop their own tables in that database.
"""

import os
import threading
import unittest

from auth import User

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def weather(name="Stockholm", lat=59.33, lon=18.07, temp=10):
    return {
        "location": {"name": name, "country": "SE", "lat": lat, "lon": lon},
        "current": {
            "dt": 1792584000,
            "main": {"temp": temp, "feels_like": temp - 1, "humidity": 60},
            "weather": [{"id": 800, "description": "clear sky"}],
        },
        "forecast": {"interval_hours": 3, "items": []},
    }


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL is not set")
class StorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg_pool import ConnectionPool

        import storage

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS activity_log, saved_forecasts, app_users")
        self.storage_module = storage
        self.pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=6, open=False, timeout=5)
        self.storage = storage.Storage(self.pool)
        self.alice = User("aad:alice", "Alice")
        self.bob = User("aad:bob", "Bob")

    def tearDown(self):
        self.storage.close()

    def test_save_list_update_get_delete_and_activity(self):
        favorite, created = self.storage.save_favorite(self.alice, "Stockholm", weather())
        self.assertTrue(created)
        self.assertEqual(favorite["summary"]["temp"], 10)

        updated, created = self.storage.save_favorite(self.alice, "stockholm", weather(temp=12))
        self.assertFalse(created)
        self.assertEqual(updated["id"], favorite["id"])

        listed = self.storage.list_favorites(self.alice)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["summary"]["temp"], 12)
        full = self.storage.get_favorite(self.alice, favorite["id"])
        self.assertEqual(full["forecast"]["location"]["name"], "Stockholm")

        actions = [entry["action"] for entry in self.storage.list_activity(self.alice)]
        self.assertEqual(actions, ["forecast_updated", "forecast_saved", "account_created"])

        self.assertTrue(self.storage.delete_favorite(self.alice, favorite["id"]))
        self.assertEqual(self.storage.list_favorites(self.alice), [])
        self.assertEqual(self.storage.list_activity(self.alice)[0]["action"], "forecast_deleted")

    def test_users_are_isolated(self):
        favorite, _ = self.storage.save_favorite(self.alice, "Stockholm", weather())
        self.assertEqual(self.storage.list_favorites(self.bob), [])
        self.assertIsNone(self.storage.get_favorite(self.bob, favorite["id"]))
        self.assertFalse(self.storage.delete_favorite(self.bob, favorite["id"]))
        self.assertEqual(self.storage.list_activity(self.bob), [])
        self.assertEqual(len(self.storage.list_favorites(self.alice)), 1)

    def test_delete_user_data_cascades(self):
        self.storage.save_favorite(self.alice, "Stockholm", weather())
        self.storage.save_favorite(self.bob, "Oslo", weather("Oslo", 59.91, 10.75))
        self.assertTrue(self.storage.delete_user_data(self.alice))
        self.assertEqual(self.storage.list_favorites(self.alice), [])
        self.assertEqual(self.storage.list_activity(self.alice), [])
        self.assertEqual(len(self.storage.list_favorites(self.bob)), 1)

    def test_limit_is_enforced_under_concurrency(self):
        limit = self.storage_module.MAX_SAVED_FORECASTS
        for i in range(limit - 2):
            self.storage.save_favorite(self.alice, f"City{i}", weather(f"City{i}", i, i))
        results = []

        def save(i):
            try:
                self.storage.save_favorite(self.alice, f"New{i}", weather(f"New{i}", 80, i))
                results.append("ok")
            except self.storage_module.FavoriteLimitReached:
                results.append("limit")

        threads = [threading.Thread(target=save, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(len(self.storage.list_favorites(self.alice)), limit)
        # Re-saving an existing city is still allowed at the limit.
        _, created = self.storage.save_favorite(self.alice, "City0", weather("City0", 0, 0))
        self.assertFalse(created)

    def test_unreachable_database_raises_storage_unavailable(self):
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1",
            min_size=1,
            open=False,
            timeout=1,
        )
        broken = self.storage_module.Storage(pool)
        try:
            with self.assertRaises(self.storage_module.StorageUnavailable):
                broken.list_favorites(self.alice)
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
