import io
import os
import unittest

# Must be set before app.config is imported: a throwaway DB, an FPP address that
# refuses connections, and a provisioned admin so the setup redirect is off.
os.environ.update(
    DATABASE_URL="sqlite://",
    SECRET_KEY="test-secret",
    ADMIN_PASSWORD_HASH="x",
    INTERNAL_TOKEN="tok-abc",
    FPP_BASE_URL="http://127.0.0.1:9/api",
    FPP_MEDIA_ROOT=os.path.join(os.path.dirname(__file__), "_no_media"),
)

from app import create_app  # noqa: E402
from app import uploads  # noqa: E402
from app.routes.auth import _failed_logins  # noqa: E402
from app.security import _rate_hits  # noqa: E402


class SecurityCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = False  # keep the real error handlers

    def setUp(self):
        _rate_hits.clear()
        _failed_logins.clear()
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s["logged_in"] = True


class Csrf(SecurityCase):
    def test_cross_site_post_is_blocked(self):
        self.login()
        r = self.client.post("/api/change-pin", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_cross_site_referer_is_blocked(self):
        self.login()
        r = self.client.post("/api/change-pin", headers={"Referer": "http://evil.example/x"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_passes_the_check(self):
        self.login()
        r = self.client.post("/api/change-pin", headers={"Origin": "http://localhost"})
        self.assertNotEqual(r.status_code, 403)

    def test_forwarded_host_counts_as_same_origin(self):
        self.login()
        r = self.client.post(
            "/api/change-pin",
            headers={"Origin": "http://10.0.0.5", "X-Forwarded-Host": "10.0.0.5"},
        )
        self.assertNotEqual(r.status_code, 403)

    def test_spoofed_leading_forwarded_host_is_ignored(self):
        self.login()
        r = self.client.post(
            "/api/change-pin",
            headers={"Origin": "http://evil.example",
                     "X-Forwarded-Host": "evil.example, 10.0.0.5"},
        )
        self.assertEqual(r.status_code, 403)

    def test_no_origin_header_is_allowed(self):
        self.login()
        r = self.client.post("/api/change-pin")
        self.assertNotEqual(r.status_code, 403)

    def test_stock_apache_without_forwarded_host_accepts_a_lan_origin(self):
        """Apache hands Flask Host 127.0.0.1:5000 and no X-Forwarded-Host."""
        self.login()
        for origin in ("http://10.27.200.118", "http://192.168.8.1", "http://backpack", "http://pi.local"):
            r = self.client.post("/api/change-pin", headers={"Origin": origin, "Host": "127.0.0.1:5000"})
            self.assertNotEqual(r.status_code, 403, origin)

    def test_stock_apache_still_refuses_public_origins(self):
        self.login()
        for origin in ("http://evil.example", "https://attacker.com", "http://8.8.8.8", "null"):
            r = self.client.post("/api/change-pin", headers={"Origin": origin, "Host": "127.0.0.1:5000"})
            self.assertEqual(r.status_code, 403, origin)

    def test_dataplicity_origin_is_accepted(self):
        """Dataplicity rewrites Host, so the wormhole address never matches it."""
        self.login()
        for host in ("127.0.0.1:5000", "localhost"):
            r = self.client.post(
                "/api/change-pin",
                headers={"Origin": "https://dowerless-seal-6034.dataplicity.io", "Host": host},
            )
            self.assertNotEqual(r.status_code, 403, host)

    def test_lookalike_dataplicity_origins_are_refused(self):
        self.login()
        for origin in ("https://dataplicity.io.evil.example", "https://evildataplicity.io",
                       "https://dataplicity.io.attacker.com:443"):
            r = self.client.post("/api/change-pin",
                                 headers={"Origin": origin, "Host": "127.0.0.1:5000"})
            self.assertEqual(r.status_code, 403, origin)

    def test_get_is_never_blocked(self):
        self.login()
        r = self.client.get("/api/zones", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 200)

    def test_session_cookie_is_lax_and_httponly(self):
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])


class Headers(SecurityCase):
    def test_security_headers_present(self):
        r = self.client.get("/login")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")

    def test_api_errors_are_json(self):
        self.login()
        r = self.client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_api_responses_not_cached(self):
        self.login()
        self.assertEqual(self.client.get("/api/zones").headers["Cache-Control"], "no-store")


class Auth(SecurityCase):
    PUBLIC_ENDPOINTS = {"static", "auth.login", "auth.logout", "auth.setup",
                        "scenes.internal_apply_scene", "effects.internal_apply_effect"}

    def test_every_other_route_requires_a_session(self):
        """No route is reachable logged-out unless it is on the public list."""
        checked = 0
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in self.PUBLIC_ENDPOINTS:
                continue
            path = rule.rule
            for arg in rule.arguments:
                path = path.replace(f"<int:{arg}>", "1").replace(f"<path:{arg}>", "x").replace(f"<{arg}>", "x")
            for method in rule.methods - {"HEAD", "OPTIONS"}:
                r = self.client.open(path, method=method)
                self.assertEqual(r.status_code, 302, f"{method} {path} ({rule.endpoint}) is reachable without login")
                checked += 1
        self.assertGreater(checked, 50)

    def test_internal_rejects_non_ascii_token_without_crashing(self):
        r = self.client.get("/internal/scene/1/apply?token=%C3%A9")
        self.assertEqual(r.status_code, 403)

    def test_internal_rejects_wrong_and_missing_token(self):
        self.assertEqual(self.client.get("/internal/scene/1/apply?token=nope").status_code, 403)
        self.assertEqual(self.client.get("/internal/scene/1/apply").status_code, 403)

    def test_internal_accepts_right_token(self):
        r = self.client.get("/internal/scene/999/apply?token=tok-abc")
        self.assertEqual(r.status_code, 404)  # got past auth; the scene is absent

    def test_change_pin_is_throttled_after_repeated_failures(self):
        self.login()
        codes = [
            self.client.post("/api/change-pin", json={"current_pin": "0000", "new_pin": "1234"}).status_code
            for _ in range(9)
        ]
        self.assertEqual(codes[0], 400)
        self.assertIn(429, codes)


class Validation(SecurityCase):
    def test_non_object_bodies_are_400_not_500(self):
        self.login()
        for path in ("/api/scenes", "/api/colors/save", "/api/holidays", "/api/custom-playlists"):
            for body in ([1], "text", 5, None):
                r = self.client.post(path, json=body)
                self.assertLess(r.status_code, 500, (path, body, r.status_code))

    def test_non_string_name_is_rejected(self):
        self.login()
        r = self.client.post("/api/scenes", json={"name": 123, "zones": {"Zone 1": "#ffffff"}})
        self.assertEqual(r.status_code, 400)

    def test_zone_list_with_junk_items(self):
        self.login()
        r = self.client.post("/api/zones", json=[1, "x", None, {"slot": 1, "display_name": "Roof"}])
        self.assertEqual(r.status_code, 200)

    def test_restore_chunk_cannot_exceed_declared_total(self):
        self.login()
        uid = "ab" * 8
        r = self.client.post(f"/api/restore/chunk?id={uid}&offset=0&total=4", data=b"12345678",
                             content_type="application/octet-stream")
        self.assertEqual(r.status_code, 413)

    def test_restore_chunk_rejects_absurd_total(self):
        self.login()
        uid = "cd" * 8
        r = self.client.post(f"/api/restore/chunk?id={uid}&offset=0&total={10 ** 15}", data=b"x",
                             content_type="application/octet-stream")
        self.assertEqual(r.status_code, 400)


class RateLimit(SecurityCase):
    def test_reboot_is_rate_limited(self):
        self.login()
        codes = [self.client.post("/api/system/reboot").status_code for _ in range(5)]
        self.assertIn(429, codes)
        self.assertEqual(codes.count(429), 2)


class ImageChecks(unittest.TestCase):
    def test_valid_images_pass(self):
        self.assertIsNone(uploads.image_problem("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 10))
        self.assertIsNone(uploads.image_problem("a.jpg", b"\xff\xd8\xff\xe0data"))
        self.assertIsNone(uploads.image_problem("a.gif", b"GIF89a...."))
        self.assertIsNone(uploads.image_problem("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "))
        self.assertIsNone(uploads.image_problem("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'))

    def test_disguised_html_is_rejected(self):
        self.assertIsNotNone(uploads.image_problem("a.png", b"<html><script>alert(1)</script>"))

    def test_scripted_svgs_are_rejected(self):
        for body in (
            b"<svg><script>alert(1)</script></svg>",
            b'<svg onload="alert(1)"></svg>',
            b'<svg><a href="javascript:alert(1)"><rect/></a></svg>',
            b"<svg><foreignObject><div/></foreignObject></svg>",
        ):
            self.assertIsNotNone(uploads.image_problem("a.svg", body), body)

    def test_empty_and_oversize_rejected(self):
        self.assertIsNotNone(uploads.image_problem("a.png", b""))
        self.assertIsNotNone(uploads.image_problem("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * uploads.MAX_IMAGE_BYTES))
        self.assertIsNotNone(uploads.image_problem("a.exe", b"MZ"))


class UploadEndpoint(SecurityCase):
    def test_scripted_svg_upload_is_refused(self):
        self.login()
        r = self.client.post(
            "/api/upload/image?type=logo",
            data={"file": (io.BytesIO(b"<svg><script>1</script></svg>"), "x.svg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
