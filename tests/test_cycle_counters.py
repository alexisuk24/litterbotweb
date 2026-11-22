"""The clean-cycle odometer, which can be reset in the Whisker app: a new counter per reset,
and a lifetime count that adds them up."""
import json
from types import SimpleNamespace

import lr_clips
import lr_db


def test_a_reset_starts_a_new_counter_and_the_lifetime_count_carries_on(env):
    env.robot()
    assert lr_db.record_clean_cycles(env.db, "LR5-TEST-1", 2089) == 2089
    assert lr_db.record_clean_cycles(env.db, "LR5-TEST-1", 2090) == 2090
    assert lr_db.record_clean_cycles(env.db, "LR5-TEST-1", 0) == 2090   # reset in the Whisker app
    assert lr_db.record_clean_cycles(env.db, "LR5-TEST-1", 5) == 2095
    assert lr_db.record_clean_cycles(env.db, "LR5-TEST-1", 5) == 2095
    rows = env.db.execute("SELECT last_count FROM cycle_counters ORDER BY id").fetchall()
    assert [r["last_count"] for r in rows] == [2090, 5]
    assert lr_db.lifetime_cycles(env.db, "LR5-OTHER") is None


def test_robot_info_carries_the_lifetime_count(env):
    env.robot()
    lr_db.set_robot_info(env.db, "LR5-TEST-1", {"clean_cycles": 100})
    lr_db.set_robot_info(env.db, "LR5-TEST-1", {"clean_cycles": 3})
    info = json.loads(env.db.execute("SELECT info FROM robots").fetchone()["info"])
    assert (info["clean_cycles"], info["lifetime_cycles"]) == (3, 103)
    lr_db.set_robot_info(env.db, "LR5-TEST-1", {"clean_cycles": None})  # not reported: no new counter
    assert lr_db.lifetime_cycles(env.db, "LR5-TEST-1") == 103


def test_a_missing_odometer_is_not_read_as_zero():
    robot = lambda state: SimpleNamespace(to_dict=lambda: {"state": state})
    assert lr_clips.clean_cycles(robot({"odometerCleanCycles": 2089})) == 2089
    assert lr_clips.clean_cycles(robot({})) is None
