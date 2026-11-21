"""Ignored visits: under 2.6 lb, or no cat and no clip."""
import web

H = 3600


def visit(env, aid):
    with web.app.test_request_context():
        return next(v for v in web.load_visits(types=None) if v["id"] == aid)


def test_rules(env):
    env.robot()
    ida = env.pet("PET-1", "Ida")
    light = env.visit(2 * H, pet=ida, weight=1.0)
    lone = env.visit(3 * H, pet=None)
    filmed = env.visit(4 * H, pet=None)
    env.clip(4 * H + 60)
    normal = env.visit(5 * H, pet=ida)
    assert visit(env, light)["ignored"] == "light" and visit(env, light)["pet"] is None
    assert visit(env, lone)["ignored"] == "no_clip"
    assert visit(env, filmed)["ignored"] is None
    assert visit(env, normal)["ignored"] is None


def test_light_visits_cant_be_given_a_cat_but_take_notes(env):
    env.robot()
    ida = env.pet("PET-1", "Ida")
    light = env.visit(2 * H, pet=ida, weight=1.0)
    r = env.client.post(f"/api/events/{light}/override", json={"pet_id": ida})
    assert r.status_code == 409
    e = env.client.post(f"/api/events/{light}/override", json={"note": "a toy"}).get_json()["event"]
    assert (e["note"], e["ignored"], e["pet"]) == ("a toy", "light", None)


def test_assigning_a_cat_counts_a_clipless_visit_again(env):
    env.robot()
    ida = env.pet("PET-1", "Ida")
    lone = env.visit(2 * H)
    e = env.client.post(f"/api/events/{lone}/override", json={"pet_id": ida}).get_json()["event"]
    assert e["ignored"] is None and e["pet"] == ida


def test_whisker_unassign_is_queued_once_for_recent_light_visits(env):
    env.robot()
    ida = env.pet("PET-1", "Ida")
    recent = env.visit(2 * H, pet=ida, weight=1.0)
    env.visit(10 * 86400, pet=ida, weight=1.0)  # too old for Whisker to accept
    with web.app.app_context():
        assert web.queue_ignored_unassigns() == 1
        assert web.queue_ignored_unassigns() == 0
    rows = env.db.execute("SELECT activity_id, to_pet FROM whisker_pushes").fetchall()
    assert [(r["activity_id"], r["to_pet"]) for r in rows] == [(recent, None)]


def test_no_unassign_with_whisker_sync_off(env):
    env.robot()
    env.visit(2 * H, pet=env.pet("PET-1", "Ida"), weight=1.0)
    env.state(writeback="0")
    with web.app.app_context():
        assert web.queue_ignored_unassigns() == 0


def test_ignored_visits_left_out_of_cats_page_and_filters(env):
    env.robot()
    ida = env.pet("PET-1", "Ida")
    env.visit(2 * H, pet=ida, weight=1.0)
    env.visit(3 * H)
    env.visit(4 * H, pet=ida)
    totals = env.client.get("/api/analysis?days=7").get_json()["totals"]
    assert (totals["visits"], totals["unassigned"]) == (1, 0)
    assert env.client.get("/api/events?filter=ignored").get_json()["total"] == 2
    assert env.client.get("/api/events?pet=none").get_json()["total"] == 0
