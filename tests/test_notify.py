"""Pushover notifications: what's sent, once, and the per-type switches."""
import pytest

import lr_notify

T, U = "a" * 30, "u" * 30


def alert(key, category="burst", group="cats", text="Ida visited 7 times", priority=0):
    return {"key": key, "group": group, "category": category, "subject": "Ida", "text": text, "priority": priority}


@pytest.fixture
def sent(env, monkeypatch):
    out = []
    monkeypatch.setattr(lr_notify, "send", lambda *a, **k: out.append(k))
    lr_notify.save_settings(env.db, {"token": T, "user": U, "enabled": True})
    env.db.commit()  # as the settings endpoint does; an open write would lock out the app's connection
    return out


def test_keys_are_validated(env):
    with pytest.raises(ValueError):
        lr_notify.save_settings(env.db, {"enabled": True})  # no keys yet
    with pytest.raises(ValueError):
        lr_notify.save_settings(env.db, {"token": "short"})
    with pytest.raises(ValueError):
        lr_notify.save_settings(env.db, {"categories": {"bogus": False}})


def test_keys_never_go_back_to_the_browser(env, sent):
    n = env.client.get("/api/settings").get_json()["notify"]
    assert (n["user_hint"], n["token_hint"]) == ("uuuu", "aaaa")
    assert T not in str(n) and U not in str(n)


def test_new_alerts_go_out_together_then_not_again(env, sent):
    alerts = [alert("cat:1:burst", priority=1), alert("cat:2:long", "long_visits")]
    assert lr_notify.notify(env.db, alerts) == 2
    assert len(sent) == 1 and sent[0]["title"] == "2 new litter box alerts" and sent[0]["priority"] == 1
    assert lr_notify.notify(env.db, alerts) == 0


def test_an_alert_that_clears_and_returns_is_sent_again(env, sent):
    a, b = alert("cat:1:burst"), alert("cat:2:long", "long_visits")
    lr_notify.notify(env.db, [a, b])
    lr_notify.notify(env.db, [b])
    assert lr_notify.notify(env.db, [a, b]) == 1
    assert sent[-1]["title"] == "Ida"


def test_switched_off_types_are_skipped(env, sent):
    lr_notify.save_settings(env.db, {"categories": {"burst": False}})
    assert lr_notify.notify(env.db, [alert("cat:1:burst"), alert("cat:2:long", "long_visits")]) == 1


def test_a_failed_send_is_retried_next_time(env, monkeypatch):
    lr_notify.save_settings(env.db, {"token": T, "user": U, "enabled": True})

    def fail(*a, **k):
        raise lr_notify.PushoverError("Couldn't reach Pushover")
    monkeypatch.setattr(lr_notify, "send", fail)
    assert lr_notify.notify(env.db, [alert("cat:1:burst")]) == 0
    assert lr_notify.settings(env.db)["last_error"] == "Couldn't reach Pushover"
    monkeypatch.setattr(lr_notify, "send", lambda *a, **k: None)
    assert lr_notify.notify(env.db, [alert("cat:1:burst")]) == 1


def test_disabled_sends_nothing(env, monkeypatch):
    monkeypatch.setattr(lr_notify, "send", lambda *a, **k: pytest.fail("sent while disabled"))
    assert lr_notify.notify(env.db, [alert("cat:1:burst")]) == 0
