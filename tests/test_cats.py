"""Per-cat baselines, warnings and the vet report."""
import pytest

import web

DAY, H = 86400, 3600


@pytest.fixture
def ida(env):
    """A cat with 30 days of steady habits: ~2 visits a day, about a minute each, 9 lb."""
    env.robot()
    pid = env.pet("PET-1", "Ida", weight_lb=9.0)
    for d in range(8, 38):
        for h in (6, 18):
            env.visit(d * DAY + h * H, pet=pid, weight=9.0 + (d % 3) * 0.05, duration=55 + (d % 5) * 3,
                      waste="Feces" if d % 4 == 0 and h == 6 else "Urine")
    return pid


def insights(env):
    return env.client.get("/api/analysis?days=30").get_json()["insights"]


def kinds(env, pid):
    return {i["kind"] for i in insights(env) if i["pet"] == pid}


def test_baseline_learned_from_before_the_last_week(env, ida):
    for d in range(7):
        env.visit(d * DAY + 6 * H, pet=ida)
        env.visit(d * DAY + 18 * H, pet=ida)
    k = next(k for k in env.client.get("/api/analysis?days=30").get_json()["kpis"] if k["pet"] == ida)
    b = k["baseline"]
    assert b["ready"] and abs(b["visits"]["mean"] - 2) < 0.2
    assert 50 <= b["duration"]["median"] <= 70 and abs(b["weight"]["median"] - 9.05) < 0.1
    assert kinds(env, ida) == set()


def test_burst_of_visits_is_flagged_at_high_priority(env, ida):
    for i in range(8):
        env.visit(i * 2 * H + 60, pet=ida)
    assert "visits_burst" in kinds(env, ida)
    with web.app.test_request_context():
        alerts = [a for a in web.current_alerts() if a["category"] == "burst"]
    assert alerts and alerts[0]["priority"] == 1


def test_repeated_long_visits_without_waste_are_flagged(env, ida):
    for i in range(3):
        env.visit(i * DAY + 3 * H, pet=ida, duration=300, waste=None)
    text = next(i["text"] for i in insights(env) if i["kind"] == "long_visits")
    assert "3 unusually long visits" in text and "no waste detected" in text


def test_fewer_visits_than_usual(env, ida):
    env.visit(2 * DAY, pet=ida)
    assert "visits_less" in kinds(env, ida)


def test_vet_report_pdf(env, ida):
    assert env.client.get(f"/report/{ida}.pdf?from=2020-01-01").status_code == 400  # over a year
    assert env.client.get(f"/report/{ida}.pdf?from=2026-02-01&to=2026-01-01").status_code == 400  # backwards
    r = env.client.get(f"/report/{ida}.pdf")
    assert r.status_code == 200 and r.data.startswith(b"%PDF") and "Ida-litter-report" in r.headers["Content-Disposition"]
    assert env.client.get("/report/nobody.pdf").status_code == 404
