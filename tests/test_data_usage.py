"""Data usage readout: counter differences, reboots, interface changes, and the routes."""
from datetime import date, datetime
from unittest import mock

import pytest

from app import data_usage


@pytest.fixture(autouse=True)
def fresh_state():
    data_usage.reset_for_tests()
    yield
    data_usage.reset_for_tests()


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app, fpp):
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    return client


class Counters:
    """Fake kernel counters: set .link / .tunnel, then call sample()."""

    def __init__(self):
        self.iface = "wlan0"
        self.link = (0, 0)
        self.tunnel = None

    def __enter__(self):
        def read(name):
            return self.tunnel if name == data_usage.TUNNEL_IFACE else self.link
        self._p = [mock.patch.object(data_usage, "default_interface", lambda: self.iface),
                   mock.patch.object(data_usage, "read_counters", read)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


def day(n=12, hour=10):
    return datetime(2026, 11, n, hour, 0)


def test_first_reading_counts_nothing_then_differences_add_up(app):
    with Counters() as c:
        c.link = (1000, 500)
        data_usage.sample(app, day())
        c.link = (3000, 900)
        data_usage.sample(app, day())
    s = data_usage.summary(app, today=date(2026, 11, 12))
    assert s["today"]["link"] == {"rx": 2000, "tx": 400, "total": 2400}


def test_reboot_counts_from_the_new_counter_not_negative(app):
    with Counters() as c:
        c.link = (5000, 5000)
        data_usage.sample(app, day())
        c.link = (100, 50)              # rebooted: counters restarted
        data_usage.sample(app, day())
    assert data_usage.summary(app, today=date(2026, 11, 12))["today"]["link"]["total"] == 150


def test_interface_change_restarts_baseline_without_a_jump(app):
    with Counters() as c:
        c.link = (1000, 1000)
        data_usage.sample(app, day())
        c.iface, c.link = "eth0", (900000, 900000)
        data_usage.sample(app, day())
        c.link = (900100, 900200)
        data_usage.sample(app, day())
    s = data_usage.summary(app, today=date(2026, 11, 12))
    assert s["today"]["link"]["total"] == 300
    assert s["interface"] == "eth0"


def test_tunnel_is_tracked_separately(app):
    with Counters() as c:
        c.link, c.tunnel = (0, 0), (0, 0)
        data_usage.sample(app, day())
        c.link, c.tunnel = (10000, 2000), (700, 300)
        data_usage.sample(app, day())
    s = data_usage.summary(app, today=date(2026, 11, 12))
    assert s["today"]["tunnel"]["total"] == 1000
    assert s["today"]["link"]["total"] == 12000


def test_periods_split_by_day_and_month(app):
    with Counters() as c:
        c.link = (0, 0)
        data_usage.sample(app, day(30))
        c.link = (1000, 0)
        data_usage.sample(app, day(30))             # Oct-style earlier day
        c.link = (3000, 0)
        data_usage.sample(app, datetime(2026, 12, 1, 9, 0))
        c.link = (3500, 0)
        data_usage.sample(app, datetime(2026, 12, 2, 9, 0))
    s = data_usage.summary(app, today=date(2026, 12, 2))
    assert s["today"]["link"]["total"] == 500
    assert s["month"]["link"]["total"] == 2500      # Dec 1 + Dec 2 only
    assert s["week"]["link"]["total"] == 3500


def test_totals_survive_a_restart(app):
    with Counters() as c:
        c.link = (0, 0)
        data_usage.sample(app, day())
        c.link = (4000, 0)
        data_usage.sample(app, day(), force_save=True)
        data_usage.reset_for_tests()                # the service restarts
        c.link = (4500, 0)                          # counters kept going (no reboot)
        data_usage.sample(app, day())
    assert data_usage.summary(app, today=date(2026, 11, 12))["today"]["link"]["total"] == 4500


def test_old_days_are_pruned(app):
    with Counters() as c:
        c.link = (0, 0)
        data_usage.sample(app, datetime(2026, 1, 1))
        c.link = (100, 0)
        data_usage.sample(app, datetime(2026, 1, 1))
        c.link = (200, 0)
        data_usage.sample(app, datetime(2026, 11, 12))
    assert "2026-01-01" not in data_usage._state["days"]


def test_no_network_reports_unavailable(app):
    with mock.patch.object(data_usage, "default_interface", lambda: None), \
            mock.patch.object(data_usage, "read_counters", lambda n: None):
        data_usage.sample(app, day())
    assert data_usage.summary(app)["available"] is False


def test_default_interface_picks_lowest_metric(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "wlan0\t00000000\t0100A8C0\t0003\t0\t0\t600\t00000000\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n"
        "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")
    with mock.patch.object(data_usage, "_ROUTE_FILE", str(route)):
        assert data_usage.default_interface() == "eth0"


def test_read_counters_rejects_path_tricks():
    assert data_usage.read_counters("../etc") is None
    assert data_usage.read_counters("") is None


def test_route_returns_summary_and_reset_clears(client, app):
    with Counters() as c:
        c.link = (0, 0)
        data_usage.sample(app)
        c.link = (2048, 1024)
        res = client.get("/api/data-usage")
        assert res.status_code == 200
        assert res.get_json()["today"]["link"]["total"] == 3072
        assert client.post("/api/data-usage/reset").status_code == 200
        assert client.get("/api/data-usage").get_json()["today"]["link"]["total"] == 0


def test_route_needs_login(app):
    assert app.test_client().get("/api/data-usage").status_code in (302, 401)


def test_staff_session_cannot_see_data_usage(app, fpp):
    """The readout lives on Settings, which a staff PIN session may not reach."""
    staff = app.test_client()
    with staff.session_transaction() as s:
        s["logged_in"] = True
        s["is_staff"] = True
    assert staff.get("/api/data-usage").status_code == 403
    assert staff.post("/api/data-usage/reset").status_code == 403
    page = staff.get("/settings")
    assert page.status_code == 302 and "settings" not in page.headers["Location"]
