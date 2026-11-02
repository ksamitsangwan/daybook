# Daybook

A personal planner and tour log you install on your iPhone and Mac. It runs on free services you control:

- **Priorities (Covey's time matrix):** every task lands in Q1–Q4 from *important?* (your call) and *urgent?* (from the due date, with override and 🔥 burning), a Matrix view, a daily **Top 3** (80/20) and a protected **Q2 block**, delegation with follow-up dates, a weekly quadrant report, and weekly big rocks by role
- **Time-blocked days** with a timeline, a "now" line and a load bar that warns when the day doesn't fit
- **Tasks with steps**, saved step lists for standard processes, and flags for tasks that are untouched or keep getting pushed back
- **Tour log**: stops, activities, observations, follow-ups that become tasks, odometer or km per leg
- **Office journey file**: fills the "Journey details Upload - Outstation" template, one row per leg
- **Close the day**, today's record, habits, focus timer, weekly review and a track record of where you're slipping
- **Reminders** on your phone at 8:36 am, 12:36 pm, 5:36 pm and 8:36 pm IST until things are closed

| Part | Runs on | Language |
|---|---|---|
| App (`docs/`) | GitHub Pages; install with *Add to Home Screen* | HTML + JavaScript |
| Database + login | Supabase (PostgreSQL, free tier), one `docs` table | SQL (`schema.sql`) |
| Reminder service (`reminders/`) | GitHub Actions, on a schedule | Python |
| Push notifications | ntfy (open source, free iOS app) | – |
| Backups (`tools/backup.py`) | Your Mac | Python |

## Files

```
schema.sql                      run once in Supabase SQL Editor (one table, row-level security, live sync)
docs/index.html                 the app
docs/config.js                  your Supabase URL + publishable key go here
docs/manifest.webmanifest       home-screen app settings
reminders/reminders.py          burning, Top 3, overdue, late steps, follow-ups, untouched, close the day, Q2, trip details
reminders/test_*.py             28 tests (python -m pytest reminders)
tools/backup.py                 export/restore all data as JSON (same format as the app's Export button)
.github/workflows/reminders.yml runs reminders.py 4 times a day
.github/workflows/keepalive.yml stops GitHub from pausing the schedule
```

## How data is stored

Every item is one row in `docs`: `(user_id, col, id, data)`, where `col` is the kind (tasks, tours, habits, days,
templates, places, focus, reviews) and `data` is JSON. New fields need no database change. Row-level security
means a signed-in person only ever sees their own rows.

## Run the Python parts on your Mac

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest reminders                      # 28 tests
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="sb_secret_..."
python reminders/reminders.py --dry-run --ignore-quiet
python tools/backup.py
```

Never commit the secret key (`sb_secret_...`). It belongs only in GitHub Secrets and your own Terminal.

Full setup steps are in the setup guide.

## License

MIT. Use and change it freely.
