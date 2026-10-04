#!/usr/bin/env python3
"""Local Classroom app server with a persistent SQLite database."""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "classroom.sqlite3"
PORT = 8000


def load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        os.environ.setdefault(key.strip(), value)


load_local_env()

SEED_COURSES = [
    ("Web Design Fundamentals", "Design", 14500, "Mon, Wed, Fri · 10:00 AM", 2, 24, 20),
    ("Digital Marketing", "Business", 12000, "Tue, Thu · 2:00 PM", 1.5, 20, 18),
    ("Computer Basics", "Technology", 8500, "Mon–Fri · 9:00 AM", 1, 30, 24),
    ("Spoken English", "Languages", 9500, "Sat, Sun · 11:00 AM", 2, 16, 16),
]


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def initialize_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                name TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                timing TEXT NOT NULL DEFAULT '',
                hours REAL NOT NULL DEFAULT 0,
                classes INTEGER NOT NULL DEFAULT 0,
                seats INTEGER NOT NULL DEFAULT 0
                ,payment_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                course TEXT NOT NULL DEFAULT '',
                payment REAL NOT NULL DEFAULT 0,
                date TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE,
                phone TEXT UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_signups (
                phone TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS otp_rate_limits (
                phone TEXT PRIMARY KEY,
                requested_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                channel TEXT NOT NULL,
                message TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                queued_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        # Add the optional course checkout link to databases created by older versions.
        course_columns = {row[1] for row in db.execute("PRAGMA table_info(courses)")}
        if "payment_url" not in course_columns:
            db.execute("ALTER TABLE courses ADD COLUMN payment_url TEXT NOT NULL DEFAULT ''")
        if db.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO courses(name,category,fee,timing,hours,classes,seats) VALUES(?,?,?,?,?,?,?)",
                SEED_COURSES,
            )
        demo_accounts = [
            ("admin", "Alex Morgan", "admin@classroom.local", "+910000000001", "learn123"),
            ("teacher", "Jordan Lee", "teacher@classroom.local", "+910000000002", "class123"),
            ("staff", "Taylor Kim", "staff@classroom.local", "+910000000003", "welcome1"),
        ]
        for username, name, email, phone, password in demo_accounts:
            if not db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                salt, password_hash = hash_password(password)
                db.execute(
                    "INSERT INTO accounts(username,full_name,email,phone,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    (username, name, email, phone, salt, password_hash, int(time.time())),
                )


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310_000).hex()
    return salt, digest


def new_session(db: sqlite3.Connection, account_id: int) -> str:
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.execute("INSERT INTO sessions(token_hash,account_id,expires_at) VALUES(?,?,?)", (token_hash, account_id, int(time.time()) + 43200))
    return token


def account_for_token(db: sqlite3.Connection, token: str | None):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return db.execute(
        "SELECT accounts.* FROM sessions JOIN accounts ON accounts.id=sessions.account_id WHERE sessions.token_hash=? AND sessions.expires_at>?",
        (token_hash, int(time.time())),
    ).fetchone()


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s().-]", "", value)
    if phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone[1:]) <= 15:
        return phone
    raise ValueError("Enter your mobile number with country code, such as +91 98765 43210.")


def normalize_student_message_phone(value: str) -> str:
    phone = re.sub(r"[\s().-]", "", value)
    # This classroom is configured for India (fees are in INR); accept standard
    # ten-digit Indian mobile numbers while still allowing explicit country codes.
    if re.fullmatch(r"[6-9]\d{9}", phone):
        return "+91" + phone
    return normalize_phone(phone)


def twilio_verify(path: str, values: dict[str, str]) -> dict:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    service_sid = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "").strip()
    if not account_sid or not auth_token or not service_sid:
        raise RuntimeError("Phone OTP is not configured. Add the Twilio Verify settings to the server environment and restart or redeploy.")
    url = f"https://verify.twilio.com/v2/Services/{service_sid}/{path}"
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    request = Request(url, data=urlencode(values).encode(), headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("message", "SMS verification request failed.")
        except Exception:
            detail = "SMS verification request failed."
        raise RuntimeError(detail) from exc
    except URLError as exc:
        raise RuntimeError("Could not reach the SMS provider. Check the server network connection.") from exc


def twilio_send_message(channel: str, phone: str, body: str) -> dict:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender_key = "TWILIO_SMS_FROM" if channel == "sms" else "TWILIO_WHATSAPP_FROM"
    sender = os.environ.get(sender_key, "").strip()
    missing_settings = [key for key, value in (("TWILIO_ACCOUNT_SID", account_sid), ("TWILIO_AUTH_TOKEN", auth_token), (sender_key, sender)) if not value]
    if missing_settings:
        raise RuntimeError(f"Messaging is not configured. Add {', '.join(missing_settings)} to the server environment and restart or redeploy.")
    normalized_phone = normalize_phone(phone)
    if channel == "whatsapp":
        if not sender.startswith("whatsapp:+"):
            raise RuntimeError("TWILIO_WHATSAPP_FROM must be an approved WhatsApp sender, such as whatsapp:+14155238886.")
        recipient = f"whatsapp:{normalized_phone}"
    else:
        if not sender.startswith("+") or not sender[1:].isdigit():
            raise RuntimeError("TWILIO_SMS_FROM must be a phone number in international format.")
        recipient = normalized_phone
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    request = Request(url, data=urlencode({"To": recipient, "From": sender, "Body": body}).encode(), headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("message", "Message could not be submitted.")
        except Exception:
            detail = "Message could not be submitted."
        raise RuntimeError(detail) from exc
    except URLError as exc:
        raise RuntimeError("Could not reach Twilio. Check the server network connection.") from exc


class ClassroomHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def bearer_account(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else None
        with connect_db() as db:
            return account_for_token(db, token)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/data/changes":
            try:
                if not self.bearer_account():
                    return self.send_json(401, {"error": "Please sign in again."})
                body = self.read_json()
                courses = body.get("courses", [])
                students = body.get("students", [])
                with connect_db() as db:
                    db.executemany(
                        "INSERT INTO courses(name,category,fee,timing,hours,classes,seats,payment_url) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET category=excluded.category,fee=excluded.fee,timing=excluded.timing,hours=excluded.hours,classes=excluded.classes,seats=excluded.seats,payment_url=excluded.payment_url",
                        [(
                            str(c.get("name", "")).strip(), str(c.get("category", "Technology")),
                            float(c.get("fee", 0) or 0), str(c.get("timing", "")),
                            float(c.get("hours", 0) or 0), int(c.get("classes", 0) or 0),
                            int(c.get("seats", 0) or 0), str(c.get("payment_url", "")).strip(),
                        ) for c in courses if str(c.get("name", "")).strip()]
                    )
                    db.executemany(
                        "INSERT INTO students(id,name,phone,email,course,payment,date) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name=excluded.name,phone=excluded.phone,email=excluded.email,course=excluded.course,payment=excluded.payment,date=excluded.date",
                        [(
                            int(s.get("id") or index + 1), str(s.get("name", "")).strip(),
                            str(s.get("phone", "")), str(s.get("email", "")), str(s.get("course", "")),
                            float(s.get("payment", 0) or 0), str(s.get("date", "")),
                        ) for index, s in enumerate(students) if str(s.get("name", "")).strip()]
                    )
                    db.executemany("DELETE FROM courses WHERE name=?", [(str(name),) for name in body.get("deletedCourseNames", [])])
                    db.executemany("DELETE FROM students WHERE id=?", [(int(student_id),) for student_id in body.get("deletedStudentIds", [])])
                return self.send_json(200, {"saved": True})
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
                return self.send_json(400, {"error": f"Could not save data: {exc}"})
        if path == "/api/notifications/send":
            try:
                if not self.bearer_account():
                    return self.send_json(401, {"error": "Please sign in again."})
                body = self.read_json()
                kind = str(body.get("kind", "general"))
                channel = str(body.get("channel", "sms")).lower()
                message = str(body.get("message", "")).strip()
                if kind not in ("general", "announcement", "schedule", "payment"):
                    return self.send_json(400, {"error": "Choose a valid notification type."})
                if body.get("permission_confirmed") is not True:
                    return self.send_json(400, {"error": "Confirm that these students agreed to receive messages on this channel."})
                if channel not in ("sms", "whatsapp"):
                    return self.send_json(400, {"error": "Choose SMS or WhatsApp."})
                if not message or len(message) > 1500:
                    return self.send_json(400, {"error": "Write a message from 1 to 1,500 characters."})
                # Check sender configuration before attempting any individual send.
                sender_key = "TWILIO_SMS_FROM" if channel == "sms" else "TWILIO_WHATSAPP_FROM"
                missing_settings = [key for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", sender_key) if not os.environ.get(key, "").strip()]
                if missing_settings:
                    return self.send_json(503, {"error": f"Messaging is not configured. Add {', '.join(missing_settings)} to the server environment and restart or redeploy."})
                with connect_db() as db:
                    all_students = [dict(row) for row in db.execute("SELECT name,phone,course FROM students WHERE trim(phone)<>'' ORDER BY id")]
                recipients = []
                invalid_count = 0
                for student in all_students:
                    try:
                        student["phone"] = normalize_student_message_phone(student["phone"])
                        recipients.append(student)
                    except ValueError:
                        invalid_count += 1
                if not recipients:
                    return self.send_json(400, {"error": "No students have a valid international phone number (+country code)."})
                if len(recipients) > 200:
                    return self.send_json(400, {"error": "This send is limited to 200 students. Send a smaller group at a time."})
                queued = 0
                failed = invalid_count
                for student in recipients:
                    personalized_message = message.replace("{name}", student["name"]).replace("{course}", student["course"])
                    try:
                        result = twilio_send_message(channel, student["phone"], personalized_message)
                        queued += 1 if result.get("sid") else 0
                        if not result.get("sid"):
                            failed += 1
                    except RuntimeError:
                        failed += 1
                target_count = len(all_students)
                now = int(time.time())
                with connect_db() as db:
                    notification_id = db.execute(
                        "INSERT INTO notifications(kind,channel,message,target_count,queued_count,failed_count,created_at) VALUES(?,?,?,?,?,?,?)",
                        (kind, channel, message, target_count, queued, failed, now),
                    ).lastrowid
                return self.send_json(200, {"id": notification_id, "targetCount": target_count, "queuedCount": queued, "failedCount": failed})
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error, RuntimeError) as exc:
                self.send_json(400 if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)) else 503, {"error": str(exc)})
            return
        if path == "/api/public/enroll":
            try:
                body = self.read_json()
                course_name = str(body.get("course", "")).strip()
                name = str(body.get("name", "")).strip()
                phone = str(body.get("phone", "")).strip()[:40]
                email = str(body.get("email", "")).strip()[:254]
                if not course_name or not name or not phone:
                    return self.send_json(400, {"error": "Enter your name and phone number."})
                with connect_db() as db:
                    course = db.execute("SELECT * FROM courses WHERE name=?", (course_name,)).fetchone()
                    if not course:
                        return self.send_json(404, {"error": "This course is no longer available."})
                    enrolled = db.execute("SELECT COUNT(*) FROM students WHERE course=?", (course_name,)).fetchone()[0]
                    if course["seats"] and enrolled >= course["seats"]:
                        return self.send_json(409, {"error": "This class is full. Contact the institute for the next batch."})
                    duplicate = db.execute("SELECT 1 FROM students WHERE course=? AND phone=?", (course_name, phone)).fetchone()
                    if duplicate:
                        return self.send_json(409, {"error": "This phone number is already registered for this course."})
                    db.execute(
                        "INSERT INTO students(name,phone,email,course,payment,date) VALUES(?,?,?,?,0,?)",
                        (name, phone, email, course_name, time.strftime("%-d %b %Y")),
                    )
                    payment_url = str(course["payment_url"] or "")
                return self.send_json(201, {"registered": True, "paymentUrl": payment_url})
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
                return self.send_json(400, {"error": f"Could not register: {exc}"})
        if path not in ("/api/login", "/api/signup/start", "/api/signup/verify"):
            return self.send_json(404, {"error": "Not found"})
        try:
            body = self.read_json()
            if path == "/api/login":
                identity = str(body.get("identity", "")).strip().lower()
                password = str(body.get("password", ""))
                with connect_db() as db:
                    account = db.execute(
                        "SELECT * FROM accounts WHERE lower(username)=? OR lower(email)=? OR phone=?",
                        (identity, identity, identity),
                    ).fetchone()
                    if not account:
                        return self.send_json(401, {"error": "Email/phone or password is incorrect."})
                    _, digest = hash_password(password, account["password_salt"])
                    if not hmac.compare_digest(digest, account["password_hash"]):
                        return self.send_json(401, {"error": "Email/phone or password is incorrect."})
                    token = new_session(db, account["id"])
                return self.send_json(200, {"token": token, "account": {"name": account["full_name"], "email": account["email"], "phone": account["phone"]}})

            if path == "/api/signup/start":
                phone = normalize_phone(str(body.get("phone", "")))
                name = str(body.get("name", "")).strip()
                email = str(body.get("email", "")).strip().lower()
                password = str(body.get("password", ""))
                if not name or not email or "@" not in email:
                    return self.send_json(400, {"error": "Enter your name and a valid email address."})
                if len(password) < 8:
                    return self.send_json(400, {"error": "Choose a password with at least 8 characters."})
                with connect_db() as db:
                    duplicate = db.execute("SELECT 1 FROM accounts WHERE phone=? OR lower(email)=?", (phone, email)).fetchone()
                    if duplicate:
                        return self.send_json(409, {"error": "An account already uses that phone or email."})
                    last = db.execute("SELECT requested_at FROM otp_rate_limits WHERE phone=?", (phone,)).fetchone()
                    wait = int(last["requested_at"]) + 60 - int(time.time()) if last else 0
                    if wait > 0:
                        return self.send_json(429, {"error": f"Please wait {wait} seconds before requesting another code."})
                salt, password_hash = hash_password(password)
                twilio_verify("Verifications", {"To": phone, "Channel": "sms"})
                with connect_db() as db:
                    db.execute("INSERT OR REPLACE INTO otp_rate_limits(phone,requested_at) VALUES(?,?)", (phone, int(time.time())))
                    db.execute(
                        "INSERT OR REPLACE INTO pending_signups(phone,full_name,email,password_salt,password_hash,expires_at) VALUES(?,?,?,?,?,?)",
                        (phone, name, email, salt, password_hash, int(time.time()) + 600),
                    )
                return self.send_json(200, {"sent": True, "phone": phone})

            phone = normalize_phone(str(body.get("phone", "")))
            code = re.sub(r"\s", "", str(body.get("code", "")))
            if not re.fullmatch(r"\d{4,10}", code):
                return self.send_json(400, {"error": "Enter the verification code from your text message."})
            with connect_db() as db:
                pending = db.execute("SELECT * FROM pending_signups WHERE phone=?", (phone,)).fetchone()
                if not pending or pending["expires_at"] < int(time.time()):
                    if pending:
                        db.execute("DELETE FROM pending_signups WHERE phone=?", (phone,))
                    return self.send_json(400, {"error": "This signup code expired. Request a new code."})
            result = twilio_verify("VerificationCheck", {"To": phone, "Code": code})
            if result.get("status") != "approved":
                return self.send_json(400, {"error": "That code is incorrect or expired. Try again."})
            with connect_db() as db:
                account_id = db.execute(
                    "INSERT INTO accounts(full_name,email,phone,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (pending["full_name"], pending["email"], phone, pending["password_salt"], pending["password_hash"], int(time.time())),
                ).lastrowid
                token = new_session(db, account_id)
                db.execute("DELETE FROM pending_signups WHERE phone=?", (phone,))
            return self.send_json(201, {"token": token, "account": {"name": pending["full_name"], "email": pending["email"], "phone": phone}})
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error, RuntimeError) as exc:
            self.send_json(400 if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)) else 503, {"error": str(exc)})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/notifications":
            if not self.bearer_account():
                return self.send_json(401, {"error": "Please sign in again."})
            with connect_db() as db:
                rows = [dict(row) for row in db.execute("SELECT id,kind,channel,message,target_count,queued_count,failed_count,created_at FROM notifications ORDER BY id DESC LIMIT 50")]
            return self.send_json(200, {"notifications": rows})
        if path == "/api/public/course":
            course_name = urlparse(self.path).query
            from urllib.parse import parse_qs
            course_name = parse_qs(course_name).get("name", [""])[0]
            with connect_db() as db:
                course = db.execute("SELECT name,category,fee,timing,hours,classes,seats FROM courses WHERE name=?", (course_name,)).fetchone()
            if not course:
                return self.send_json(404, {"error": "Course not found."})
            return self.send_json(200, dict(course))
        if path != "/api/data":
            return super().do_GET()
        if not self.bearer_account():
            return self.send_json(401, {"error": "Please sign in again."})
        with connect_db() as db:
            courses = [dict(row) for row in db.execute("SELECT * FROM courses ORDER BY name")]
            students = [dict(row) for row in db.execute("SELECT * FROM students ORDER BY id")]
        self.send_json(200, {"courses": courses, "students": students})

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/data":
            return self.send_json(404, {"error": "Not found"})
        try:
            if not self.bearer_account():
                return self.send_json(401, {"error": "Please sign in again."})
            payload = self.read_json()
            courses = payload.get("courses", [])
            students = payload.get("students", [])
            for course in courses:
                payment_url = str(course.get("payment_url", "")).strip()
                if payment_url:
                    parsed_payment_url = urlparse(payment_url)
                    if parsed_payment_url.scheme != "https" or not parsed_payment_url.netloc:
                        return self.send_json(400, {"error": "Payment links must use a secure https:// address."})
            with connect_db() as db:
                db.execute("DELETE FROM students")
                db.execute("DELETE FROM courses")
                db.executemany(
                    "INSERT INTO courses(name,category,fee,timing,hours,classes,seats,payment_url) VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            str(c.get("name", "")).strip(),
                            str(c.get("category", "Technology")),
                            float(c.get("fee", 0) or 0),
                            str(c.get("timing", "")),
                            float(c.get("hours", 0) or 0),
                            int(c.get("classes", 0) or 0),
                            int(c.get("seats", 0) or 0),
                            str(c.get("payment_url", "")).strip(),
                        )
                        for c in courses
                        if str(c.get("name", "")).strip()
                    ],
                )
                db.executemany(
                    "INSERT INTO students(id,name,phone,email,course,payment,date) VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            int(s.get("id") or index + 1),
                            str(s.get("name", "")).strip(),
                            str(s.get("phone", "")),
                            str(s.get("email", "")),
                            str(s.get("course", "")),
                            float(s.get("payment", 0) or 0),
                            str(s.get("date", "")),
                        )
                        for index, s in enumerate(students)
                        if str(s.get("name", "")).strip()
                    ],
                )
            self.send_json(200, {"saved": True})
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
            self.send_json(400, {"error": f"Could not save data: {exc}"})


if __name__ == "__main__":
    initialize_db()
    print(f"Classroom is running at http://localhost:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    ThreadingHTTPServer(("127.0.0.1", PORT), ClassroomHandler).serve_forever()
