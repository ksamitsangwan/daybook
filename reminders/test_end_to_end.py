"""One full reminder run and one backup/restore against a simulated Supabase and ntfy."""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import reminders

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import backup  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class FakeSupabase:
    """Just enough of Supabase's REST API (the docs table) and ntfy."""

    def __init__(self, rows):
        self.rows, self.pushes, self.patches = rows, [], []

    def get(self, url, headers=None, params=None, timeout=None):
        col = (params or {}).get("col", "").replace("eq.", "")
        start, end = map(int, headers["Range"].split("-"))
        data = [r for r in self.rows if not col or r["col"] == col][start:end + 1]
        return mock.Mock(json=lambda: json.loads(json.dumps(data)), raise_for_status=lambda: None)

    def patch(self, url, headers=None, params=None, json=None, timeout=None):
        self.patches.append((params, json))
        for r in self.rows:
            if r["col"] == "tasks" and r["id"] == params["id"].replace("eq.", ""):
                r["data"] = json["data"]
        return mock.Mock(raise_for_status=lambda: None)

    def post(self, url, data=None, headers=None, json=None, params=None, timeout=None):
        if "ntfy.sh" in url:
            self.pushes.append((headers, data.decode()))
        else:
            for row in json:
                self.rows = [r for r in self.rows if not (r["col"] == row["col"] and r["id"] == row["id"])] + [row]
        return mock.Mock(raise_for_status=lambda: None)


def rows():
    ms = datetime(2026, 10, 27, 20, 36, tzinfo=IST).timestamp() * 1000
    return [
        {"user_id": "u1", "col": "tasks", "id": "t1", "data": {"title": "Reply on NFR file", "status": "open", "due": "2026-10-25", "priority": 1, "remindCount": 1, "createdAt": ms}},
        {"user_id": "u1", "col": "tasks", "id": "t2", "data": {"title": "Lease note", "status": "open", "createdAt": ms - 8 * 86_400_000}},
        {"user_id": "u1", "col": "tours", "id": "r1", "data": {"date": "2026-10-27", "status": "done", "type": "Local", "startOdo": 100, "stops": [{"place": "Phul", "arrive": "10:00", "depart": "11:00", "km": 20}]}},
        {"user_id": "u1", "col": "days", "id": "2026-10-26", "data": {"closed": True}},
    ]


def run(monkeypatch, fake, when):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_test")
    monkeypatch.setenv("NTFY_TOPIC", "daybook-test")
    monkeypatch.setenv("APP_URL", "https://you.github.io/daybook/")
    for name in ("get", "patch", "post"):
        monkeypatch.setattr(reminders.requests, name, getattr(fake, name))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when
    monkeypatch.setattr(reminders, "datetime", Clock)
    return reminders.main([])


def test_evening_run_sends_one_push_and_counts_reminders(monkeypatch):
    fake = FakeSupabase(rows())
    assert run(monkeypatch, fake, datetime(2026, 10, 27, 20, 36, tzinfo=IST)) == 0
    assert len(fake.pushes) == 1
    headers, body = fake.pushes[0]
    assert headers["Title"] == "Daybook: 1 overdue task" and headers["Priority"] == "high" and headers["Click"].startswith("https://")
    assert body.splitlines() == [
        "• Reply on NFR file — 2d late (reminder #2)",
        "Untouched:", "• Lease note — 8 days",
        "Trip details to complete:", "• Tue 27 Oct — 1 item missing",
        "Close your day in Daybook: 1 item still open for today",
        "No Q2 work today (important, not urgent). Plan a Q2 block for tomorrow.",
        "Open Daybook to act on these."]
    task = next(r for r in fake.rows if r["id"] == "t1")["data"]
    assert task["remindCount"] == 2 and task["title"] == "Reply on NFR file" and "lastReminded" in task


def test_quiet_hours_send_nothing(monkeypatch):
    fake = FakeSupabase(rows())
    assert run(monkeypatch, fake, datetime(2026, 10, 27, 23, 0, tzinfo=IST)) == 0
    assert fake.pushes == [] and fake.patches == []


def test_backup_then_restore(monkeypatch, tmp_path):
    fake = FakeSupabase(rows())
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_test")
    monkeypatch.setattr(backup.requests, "get", fake.get)
    monkeypatch.setattr(backup.requests, "post", fake.post)
    path = backup.backup(str(tmp_path / "b.json"))
    saved = json.loads(Path(path).read_text())
    assert saved["tasks"]["t1"]["title"] == "Reply on NFR file" and saved["tours"]["r1"]["stops"][0]["km"] == 20 and saved["userIds"] == ["u1"]
    fake.rows = []
    assert backup.restore(path, None) == 4
    assert {r["col"] for r in fake.rows} == {"tasks", "tours", "days"}
