# Allowance Management — Smart Personal Finance Manager

Single-file Flask + SQLite personal finance app (Pydroid 3 compatible).
Everything — backend, HTML, CSS, JS, and the database layer — lives in `main.py`.

## Features

- Register / Login with email verification (6-digit code, Gmail SMTP, 10-minute expiry)
- Login is blocked until the user's email is verified
- Dashboard Welcome Card with a **Cover Photo background** (no video — removed for
  speed/stability on mobile), dark overlay for text readability, smooth fade-in
- Separate **Profile Photo** and **Cover Photo** upload/remove in Settings
  (both stored as Base64 strings directly in SQLite — no uploads folder)
- Allowance / Expense tracking, Budgets, Savings Goals, Reports, CSV/Print export
- Modern blue glassmorphism UI, mobile responsive

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`

## Local Setup

```bash
git clone <your-repo-url>
cd <repo>
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your real values
python main.py
```

The app starts on `http://127.0.0.1:5000` (or the `PORT` you set).
The SQLite database (`allowance.db` by default) is created automatically on
first run.

## Environment Variables

See `.env.example` for the full list. The important ones:

| Variable             | Purpose                                                        |
|-----------------------|------------------------------------------------------------------|
| `SECRET_KEY`          | Flask session secret. Set this in production.                   |
| `DB_PATH`             | Path to the SQLite file. On Render, point this at a persistent disk. |
| `SMTP_SERVER` / `SMTP_PORT` | Gmail SMTP host/port (defaults: `smtp.gmail.com` / `587`). |
| `EMAIL_ADDRESS`       | Gmail address used to send verification codes.                  |
| `EMAIL_APP_PASSWORD`  | Gmail **App Password** (not your normal password) — [create one here](https://myaccount.google.com/apppasswords). |

If `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` are left blank, registration still
works and a code is generated and stored, but no email is actually sent
(useful for local/offline testing).

## Deploying to Render

1. Push this repo to GitHub.
2. Create a new **Web Service** on Render, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn main:app` (already set in the `Procfile`).
5. Add a **Persistent Disk** (e.g. mounted at `/var/data`) so the SQLite
   database survives redeploys, then set `DB_PATH=/var/data/allowance.db`.
6. Add the environment variables listed above (`SECRET_KEY`, `EMAIL_ADDRESS`,
   `EMAIL_APP_PASSWORD`, etc.) in the Render dashboard.

Without a persistent disk, Render's filesystem is ephemeral and the database
resets on every deploy.

## Project Structure

```
.
├── main.py           # entire app: routes, DB layer, inline HTML templates
├── requirements.txt
├── Procfile           # gunicorn start command for Render/Heroku-style hosts
├── .env.example        # copy to .env and fill in real values
└── .gitignore
```

## Troubleshooting: Verification Email Not Sending

1. **Make sure you copied `.env.example` to `.env`** and filled in real values —
   the app loads `.env` automatically via `python-dotenv` (included in
   `requirements.txt`). If `python-dotenv` isn't installed, `.env` is silently
   skipped, so run `pip install -r requirements.txt` again if unsure.
2. **`EMAIL_APP_PASSWORD` must be a Gmail App Password**, not your normal
   Gmail login password. Generate one at
   <https://myaccount.google.com/apppasswords> (requires 2-Step Verification
   to be enabled on the Google account first).
3. **Check the server console/terminal** where `python main.py` is running —
   on failure it prints the real reason, e.g. `[email] Failed to send
   verification email to ...: (535, b'5.7.8 Username and Password not
   accepted...')`, which almost always means the app password is wrong or
   2-Step Verification isn't enabled.
4. If `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` are left blank, the app still
   generates and stores a verification code (visible in the `users` table)
   but intentionally does not attempt to send anything — useful for local
   testing without a real Gmail account.
5. On Render/other hosts, set `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` as
   actual environment variables in the dashboard (there's no `.env` file
   there — the platform injects them directly).

## Notes

- No separate `uploads/` folder — profile and cover photos are stored as
  Base64 data URIs directly in the `users` table.
- The old video-background feature has been fully removed (no
  `bg_video_filename` column, no video streaming route).
