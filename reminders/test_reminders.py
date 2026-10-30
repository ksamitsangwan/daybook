"""Tests for the reminder logic. Run with:  python -m pytest reminders"""

from datetime import datetime
from zoneinfo import ZoneInfo

from reminders import (api_headers, build_message, find_incomplete_tours, find_late_steps, find_overdue,
                       find_untouched, is_overdue, is_quiet, late_label, open_due_count, tour_missing)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 27, 12, 36, tzinfo=IST)  # Tuesday 12:36 pm
EVENING = datetime(2026, 10, 27, 20, 36, tzinfo=IST)
MS = NOW.timestamp() * 1000
DAY = 86_400_000


def task(**kw):
    base = {"_id": "t", "_user": "u1", "title": "Pay bill", "status": "open", "priority": 3, "due": "2026-10-27", "createdAt": MS}
    base.update(kw)
    return base


# --- overdue -----------------------------------------------------------------
def test_past_date_is_overdue():
    assert is_overdue(task(due="2026-10-26"), NOW)


def test_today_without_time_is_not_overdue_yet():
    assert not is_overdue(task(), NOW)


def test_today_with_passed_time_is_overdue():
    assert is_overdue(task(dueTime="09:00"), NOW)
    assert not is_overdue(task(dueTime="18:00"), NOW)


def test_done_and_undated_are_ignored():
    assert not is_overdue(task(due="2026-10-01", status="done"), NOW)
    assert not is_overdue(task(due=None), NOW)


def test_snoozed_task_waits():
    assert not is_overdue(task(due="2026-10-20", snoozeUntil=MS + 3600_000), NOW)
    assert is_overdue(task(due="2026-10-20", snoozeUntil=MS - 60_000), NOW)


def test_quiet_hours_over_midnight():
    assert is_quiet(datetime(2026, 10, 27, 23, 0, tzinfo=IST), 22, 8)
    assert is_quiet(datetime(2026, 10, 27, 7, 59, tzinfo=IST), 22, 8)
    assert not is_quiet(datetime(2026, 10, 27, 8, 0, tzinfo=IST), 22, 8)
    assert not is_quiet(EVENING, 22, 8)


def test_late_labels():
    assert late_label(task(due="2026-10-24"), NOW) == "3d late"
    assert late_label(task(dueTime="10:00"), NOW) == "2h late"
    assert late_label(task(dueTime="12:00"), NOW) == "36m late"


def test_order_and_message():
    tasks = [task(_id="a", title="Low", due="2026-10-20"), task(_id="b", title="Must", due="2026-10-26", priority=1, remindCount=2),
             task(_id="c", title="Future", due="2026-11-01")]
    overdue = find_overdue(tasks, NOW)
    assert [t["_id"] for t in overdue] == ["b", "a"]
    title, body, prio = build_message(overdue, NOW)
    assert title == "Daybook: 2 overdue tasks"
    assert body.splitlines()[0] == "• Must — 1d late (reminder #3)"
    assert prio == "high"


def test_long_message_is_trimmed():
    many = [task(_id=str(i), title=f"T{i}", due="2026-10-20") for i in range(15)]
    _, body, prio = build_message(find_overdue(many, NOW), NOW)
    assert len(body.splitlines()) == 12 and body.splitlines()[-1] == "+4 more"
    assert prio == "default"


def test_headers_for_new_and_legacy_keys():
    assert "Authorization" not in api_headers("sb_secret_abc")
    assert api_headers("eyJhbGciOi.legacy")["Authorization"] == "Bearer eyJhbGciOi.legacy"


# --- steps -------------------------------------------------------------------
def test_late_steps_are_found_and_listed():
    t = task(_id="rr", title="R&R Shah Motors", due="2026-11-05", priority=1, steps=[
        {"id": "a", "title": "Feasibility", "done": True, "due": "2026-10-20"},
        {"id": "b", "title": "Estimate vetting", "done": False, "due": "2026-10-25"},
        {"id": "c", "text": "MIRR (older format)", "done": False, "due": "2026-10-26"},
        {"id": "d", "title": "DRSH approval", "done": False, "due": "2026-10-28"},
    ])
    late = find_late_steps([t], NOW)
    assert [s["id"] for _, s in late] == ["b", "c"]
    title, body, prio = build_message(find_overdue([t], NOW), NOW, late)
    assert title == "Daybook: 2 late steps"
    assert body.splitlines() == ["• Step: Estimate vetting (in R&R Shah Motors) — 2d late",
                                 "• Step: MIRR (older format) (in R&R Shah Motors) — 1d late"]
    assert prio == "high"


def test_done_or_snoozed_tasks_hide_their_steps():
    steps = [{"id": "x", "title": "Late", "done": False, "due": "2026-10-01"}]
    assert find_late_steps([task(status="done", steps=steps)], NOW) == []
    assert find_late_steps([task(snoozeUntil=MS + 3600_000, steps=steps)], NOW) == []


def test_overdue_task_shows_step_progress():
    t = task(due="2026-10-26", steps=[{"id": "a", "title": "One", "done": True}, {"id": "b", "title": "Two", "done": False}])
    title, body, _ = build_message(find_overdue([t], NOW), NOW)
    assert body == "• Pay bill — 1d late (reminder #1) · 1/2 steps"


# --- untouched, close the day ------------------------------------------------
def test_untouched_tasks():
    old = task(_id="o", title="Lease note", due=None, createdAt=MS - 9 * DAY, touchedAt=MS - 9 * DAY)
    fresh = task(_id="f", title="Fresh", due=None, createdAt=MS - 9 * DAY, touchedAt=MS - DAY)
    late = task(_id="l", title="Late one", due="2026-10-01", createdAt=MS - 30 * DAY)
    assert [(t["_id"], d) for t, d in find_untouched([old, fresh, late], NOW)] == [("o", 9)]
    title, body, _ = build_message([], NOW, untouched=find_untouched([old], NOW))
    assert title == "Daybook: untouched tasks" and body.splitlines() == ["Untouched:", "• Lease note — 9 days"]


def test_close_day_nudge():
    tasks = [task(), task(_id="b", due="2026-10-25"), task(_id="c", due="2026-10-30"), task(_id="d", status="done")]
    assert open_due_count(tasks, EVENING) == 2
    title, body, _ = build_message([], EVENING, close_day=2)
    assert title == "Daybook: close your day" and body == "Close your day in Daybook: 2 items still open for today"
    assert build_message([], EVENING, close_day=0)[1] == "Close your day in Daybook: log what you did and set tomorrow's first task"


# --- trip details ------------------------------------------------------------
def tour(**kw):
    base = {"date": "2026-10-27", "status": "done", "type": "Outstation", "mode": "Own Car / Two Wheeler", "cls": "AC", "vehicle": "PB03AB1234",
            "startTime": "09:00", "startOdo": 45210, "endTime": "16:00", "endOdo": 45329,
            "stops": [{"place": "Patran", "arrive": "10:00", "depart": "12:00", "odo": 45262},
                      {"place": "Ghagga", "arrive": "12:30", "depart": "13:20", "km": 19, "odo": 45281}]}
    base.update(kw)
    return base


def test_complete_tour_has_nothing_missing():
    assert tour_missing(tour(), EVENING.date()) == 0


def test_km_or_odometer_either_counts():
    t = tour(endOdo=None, kmBack=48)
    assert tour_missing(t, EVENING.date()) == 0
    t = tour(endOdo=None)  # last leg has neither km nor end odometer
    assert tour_missing(t, EVENING.date()) == 1


def test_missing_times_and_claim_fields_are_counted():
    t = tour(vehicle="", stops=[{"place": "Patran", "arrive": None, "depart": None, "km": 52}])
    assert tour_missing(t, EVENING.date()) == 4  # arrive, depart, km back to HQ (no odometer at Patran), vehicle
    assert tour_missing(tour(type="Local", vehicle=""), EVENING.date()) == 0  # local tours need no claim fields


def test_running_and_old_tours():
    tours = [tour(status="active", purpose="NFR visits"), tour(date="2026-10-10", endOdo=None), tour(date="2026-10-26", endOdo=None)]
    found = find_incomplete_tours(tours, EVENING)
    assert [(t["date"], m) for t, m in found] == [("2026-10-26", 1), ("2026-10-27", "running")]
    _, body, _ = build_message([], EVENING, tours=found)
    assert body.splitlines() == ["Trip details to complete:", "• Mon 26 Oct — 1 item missing", "• Tue 27 Oct, NFR visits — still running, end it"]
