"""Terms of Service: public page, login notice, setup agreement, accept gate."""
from unittest import mock

import pytest

from app import db
from app.models import AppSetting


def _setting(app, key):
    with app.app_context():
        row = db.session.get(AppSetting, key)
        return row.value if row else None


@pytest.fixture
def client(app_factory, fpp):
    app = app_factory()
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    client.app = app
    return client


def test_terms_page_is_public(app_factory, fpp):
    resp = app_factory().test_client().get("/terms")
    assert resp.status_code == 200
    assert b"Terms of Service" in resp.data
    assert b"Authorized Reseller" in resp.data


def test_terms_page_readable_before_first_run_setup(app_factory, fpp):
    resp = app_factory(provisioned=False).test_client().get("/terms")
    assert resp.status_code == 200


def test_login_page_states_the_agreement_with_a_link(app_factory, fpp):
    resp = app_factory().test_client().get("/login")
    assert b"By logging in you agree to the" in resp.data
    assert b'href="/terms"' in resp.data


def test_setup_requires_the_agreement(app_factory, fpp):
    app = app_factory(provisioned=False)
    client = app.test_client()
    resp = client.post("/setup", data={"pin": "4321", "confirm": "4321"})
    assert resp.status_code == 200
    assert b"agree to the Terms of Service" in resp.data
    assert app.config["ADMIN_PASSWORD_HASH"] == ""


def test_setup_records_the_acceptance(app_factory, fpp, real_terms):
    from app import terms
    app = app_factory(provisioned=False)
    client = app.test_client()
    with mock.patch("app.routes.auth.write_env_key", side_effect=lambda k, v: app.config.__setitem__(k, v)):
        resp = client.post("/setup", data={"pin": "4321", "confirm": "4321", "agree": "1"})
    assert resp.status_code == 302
    assert _setting(app, "terms_accepted_version") == terms.TERMS_VERSION
    assert _setting(app, "terms_accepted_via") == "setup"
    assert _setting(app, "terms_accepted_at").endswith("Z")
    assert client.get("/api/zones").status_code == 200   # not gated afterwards


def test_unaccepted_install_is_sent_to_the_accept_screen(client, real_terms):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/terms/accept")
    resp = client.get("/api/zones")
    assert resp.status_code == 403 and resp.get_json()["terms_required"] is True
    assert b"I have read and agree" in client.get("/terms/accept").data


def test_accepting_unlocks_the_app_and_records_it(client, real_terms):
    from app import terms
    assert client.post("/terms/accept", data={}).status_code == 200      # box not ticked
    assert _setting(client.app, "terms_accepted_version") is None
    resp = client.post("/terms/accept", data={"agree": "1"}, follow_redirects=False)
    assert resp.status_code == 302
    assert _setting(client.app, "terms_accepted_version") == terms.TERMS_VERSION
    assert _setting(client.app, "terms_accepted_via") == "login"
    assert client.get("/settings").status_code == 200
    assert client.get("/api/zones").status_code == 200
    # Already accepted: the screen steps aside.
    assert client.get("/terms/accept", follow_redirects=False).status_code == 302


def test_new_terms_version_asks_again(client, real_terms):
    client.post("/terms/accept", data={"agree": "1"})
    assert client.get("/settings").status_code == 200
    with mock.patch.object(real_terms, "TERMS_VERSION", "2099-01-01"):
        assert client.get("/settings", follow_redirects=False).status_code == 302


def test_fpp_playlist_callbacks_are_not_gated(app_factory, fpp, real_terms):
    # /internal/ calls authenticate by token, so an unaccepted install must
    # not stop scheduled scenes from running.
    resp = app_factory().test_client().get("/internal/scene/999/apply?token=tok-abc")
    assert resp.status_code == 404


def test_accept_screen_requires_login(app_factory, fpp, real_terms):
    resp = app_factory().test_client().get("/terms/accept", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


def test_reset_terms_requires_a_master_session(client, real_terms):
    client.post("/terms/accept", data={"agree": "1"})
    resp = client.post("/api/reset-terms")
    assert resp.status_code == 403
    assert _setting(client.app, "terms_accepted_version") == real_terms.TERMS_VERSION


def test_master_reset_clears_acceptance_and_signs_out(client, real_terms):
    client.post("/terms/accept", data={"agree": "1"})
    with client.session_transaction() as s:
        s["is_master"] = True
    assert client.post("/api/reset-terms").status_code == 200
    for key in ("terms_accepted_version", "terms_accepted_at", "terms_accepted_via"):
        assert _setting(client.app, key) is None
    # Signed out; the next login (the client) must agree again.
    assert client.get("/settings", follow_redirects=False).headers["Location"].endswith("/login")
    with client.session_transaction() as s:
        s["logged_in"] = True
    assert client.get("/settings", follow_redirects=False).headers["Location"].endswith("/terms/accept")
