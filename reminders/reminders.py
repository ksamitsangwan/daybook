"""Daybook reminder service.

Runs on a schedule (GitHub Actions): 8:36 am, 12:36 pm, 5:36 pm and 8:36 pm IST.
Each run reads your Daybook data from Supabase and sends ONE push notification
to your phone through ntfy listing:
  - overdue tasks and late steps (reminders repeat on every run until closed),
  - tasks untouched for 5+ days,
  - from 5 pm: a nudge to close the day if you haven't,
  - from 8 pm: tours from the last 7 days with missing trip details,
  - always first: burning tasks (marked 🔥 for today),
  - mornings: today's Top 3 (or suggestions if you haven't picked them),
  - delegated tasks whose follow-up date has come,
  - from 8 pm: a nudge if no important-but-not-urgent (Q2) work got done today.
It adds 1 to remindCount on each overdue task, so you can see how often it was nagged.

Settings come from environment variables (GitHub stores them as Secrets):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the secret key, sb_secret_... (never put it in the app)
  NTFY_TOPIC            your private topic name
  APP_URL               optional, opened when you tap the notification
  TIMEZONE              optional, default Asia/Kolkata
  QUIET_START / QUIET_END  optional hours, default 22 and 8

Run locally:  python reminders.py --dry-run --ignore-quiet   (prints, sends nothing)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

NTFY_SERVER = "https://ntfy.sh"
MAX_LINES = 12
STALE_DAYS = 5
DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Pure logic: plain data in, plain data out (see test_reminders.py)
# Field names match the app: due, dueTime, snoozeUntil, remindCount, steps ...
# ---------------------------------------------------------------------------

def is_quiet(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """True when `now` is inside quiet hours. Handles windows over midnight."""
    h = now.hour
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= h < quiet_end
    return h >= quiet_start or h < quiet_end


def _minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


def _num(value) -> bool:
    if value is None or value == "":
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _now_ms(now: datetime) -> float:
    return now.timestamp() * 1000


def is_open(task: dict) -> bool:
    return task.get("status") != "done"


def is_snoozed(task: dict, now: datetime) -> bool:
    s = task.get("snoozeUntil")
    return bool(s) and s > _now_ms(now)


def is_overdue(task: dict, now: datetime) -> bool:
    """Open, not snoozed, and its due date (or due time today) has passed."""
    if not is_open(task) or not task.get("due") or is_snoozed(task, now):
        return False
    due = _day(task["due"])
    if due < now.date():
        return True
    if due == now.date():
        m = _minutes(task.get("dueTime"))
        return m is not None and m < now.hour * 60 + now.minute
    return False


def late_label(task: dict, now: datetime) -> str:
    days = (now.date() - _day(task["due"])).days
    if days >= 1:
        return f"{days}d late"
    late = now.hour * 60 + now.minute - (_minutes(task.get("dueTime")) or 0)
    return f"{late // 60}h late" if late >= 60 else f"{late}m late"


def importance(task: dict) -> bool | None:
    """True/False as set in the app; old tasks count as important only if priority was 1."""
    imp = task.get("important")
    if imp is True or imp is False:
        return imp
    return True if task.get("priority") == 1 else None


def is_burning(task: dict, now: datetime) -> bool:
    return is_open(task) and task.get("burningOn") == now.date().isoformat()


def is_urgent(task: dict, now: datetime) -> bool:
    """Same rule as the app: burning, overdue, forced by hand, or due today/tomorrow."""
    if not is_open(task):
        return False
    if is_burning(task, now):
        return True
    due = _day(task["due"]) if task.get("due") else None
    if due and due < now.date():
        return True
    if due == now.date():
        m = _minutes(task.get("dueTime"))
        if m is not None and m < now.hour * 60 + now.minute:
            return True
    if task.get("urgency") == "yes":
        return True
    if task.get("urgency") == "no":
        return False
    return due is not None and (due - now.date()).days <= 1


def quadrant(task: dict, now: datetime) -> int:
    """Covey quadrant 1-4, or 0 when importance hasn't been decided yet."""
    imp = importance(task)
    if imp is None:
        return 0
    urg = is_urgent(task, now)
    return (1 if urg else 2) if imp else (3 if urg else 4)


QRANK = {1: 1, 2: 2, 0: 3, 3: 4, 4: 5}


def prio_key(task: dict, now: datetime) -> tuple:
    return (0 if is_burning(task, now) else QRANK[quadrant(task, now)], str(task.get("due") or "9999"),
            task.get("dueTime") or "", task.get("priority") or 3)


def find_overdue(tasks: list[dict], now: datetime) -> list[dict]:
    overdue = [t for t in tasks if is_overdue(t, now)]
    return sorted(overdue, key=lambda t: prio_key(t, now))


def find_burning(tasks: list[dict], now: datetime) -> list[dict]:
    return [t for t in tasks if is_burning(t, now)]


def find_followups(tasks: list[dict], now: datetime) -> list[dict]:
    """Delegated tasks whose follow-up date has come."""
    out = [t for t in tasks if is_open(t) and t.get("delegateTo") and t.get("followUp")
           and _day(t["followUp"]) <= now.date()]
    return sorted(out, key=lambda t: str(t["followUp"]))


def top3_today(tasks: list[dict], day_doc: dict | None, now: datetime) -> tuple[list[dict], list[dict]]:
    """(picked Top 3 still open, suggestions when none were picked)."""
    by_id = {t.get("_id"): t for t in tasks}
    picked = [by_id[i] for i in (day_doc or {}).get("top3") or [] if i in by_id]
    if picked:
        return [t for t in picked if is_open(t)], []
    horizon = now.date().toordinal() + 7
    cands = [t for t in tasks if is_open(t) and importance(t) is True and not is_burning(t, now)
             and not t.get("delegateTo") and (not t.get("due") or _day(t["due"]).toordinal() <= horizon)]
    return [], sorted(cands, key=lambda t: prio_key(t, now))[:3]


def q2_done_today(tasks: list[dict], now: datetime, tz=None) -> bool:
    """True if any Q2 task was finished today, or a step of an important task was ticked today."""
    day = now.date()

    def on_day(ms) -> bool:
        return bool(ms) and datetime.fromtimestamp(ms / 1000, tz or now.tzinfo).date() == day

    for t in tasks:
        if t.get("status") == "done" and t.get("doneQ") == 2 and on_day(t.get("doneAt")):
            return True
        if importance(t) is True and any(s.get("done") and on_day(s.get("doneAt")) for s in t.get("steps") or []):
            return True
    return False


def next_q2(tasks: list[dict], now: datetime) -> dict | None:
    q2 = [t for t in tasks if is_open(t) and quadrant(t, now) == 2]
    return sorted(q2, key=lambda t: prio_key(t, now))[0] if q2 else None


def step_title(step: dict) -> str:
    return step.get("title") or step.get("text") or "Untitled step"


def step_progress(task: dict) -> str:
    steps = task.get("steps") or []
    if not steps:
        return ""
    return f" · {sum(1 for s in steps if s.get('done'))}/{len(steps)} steps"


def find_late_steps(tasks: list[dict], now: datetime) -> list[tuple[dict, dict]]:
    out = []
    for t in tasks:
        if not is_open(t) or is_snoozed(t, now):
            continue
        for s in t.get("steps") or []:
            if not s.get("done") and s.get("due") and _day(s["due"]) < now.date():
                out.append((t, s))
    return sorted(out, key=lambda ts: (prio_key(ts[0], now), str(ts[1]["due"])))


def untouched_days(task: dict, now: datetime) -> int:
    last = task.get("touchedAt") or task.get("createdAt")
    if not last:
        return 0
    return int((_now_ms(now) - last) // DAY_MS)


def find_untouched(tasks: list[dict], now: datetime) -> list[tuple[dict, int]]:
    out = [(t, untouched_days(t, now)) for t in tasks if is_open(t) and not is_overdue(t, now)]
    return sorted([x for x in out if x[1] >= STALE_DAYS], key=lambda x: -x[1])


def open_due_count(tasks: list[dict], now: datetime) -> int:
    return sum(1 for t in tasks if is_open(t) and t.get("due") and _day(t["due"]) <= now.date())


def tour_missing(tour: dict, today: date) -> int | str:
    """Number of missing trip details, or "running" for a tour that was never ended."""
    if tour.get("status") == "active":
        return "running"
    stops = tour.get("stops") or []
    back = tour.get("returnHQ") is not False
    missing = 0
    for i, s in enumerate(stops):
        if not s.get("arrive"):
            missing += 1
        is_last = i == len(stops) - 1
        if not s.get("depart") and not (is_last and not back):
            missing += 1
    # legs: HQ -> stops -> HQ; distance = km on the destination, or odometer at both ends
    pts = [{"odo": tour.get("startOdo")}] + [{"odo": s.get("odo"), "km": s.get("km")} for s in stops]
    if back and stops:
        pts.append({"odo": tour.get("endOdo"), "km": tour.get("kmBack")})
    for a, b in zip(pts, pts[1:]):
        if not _num(b.get("km")) and not (_num(a.get("odo")) and _num(b.get("odo"))):
            missing += 1
    if (tour.get("type") or "Outstation") == "Outstation":
        missing += sum(1 for k in ("mode", "cls", "vehicle") if not str(tour.get(k) or "").strip())
    if not stops:
        missing += 1
    return missing


def find_incomplete_tours(tours: list[dict], now: datetime) -> list[tuple[dict, int | str]]:
    out = []
    for t in tours:
        if not t.get("date") or (now.date() - _day(t["date"])).days > 7:
            continue
        m = tour_missing(t, now.date())
        if m == "running" or m:
            out.append((t, m))
    return sorted(out, key=lambda x: str(x[0]["date"]))


def build_message(overdue: list[dict], now: datetime, late_steps=(), untouched=(), tours=(),
                  close_day: int | None = None, burning=(), top3=(), suggest=(), followups=(),
                  q2_nudge: dict | None | bool = None) -> tuple[str, str, str]:
    """Return (title, body, priority) for the push notification.

    q2_nudge: None = no nudge; False = nudge with no Q2 task to suggest; a task = nudge naming it.
    """
    lines: list[str] = []
    n_tasks, n_steps = len(overdue), len(late_steps)
    for t in burning:
        lines.append(f"🔥 {t['title']} (burning today)")
    if top3:
        lines.append("Today's Top 3 (do these first):")
        lines += [f"★ {t['title']}" for t in top3]
    elif suggest:
        lines.append("Pick today's Top 3. Suggested:")
        lines += [f"☆ {t['title']}" for t in suggest]
    burning_ids = {id(t) for t in burning}
    for t in overdue:
        if id(t) in burning_ids:
            continue
        count = (t.get("remindCount") or 0) + 1
        lines.append(f"• {t['title']} — {late_label(t, now)} (reminder #{count}){step_progress(t)}")
    for t, s in late_steps:
        days = (now.date() - _day(s["due"])).days
        lines.append(f"• Step: {step_title(s)} (in {t['title']}) — {days}d late")
    for t in followups:
        lines.append(f"• Follow up with {t['delegateTo']}: {t['title']}")
    if untouched:
        lines.append("Untouched:")
        lines += [f"• {t['title']} — {d} days" for t, d in list(untouched)[:3]]
        if len(untouched) > 3:
            lines.append(f"+{len(untouched) - 3} more")
    if tours:
        lines.append("Trip details to complete:")
        for t, m in tours:
            label = _day(t["date"]).strftime("%a %d %b") + (f", {t['purpose']}" if t.get("purpose") else "")
            lines.append(f"• {label} — " + ("still running, end it" if m == "running" else f"{m} item{'s' if m != 1 else ''} missing"))
    if close_day is not None:
        lines.append(f"Close your day in Daybook: {close_day} item{'s' if close_day != 1 else ''} still open for today" if close_day
                     else "Close your day in Daybook: log what you did and set tomorrow's first task")
    if q2_nudge is not None:
        lines.append("No Q2 work today (important, not urgent)." + (f" Block time tomorrow for: {q2_nudge['title']}" if q2_nudge else " Plan a Q2 block for tomorrow."))
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + [f"+{len(lines) - MAX_LINES + 1} more"]

    if burning:
        title = f"Daybook: {len(burning)} burning task" + ("" if len(burning) == 1 else "s")
    elif n_tasks or n_steps:
        title = f"Daybook: {n_tasks} overdue task" + ("" if n_tasks == 1 else "s") if n_tasks else "Daybook:"
        if n_steps:
            title = (title + ", " if n_tasks else "Daybook: ") + f"{n_steps} late step" + ("" if n_steps == 1 else "s")
    elif top3 or suggest:
        title = "Daybook: today's Top 3"
    elif followups:
        title = f"Daybook: {len(followups)} follow-up" + ("" if len(followups) == 1 else "s") + " due"
    elif tours:
        title = "Daybook: trip details to complete"
    elif close_day is not None:
        title = "Daybook: close your day"
    elif q2_nudge is not None:
        title = "Daybook: no Q2 work today"
    else:
        title = "Daybook: untouched tasks"
    involved = list(overdue) + [t for t, _ in late_steps]
    urgent = bool(burning) or any(importance(t) is True or (t.get("remindCount") or 0) >= 2 for t in involved)
    return title, "\n".join(lines), ("high" if urgent else "default")


# ---------------------------------------------------------------------------
# Network: Supabase REST API ("docs" table) and ntfy
# ---------------------------------------------------------------------------

def api_headers(key: str) -> dict:
    """New-style keys (sb_secret_...) go only in `apikey`; legacy keys also need Authorization."""
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


class Supabase:
    def __init__(self, url: str, service_key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = api_headers(service_key)

    def docs(self, col: str) -> list[dict]:
        """All documents of one kind, as dicts of the app's fields plus "_user" and "_id"."""
        out, start = [], 0
        while True:
            r = requests.get(f"{self.base}/docs", headers={**self.headers, "Range": f"{start}-{start + 999}"},
                             params={"select": "user_id,id,data", "col": f"eq.{col}"}, timeout=30)
            r.raise_for_status()
            rows = r.json()
            out += [{**(row["data"] or {}), "_user": row["user_id"], "_id": row["id"]} for row in rows]
            if len(rows) < 1000:
                return out
            start += 1000

    def mark_reminded(self, task: dict, now_ms: int) -> None:
        data = {k: v for k, v in task.items() if not k.startswith("_")}
        data.update({"remindCount": (task.get("remindCount") or 0) + 1, "lastReminded": now_ms})
        r = requests.patch(f"{self.base}/docs", headers={**self.headers, "Prefer": "return=minimal"},
                           params={"user_id": f"eq.{task['_user']}", "col": "eq.tasks", "id": f"eq.{task['_id']}"},
                           json={"data": data, "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=30)
        r.raise_for_status()


def send_push(topic: str, title: str, body: str, priority: str, click_url: str | None) -> None:
    headers = {"Title": title, "Priority": priority, "Tags": "alarm_clock"}
    if click_url:
        headers["Click"] = click_url
    r = requests.post(f"{NTFY_SERVER}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"Missing setting {name}. Add it as a GitHub Secret (see the setup guide).")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send Daybook reminders.")
    parser.add_argument("--dry-run", action="store_true", help="print the reminder, send nothing, change nothing")
    parser.add_argument("--ignore-quiet", action="store_true", help="run even during quiet hours (for testing)")
    args = parser.parse_args(argv)

    tz = ZoneInfo(os.environ.get("TIMEZONE") or "Asia/Kolkata")
    now = datetime.now(tz)
    db = Supabase(env("SUPABASE_URL"), env("SUPABASE_SERVICE_KEY"))
    tasks = db.docs("tasks")  # also counts as activity, keeping the free project awake
    print(f"{now:%Y-%m-%d %H:%M %Z}: {len(tasks)} task(s)")

    if is_quiet(now, int(os.environ.get("QUIET_START") or 22), int(os.environ.get("QUIET_END") or 8)) and not args.ignore_quiet:
        print("Quiet hours. No reminder sent.")
        return 0

    overdue, late = find_overdue(tasks, now), find_late_steps(tasks, now)
    untouched = find_untouched(tasks, now)
    tours = find_incomplete_tours(db.docs("tours"), now) if now.hour >= 20 else []
    burning, followups = find_burning(tasks, now), find_followups(tasks, now)
    today = now.date().isoformat()
    days = db.docs("days") if now.hour < 12 or now.hour >= 17 else []
    day_doc = next((d for d in days if d.get("_id") == today), None)
    top3, suggest = top3_today(tasks, day_doc, now) if now.hour < 12 else ([], [])
    close_day = None
    if now.hour >= 17 and not (day_doc or {}).get("closed"):
        close_day = open_due_count(tasks, now)
    q2_nudge = None
    if now.hour >= 20 and not q2_done_today(tasks, now):
        q2_nudge = next_q2(tasks, now) or False

    if not (overdue or late or untouched or tours or close_day is not None or burning or top3 or suggest
            or followups or q2_nudge is not None):
        print("Nothing to remind.")
        return 0
    title, body, priority = build_message(overdue, now, late, untouched, tours, close_day,
                                          burning=burning, top3=top3, suggest=suggest,
                                          followups=followups, q2_nudge=q2_nudge)
    print(f"{title} [{priority}]\n{body}")
    if args.dry_run:
        print("(dry run: nothing sent or changed)")
        return 0

    send_push(env("NTFY_TOPIC"), title, body + "\nOpen Daybook to act on these.", priority, os.environ.get("APP_URL") or None)
    now_ms = int(time.time() * 1000)
    marked = {t["_id"]: t for t in list(overdue) + [t for t, _ in late]}
    for t in marked.values():
        db.mark_reminded(t, now_ms)
    print(f"Sent. Updated {len(marked)} task(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
