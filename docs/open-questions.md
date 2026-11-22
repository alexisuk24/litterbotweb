# Open questions about Whisker and the Litter-Robot 5

How the robot and Whisker's API behave where nobody has confirmed it yet. Settled
answers move to [What we've confirmed](#what-weve-confirmed) with how we know, and the
code should follow them. Add new questions as you find them.

Two ways to find answers:

- **The sync log** (Settings > Logs, or `docker logs`). Between syncs the status loop
  logs each status change (`Marvin: status READY -> CAT_DETECTED`) and each new activity
  with how long after its own time it was listed (`new activities: PET_VISIT from 540s
  ago`). Follow-ups log what they found 1, 5 and 15 minutes after a cycle.
- **A watch script** that polls one robot every few seconds and records every field
  that changes. Ask for it again if needed. Its output contains raw robot data, so keep
  it out of the repo.

## Open

### Is a visit listed before the robot cycles?

Visits are timestamped about 9 minutes before their `CYCLE_COMPLETED` (the clean cycle
delay, 7 minutes by default, plus about 2 minutes of cycling). If Whisker lists the
`PET_VISIT` soon after the cat leaves, the status loop could fetch it, and send the
visit notification, when the robot reports `CAT_DETECTED` rather than after the cycle:
about 8 minutes sooner.

- **How to answer:** look in the sync log for `cat detected, waiting to cycle; new
  activities: PET_VISIT …` lines. If they appear, visits are listed early. If visits
  only ever appear at `clean cycle finished`, they aren't.
- **Affects:** `status_watcher` and `after_cycle` in `lr_clips.py`.

### When does the camera's cat detection arrive?

Before the status loop, detections were seen spread over two or three 16-minute syncs
after a visit. A test cycle with no cat produced none, so there's no timing yet.

- **How to answer:** check which follow-up (1, 5 or 15 minutes) logs `new camera
  detection(s)` after real visits.
- **Affects:** `FOLLOWUP_DELAYS` in `lr_clips.py`.

### Does the robot's status change when the cat leaves?

In the test (no cat, robot triggered by hand) the status stayed `CAT_DETECTED`, shown as
"In use", for the whole clean cycle delay, and `catDetect` stayed `LaserClear`. If a
real cat leaving changes some field, that would mark the end of a visit more precisely
than the cycle.

- **How to answer:** a watch-script run during a real visit.

### What is `lastResetOdometerCleanCycles`?

pylitterbot describes it as the clean-cycle count at the last waste drawer reset. One
robot reported 6 against an odometer of 2089, which doesn't fit either reading. If it
turns out to mean the cycles since the drawer was reset, the Waste drawer tile could use
it instead of working the number out from drawer level drops.

- **How to answer:** note its value before and after emptying the drawer and pressing
  reset, and after a cycle. It's in `robots.raw` (`state.lastResetOdometerCleanCycles`).

### What does an interrupted cycle look like?

`CYCLE_INTERRUPTED` appears in the activity log (a cat getting in mid-cycle), but how the
status and odometer move around it hasn't been seen. A cycle that's interrupted and then
completes should still count once.

- **How to answer:** the status lines in the sync log around a `CYCLE_INTERRUPTED`.

### Does sleep mode change any of this?

Not yet observed. Status checks could be less frequent during sleep hours if nothing
happens then.

## What we've confirmed

From a test cycle on a Litter-Robot 5 Pro (pylitterbot 2025.9.0), November 2026:

- **The status passes through `CLEAN_CYCLE` for about 2m20s** (`cycleType` goes Dump,
  Dfi, Level, Home), so a 60-second status check sees it.
- **The clean-cycle odometer (`odometerCleanCycles`, pylitterbot's `cycle_count`) goes
  up as the cycle ends**, in the same response that reports `READY`. It's the Whisker
  app's "scoops saved", not a count since the drawer was emptied, and the user can reset
  it in the Whisker app.
- **Whisker doesn't report cycles since the drawer was emptied** anywhere in the app.
- **The `CYCLE_COMPLETED` activity is listed within about 30 seconds** of the cycle ending.
- **A clip is listed about 2.5 minutes after the motion that starts it**, which is before
  the clean cycle delay ends. The newest-clips listing (no date) finds it.
- **A status refresh takes about 0.3 seconds.**
- **The activity log has no "drawer emptied" entry**, only `DRAWER_FULL`.
