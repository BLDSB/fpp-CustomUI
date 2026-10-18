"""Alert emails carry the Site Name so recipients can tell controllers apart."""
from app.alert_monitor import _label


def test_label_stamps_the_site_name_on_subject_and_body():
    subject, body = _label({"site_name": "Inova"}, "Show missed", "It did not start.")
    assert subject == "[Inova] Show missed"
    assert body.startswith("Controller: Inova\n\n") and body.endswith("It did not start.")


def test_label_leaves_the_email_alone_without_a_site_name():
    assert _label({}, "Show missed", "body") == ("Show missed", "body")
    assert _label({"site_name": "   "}, "Show missed", "body") == ("Show missed", "body")
