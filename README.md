# Classroom

A small classroom management app for courses, student enrollment, fees, schedules, and class capacity. Local development uses SQLite; the Vercel deployment uses Supabase Postgres through a server-side API.

## Run locally

1. Open a terminal in this project folder.
2. Start the app server:

   ```sh
   python3 server.py
   ```

3. Open <http://127.0.0.1:8000/index.html> in your browser.

Keep the server running while using the app. Course and student records are stored in `classroom.sqlite3` on this computer; that database is intentionally excluded from Git.

## Supabase and Vercel

`supabase/schema.sql` creates the app tables, enables row-level security, and grants the server role access. It has been applied to the project referenced in `.env.example`. The existing local courses and students were imported into the `courses` and `students` tables.

The Vercel API in `api/[...path].py` uses the Supabase REST API. Set these server-side Vercel environment variables for Production, Preview, and Development:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (server only; never use in browser code)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_VERIFY_SERVICE_SID` for phone OTP sign-up
- `TWILIO_SMS_FROM` and/or `TWILIO_WHATSAPP_FROM` if student notifications are enabled

Import this repository as a new Vercel project with the project root set to this folder. The app is static HTML with Python serverless API functions and needs no build command. Local SQLite files and `.env` are excluded from deployment. Local development continues to run with `python3 server.py` and SQLite.

## Demo sign-in

- `admin` / `learn123`
- `teacher` / `class123`
- `staff` / `welcome1`

Demo accounts are for local development only. Do not create them in the hosted database.

## Optional SMS and phone verification

Copy `.env.example` to `.env`, add your own Twilio credentials, then restart `server.py`. Keep `.env` private; it is excluded from Git.
