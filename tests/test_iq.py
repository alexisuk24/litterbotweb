"""Robot IQ forecasts: drawer full and hopper refill."""
import lr_iq

DAY, H = 86400, 3600
SERIAL = "LR5-TEST-1"


def snapshot(env, ago_s, drawer=None, cycles=None, hopper=None):
    env.db.execute("INSERT INTO robot_snapshots (robot_serial, ts, waste_drawer_level, cycle_count, hopper_status) "
                   "VALUES (?, ?, ?, ?, ?)", (SERIAL, env.now - ago_s, drawer, cycles, hopper))


def cycles(env, per_day, days=7):
    for i in range(int(per_day * days)):
        env.visit(i * DAY / per_day + 60, kind="CYCLE_COMPLETED")


def test_drawer_forecast_from_fill_rate_since_emptying(env):
    env.robot(info={"faults": []})
    cycles(env, 6)
    snapshot(env, 13 * DAY, drawer=70, cycles=900)  # before emptying: ignored
    cyc, lvl = 1000, 2.0
    for h in range(12 * 24, 0, -1):
        cyc += 6 / 24
        lvl += 0.6 * 6 / 24  # 0.6% per clean cycle
        snapshot(env, h * H, drawer=round(lvl, 1), cycles=int(cyc))
    env.db.commit()
    d = lr_iq.forecasts(env.db, SERIAL)["drawer"]
    assert d["state"] == "ok" and abs(d["per_cycle"] - 0.6) < 0.02
    assert abs(d["days"] - (100 - d["level"]) / 0.6 / d["cycles_per_day"]) < 0.2


def test_drawer_full_level_learned_from_full_events(env):
    env.robot(info={"faults": []})
    snapshot(env, 5 * DAY, drawer=75, cycles=1000)
    env.visit(5 * DAY - 600, kind="DRAWER_FULL")
    env.db.commit()
    assert lr_iq.forecasts(env.db, SERIAL)["drawer"]["full_level"] == 75


def test_drawer_learning_without_history(env):
    env.robot(info={"faults": []})
    snapshot(env, H, drawer=13, cycles=500)
    env.db.commit()
    assert lr_iq.forecasts(env.db, SERIAL)["drawer"]["state"] == "learning"


def test_hopper_capacity_learned_from_a_refill_running_low(env):
    env.robot(raw={"state": {"hopperStatusIndicator": {"value": "READY"}, "isHopperInstalled": True}})
    snapshot(env, 31 * DAY, hopper="EMPTY")
    snapshot(env, 30 * DAY, hopper="READY")      # refilled
    for i in range(12):                          # 12 dispenses until it ran low
        env.visit(29 * DAY - i * DAY, kind="HOPPER_DISPENSED")
    snapshot(env, 15 * DAY, hopper="LITTER_LOW")
    snapshot(env, 14 * DAY, hopper="READY")      # refilled again
    for i in range(10):                          # 10 used since
        env.visit(13 * DAY - i * 1.2 * DAY, kind="HOPPER_DISPENSED")
    snapshot(env, H, hopper="READY")
    env.db.commit()
    h = lr_iq.forecasts(env.db, SERIAL)["hopper"]
    assert (h["state"], h["capacity"], h["since_refill"]) == ("ok", 12, 10)
    assert 0 < h["days"] < 5


def test_hopper_low_and_disabled(env):
    env.robot(raw={"state": {"hopperStatusIndicator": {"value": "LITTER_LOW"}}})
    assert lr_iq.forecasts(env.db, SERIAL)["hopper"]["state"] == "low"
    env.db.execute("UPDATE robots SET raw = '{\"state\": {\"isHopperInstalled\": false}}'")
    assert lr_iq.forecasts(env.db, SERIAL)["hopper"] is None
