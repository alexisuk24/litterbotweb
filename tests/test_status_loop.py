"""The collector's status loop: noticing a finished clean cycle, fetching the visit and
sending a visit notification at once, and the delayed follow-up for detections and clips."""
import argparse
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from pylitterbot.enums import LitterBoxStatus

import lr_clips
import lr_db
import lr_notify

T, U = "a" * 30, "u" * 30


def robot(count, status=LitterBoxStatus.READY, serial="LR5-TEST-1", activities=()):
    async def get_activities(limit=100, offset=0):
        return list(activities)[offset:offset + limit]
    return SimpleNamespace(serial=serial, name="Marvin", cycle_count=count, status=status,
                           has_camera=True, get_activities=get_activities)


def test_a_cycle_counts_once_it_finishes(env):
    assert not lr_clips.cycle_finished(env.db, robot(10))  # first look only records the count
    assert not lr_clips.cycle_finished(env.db, robot(10))
    assert not lr_clips.cycle_finished(env.db, robot(11, LitterBoxStatus.CLEAN_CYCLE))  # still cycling
    assert lr_clips.cycle_finished(env.db, robot(11))
    assert not lr_clips.cycle_finished(env.db, robot(11))  # not again, including after a restart
    assert not lr_clips.cycle_finished(lr_db.connect(), robot(11))
    assert not lr_clips.cycle_finished(env.db, robot(0))  # reset in the Whisker app
    assert lr_clips.cycle_finished(env.db, robot(1))


def test_a_cycle_right_after_a_reset_still_counts(env):
    lr_clips.cycle_finished(env.db, robot(2090))
    assert lr_clips.cycle_finished(env.db, robot(1))  # reset, then a cycle, between two looks
    assert not lr_clips.cycle_finished(env.db, robot(1))


@pytest.fixture
def sent(env, monkeypatch):
    out = []
    monkeypatch.setattr(lr_notify, "send", lambda *a, **k: out.append((a, k)))
    lr_notify.save_settings(env.db, {"token": T, "user": U, "enabled": True})
    env.db.commit()
    return out


def test_visit_notifications_are_off_until_switched_on(env, sent):
    env.robot()
    env.visit(60)
    assert lr_notify.notify_visits(env.db, "LR5-TEST-1") == 0 and not sent
    cats = env.client.get("/api/settings").get_json()["notify"]["categories"]
    assert {c["id"]: c["on"] for c in cats}["visit"] is False
    assert {c["id"]: c["on"] for c in cats}["burst"] is True


def test_switching_visits_on_leaves_the_other_types_alone(env, sent):
    lr_notify.save_settings(env.db, {"categories": {"burst": False}})
    lr_notify.save_settings(env.db, {"categories": {"visit": True}})
    off = lr_notify.settings(env.db)["off"]
    assert "visit" not in off and "burst" in off
    lr_notify.save_settings(env.db, {"categories": {"visit": False}})
    assert "visit" in lr_notify.settings(env.db)["off"]


def test_each_new_visit_is_sent_once_naming_the_cat(env, sent):
    lr_notify.save_settings(env.db, {"categories": {"visit": True}})
    env.state(weight_unit="lb")
    env.robot()
    env.pet("PET-1", "Astrid")
    env.visit(7200, pet="PET-1")         # too old to be news
    env.visit(120, pet="PET-1", weight=9.5)
    env.visit(60, weight=0.4)             # a scale blip, not a cat
    assert lr_notify.notify_visits(env.db, "LR5-TEST-1") == 1
    _, _, title, message = sent[0][0]
    assert title == "Marvin: visit" and message.startswith("Astrid at ") and "9.50 lb · urine" in message
    assert lr_notify.notify_visits(env.db, "LR5-TEST-1") == 0
    env.visit(10)
    assert lr_notify.notify_visits(env.db, "LR5-TEST-1") == 1
    assert sent[-1][0][3].startswith("A cat at ")


def test_a_finished_cycle_fetches_the_visit_and_schedules_a_follow_up(env, sent):
    lr_notify.save_settings(env.db, {"categories": {"visit": True}})
    env.robot()
    env.state(**{"activities_backfilled:LR5-TEST-1": 1})
    item = {"messageId": "m1", "type": "PET_VISIT", "timestamp": time.time() - 30, "petWeight": 950,
            "wasteType": "Urine", "petIds": []}
    args = argparse.Namespace(db=str(lr_db.DB_PATH))
    asyncio.run(lr_clips.after_cycle(env.db, robot(11, activities=[item]), args))
    assert env.db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    assert len(sent) == 1
    due = json.loads(lr_db.get_state(env.db, "followup:LR5-TEST-1"))
    assert len(due) == len(lr_clips.FOLLOWUP_DELAYS) and due[0] > time.time()
    assert lr_clips.due_followups(env.db) == ([], due[0])
    assert lr_clips.due_followups(env.db, now=due[0] + 1) == (["LR5-TEST-1"], due[1])


def test_no_fast_path_before_the_first_backfill(env):
    asyncio.run(lr_clips.after_cycle(env.db, robot(11, activities=[{"type": "PET_VISIT", "timestamp": 1}]),
                                     argparse.Namespace(db=str(lr_db.DB_PATH))))
    assert env.db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert lr_db.get_state(env.db, "followup:LR5-TEST-1") is None


def test_the_sync_loop_wakes_for_a_due_follow_up(env):
    env.state(sync_interval=86400, refresh_requested=0, sync_started=time.time(),
              **{"followup:LR5-TEST-1": json.dumps([time.time() + 0.3, time.time() + 600])})

    async def run():
        return await asyncio.wait_for(lr_clips.wait_for_next_run(env.db, 3600, None), 5)

    assert asyncio.run(run()) == "followup"


def test_the_follow_up_lists_only_the_newest_clips():
    asked = []

    async def get_camera_videos(date=None, timezone=None):
        asked.append(date)
        return []
    r = SimpleNamespace(name="Marvin", get_camera_videos=get_camera_videos)
    asyncio.run(lr_clips.list_clips(r, 7, "UTC", latest_only=True))
    assert asked == [None]
    asyncio.run(lr_clips.list_clips(r, 7, "UTC"))
    assert len(asked) == 1 + 1 + 9  # the newest, then each day of the window


def test_new_activities_are_logged_with_how_late_they_were_listed(env, caplog):
    env.robot()
    items = [{"messageId": "m1", "type": "PET_VISIT", "timestamp": time.time() - 540, "petWeight": 950},
             {"messageId": "m2", "type": "CYCLE_COMPLETED", "timestamp": time.time() - 20}]
    with caplog.at_level("INFO", logger="lr_clips"):
        asyncio.run(lr_clips.fetch_new_activities(env.db, robot(11, activities=items), "clean cycle finished"))
        asyncio.run(lr_clips.fetch_new_activities(env.db, robot(11, activities=items), "follow-up"))
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1  # nothing new at the follow-up, so nothing logged
    assert "PET_VISIT from 54" in lines[0] and "CYCLE_COMPLETED from 2" in lines[0]
