# Classroom

A small classroom management app for courses, student enrollment, fees, schedules, and class capacity. The app uses a local SQLite database.

## Run locally

1. Open a terminal in this project folder.
2. Start the app server:

   ```sh
   python3 server.py
   ```

3. Open <http://127.0.0.1:8000/index.html> in your browser.

Keep the server running while using the app. Course and student records are stored in `classroom.sqlite3` on this computer; that database is intentionally excluded from Git.

## Demo sign-in

- `admin` / `learn123`
- `teacher` / `class123`
- `staff` / `welcome1`

These are development demo accounts. Change them before exposing the app to other people.

## Optional SMS and phone verification

Copy `.env.example` to `.env`, add your own Twilio credentials, then restart `server.py`. Keep `.env` private; it is excluded from Git.
