import os
import unittest
from unittest import mock

os.environ.update(
    DATABASE_URL="sqlite://",
    SECRET_KEY="test-secret",
    ADMIN_PASSWORD_HASH="x",
    INTERNAL_TOKEN="tok",
    FPP_BASE_URL="http://127.0.0.1:9/api",
    FPP_MEDIA_ROOT=os.path.join(os.path.dirname(__file__), "_no_media"),
)

from sqlalchemy import event, inspect  # noqa: E402

from app import _add_missing_indexes, create_app, db  # noqa: E402
from app.models import CustomPlaylist, CustomPlaylistItem, Scene, SceneZone  # noqa: E402
from app.routes import playlists as playlists_routes  # noqa: E402


class PerfCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        with cls.app.app_context():
            for i in range(30):
                scene = Scene(name=f"S{i}")
                scene.zones = [SceneZone(fpp_model=f"Zone {z + 1}", hex_color="#ff0000") for z in range(5)]
                db.session.add(scene)
            for p in range(10):
                cp = CustomPlaylist(name=f"P{p}")
                cp.items = [
                    CustomPlaylistItem(position=n, item_type="scene", ref_id=n + 1, duration=10)
                    for n in range(8)
                ]
                db.session.add(cp)
            db.session.commit()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True
        playlists_routes.invalidate_status_cache()

    def count_queries(self, path):
        counter = {"n": 0}

        def hook(*_args):
            counter["n"] += 1

        with self.app.app_context():
            event.listen(db.engine, "before_cursor_execute", hook)
            try:
                response = self.client.get(path)
            finally:
                event.remove(db.engine, "before_cursor_execute", hook)
        return response, counter["n"]


class QueryCounts(PerfCase):
    def test_scene_list_is_not_n_plus_one(self):
        response, n = self.count_queries("/api/scenes")
        self.assertEqual(len(response.get_json()), 30)
        self.assertLessEqual(n, 3)   # was 1 + one per scene

    def test_playlist_list_is_not_n_plus_one(self):
        response, n = self.count_queries("/api/custom-playlists")
        data = response.get_json()
        self.assertEqual(len(data), 10)
        self.assertEqual(len(data[0]["items"]), 8)
        self.assertLessEqual(n, 6)   # was 1 + one per playlist + one per item

    def test_playlist_summary_is_a_single_query(self):
        response, n = self.count_queries("/api/custom-playlists?summary=1")
        self.assertEqual(n, 1)
        self.assertEqual(set(response.get_json()[0]), {"id", "name"})


class Paging(PerfCase):
    def test_limit_and_offset(self):
        page = self.client.get("/api/scenes?limit=5&offset=10").get_json()
        self.assertEqual([s["name"] for s in page], [f"S{i}" for i in range(10, 15)])

    def test_bad_or_huge_values_are_clamped(self):
        self.assertEqual(len(self.client.get("/api/scenes?limit=abc&offset=-4").get_json()), 30)
        self.assertEqual(len(self.client.get("/api/scenes?limit=0").get_json()), 1)
        self.assertEqual(len(self.client.get("/api/scenes?limit=99999999").get_json()), 30)


class Indexes(PerfCase):
    def test_foreign_key_indexes_exist(self):
        with self.app.app_context():
            names = {ix["name"] for t in ("scene_zones", "custom_playlist_items", "color_buttons")
                     for ix in inspect(db.engine).get_indexes(t)}
        self.assertTrue({"ix_scene_zones_scene_id", "ix_custom_playlist_items_playlist_id",
                         "ix_color_buttons_saved_color_id"} <= names)

    def test_index_migration_is_idempotent(self):
        with self.app.app_context():
            _add_missing_indexes(self.app)
            _add_missing_indexes(self.app)


class StatusCache(PerfCase):
    def fake_response(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"status_name": "idle"}
        return resp

    def test_concurrent_pollers_share_one_fppd_call(self):
        with mock.patch.object(playlists_routes.requests, "get", return_value=self.fake_response()) as get:
            for _ in range(5):
                self.assertEqual(self.client.get("/api/fppd/status").get_json(), {"status_name": "idle"})
        self.assertEqual(get.call_count, 1)

    def test_a_post_clears_the_cache(self):
        ok = self.fake_response()
        with mock.patch.object(playlists_routes.requests, "get", return_value=ok) as get,                 mock.patch.object(playlists_routes.requests, "put", return_value=ok),                 mock.patch.object(playlists_routes.requests, "post", return_value=ok):
            self.client.get("/api/fppd/status")
            self.client.post("/api/playlists/stop")
            get.reset_mock()
            self.client.get("/api/fppd/status")
        self.assertEqual(get.call_count, 1)

    def test_failures_are_not_cached(self):
        boom = playlists_routes.requests.ConnectionError("down")
        with mock.patch.object(playlists_routes.requests, "get", side_effect=boom):
            self.assertEqual(self.client.get("/api/fppd/status").status_code, 502)
        with mock.patch.object(playlists_routes.requests, "get", return_value=self.fake_response()):
            self.assertEqual(self.client.get("/api/fppd/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
