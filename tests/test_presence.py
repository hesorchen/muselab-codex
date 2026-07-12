"""Phone-only presence gating."""

from backend import presence


def setup_function():
    presence._devices.clear()


def test_desktop_presence_never_suppresses_phone_push():
    presence.mark_seen("desktop", True, device_kind="desktop")

    assert presence.recently_active() is False
    assert presence.last_seen_age() is None


def test_visible_mobile_suppresses_until_it_hides():
    presence.mark_seen("phone", True, device_kind="mobile")
    assert presence.recently_active() is True

    presence.mark_seen("phone", False, device_kind="mobile")
    assert presence.recently_active() is False


def test_legacy_unknown_presence_does_not_suppress_phone_push():
    presence.mark_seen("legacy", True)

    assert presence.recently_active() is False
