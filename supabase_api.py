"""Vercel serverless API backed by the private Supabase Data API."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from server import normalize_phone, normalize_student_message_phone, twilio_send_message, twilio_verify


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def rest(table: str, method: str = "GET", params: dict | None = None, payload=None, prefer: str | None = None):
    base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not base or not key:
        raise ApiError(503, "Supabase server settings are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to Vercel Environment Variables.")
    query = urlencode(params or {}, doseq=True)
    url = f"{base}/rest/v1/{table}" + (f"?{query}" if query else "")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("message") or detail.get("details") or "Supabase request failed."
        except Exception:
            message = "Supabase request failed."
        raise ApiError(503 if exc.code >= 500 else 400, message) from exc
    except (URLError, TimeoutError) as exc:
        raise ApiError(503, "Could not connect to Supabase. Check the Vercel environment settings and try again.") from exc


def one(table: str, **filters):
    params = {"select": "*", **{key: f"eq.{value}" for key, value in filters.items()}}
    rows = rest(table, params=params)
    return rows[0] if rows else None


def session_account(token: str | None):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session = rest("sessions", params={"select": "account_id", "token_hash": f"eq.{token_hash}", "expires_at": f"gt.{int(time.time())}"})
    if not session:
        return None
    return one("accounts", id=session[0]["account_id"])


def new_session(account_id: int) -> str:
    token = secrets.token_urlsafe(36)
    rest("sessions", "POST", payload={
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "account_id": account_id,
        "expires_at": int(time.time()) + 43200,
    })
    return token


class ClassroomApiHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # Do not log request bodies, credentials, or student contact details.
        return

    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64000:
            raise ApiError(413, "Request is too large.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "Request body must be valid JSON.") from exc

    def signed_in(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else None
        account = session_account(token)
        if not account:
            raise ApiError(401, "Please sign in again.")
        return account

    def run(self, action):
        try:
            action()
        except ApiError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid request."})
        except RuntimeError as exc:
            self.send_json(503, {"error": str(exc)})
        except Exception:
            self.send_json(503, {"error": "The server could not complete the request."})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        self.run(self._get)

    def _get(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            rest("courses", params={"select": "name", "limit": "1"})
            return self.send_json(200, {"ok": True, "storage": "supabase"})
        if parsed.path == "/api/data":
            self.signed_in()
            courses = rest("courses", params={"select": "*", "order": "name.asc"})
            students = rest("students", params={"select": "*", "order": "id.asc"})
            return self.send_json(200, {"courses": courses, "students": students})
        if parsed.path == "/api/notifications":
            self.signed_in()
            rows = rest("notifications", params={"select": "id,kind,channel,message,target_count,queued_count,failed_count,created_at", "order": "id.desc", "limit": "50"})
            return self.send_json(200, {"notifications": rows})
        if parsed.path == "/api/public/course":
            name = parse_qs(parsed.query).get("name", [""])[0]
            course = one("courses", name=name)
            if not course:
                raise ApiError(404, "Course not found.")
            public = {key: course.get(key) for key in ("name", "category", "fee", "timing", "hours", "classes", "seats")}
            return self.send_json(200, public)
        raise ApiError(404, "Not found.")

    def do_POST(self):
        self.run(self._post)

    def _post(self):
        path = urlparse(self.path).path
        body = self.read_json()
        if path == "/api/data/changes":
            self.signed_in()
            courses = body.get("courses", [])
            students = body.get("students", [])
            delete_courses = body.get("deletedCourseNames", [])
            delete_students = body.get("deletedStudentIds", [])
            if not all(isinstance(value, list) for value in (courses, students, delete_courses, delete_students)):
                raise ApiError(400, "Courses, students, and delete lists must be arrays.")
            for course in courses:
                payment_url = str(course.get("payment_url", "")).strip()
                if payment_url:
                    parsed = urlparse(payment_url)
                    if parsed.scheme != "https" or not parsed.netloc:
                        raise ApiError(400, "Payment links must use a secure https:// address.")
            clean_courses = [{
                "name": str(c.get("name", "")).strip(), "category": str(c.get("category", "Technology")),
                "fee": float(c.get("fee", 0) or 0), "timing": str(c.get("timing", "")),
                "hours": float(c.get("hours", 0) or 0), "classes": int(c.get("classes", 0) or 0),
                "seats": int(c.get("seats", 0) or 0), "payment_url": str(c.get("payment_url", "")).strip(),
            } for c in courses if str(c.get("name", "")).strip()]
            clean_students = [{
                "id": int(s.get("id") or index + 1), "name": str(s.get("name", "")).strip(),
                "phone": str(s.get("phone", "")), "email": str(s.get("email", "")),
                "course": str(s.get("course", "")), "payment": float(s.get("payment", 0) or 0),
                "date": str(s.get("date", "")),
            } for index, s in enumerate(students) if str(s.get("name", "")).strip()]
            if clean_courses:
                rest("courses", "POST", {"on_conflict": "name"}, clean_courses, "resolution=merge-duplicates,return=minimal")
            for name in delete_courses:
                rest("courses", "DELETE", {"name": f"eq.{str(name)}"})
            if clean_students:
                rest("students", "POST", {"on_conflict": "id"}, clean_students, "resolution=merge-duplicates,return=minimal")
            for student_id in delete_students:
                rest("students", "DELETE", {"id": f"eq.{int(student_id)}"})
            return self.send_json(200, {"saved": True})

        if path == "/api/notifications/send":
            self.signed_in()
            kind = str(body.get("kind", "general"))
            channel = str(body.get("channel", "sms")).lower()
            message = str(body.get("message", "")).strip()
            if kind not in ("general", "announcement", "schedule", "payment"):
                raise ApiError(400, "Choose a valid notification type.")
            if body.get("permission_confirmed") is not True:
                raise ApiError(400, "Confirm that these students agreed to receive messages on this channel.")
            if channel not in ("sms", "whatsapp"):
                raise ApiError(400, "Choose SMS or WhatsApp.")
            if not message or len(message) > 1500:
                raise ApiError(400, "Write a message from 1 to 1,500 characters.")
            all_students = rest("students", params={"select": "name,phone,course", "phone": "not.is.null", "order": "id.asc"})
            all_students = [student for student in all_students if str(student.get("phone", "")).strip()]
            recipients, invalid = [], 0
            for student in all_students:
                try:
                    student["phone"] = normalize_student_message_phone(student["phone"])
                    recipients.append(student)
                except ValueError:
                    invalid += 1
            if not recipients:
                raise ApiError(400, "No students have a valid international phone number (+country code).")
            if len(recipients) > 200:
                raise ApiError(400, "This send is limited to 200 students. Send a smaller group at a time.")
            queued, failed = 0, invalid
            for student in recipients:
                text = message.replace("{name}", student["name"]).replace("{course}", student["course"])
                try:
                    queued += bool(twilio_send_message(channel, student["phone"], text).get("sid"))
                except RuntimeError:
                    failed += 1
            record = rest("notifications", "POST", payload={
                "kind": kind, "channel": channel, "message": message,
                "target_count": len(all_students), "queued_count": queued,
                "failed_count": failed, "created_at": int(time.time()),
            }, prefer="return=representation")
            return self.send_json(200, {"id": record[0]["id"], "targetCount": len(all_students), "queuedCount": queued, "failedCount": failed})

        if path == "/api/public/enroll":
            name = str(body.get("course", "")).strip()
            student_name = str(body.get("name", "")).strip()
            phone = str(body.get("phone", "")).strip()[:40]
            email = str(body.get("email", "")).strip()[:254]
            if not name or not student_name or not phone:
                raise ApiError(400, "Enter your name and phone number.")
            course = one("courses", name=name)
            if not course:
                raise ApiError(404, "This course is no longer available.")
            enrolled = rest("students", params={"select": "id", "course": f"eq.{name}"})
            if course.get("seats") and len(enrolled) >= int(course["seats"]):
                raise ApiError(409, "This class is full. Contact the institute for the next batch.")
            if one("students", course=name, phone=phone):
                raise ApiError(409, "This phone number is already registered for this course.")
            rest("students", "POST", payload={
                "name": student_name, "phone": phone, "email": email, "course": name,
                "payment": 0, "date": time.strftime("%-d %b %Y"),
            })
            return self.send_json(201, {"registered": True, "paymentUrl": str(course.get("payment_url") or "")})

        if path == "/api/login":
            identity = str(body.get("identity", "")).strip().lower()
            password = str(body.get("password", ""))
            account = (one("accounts", username=identity) or one("accounts", email=identity)
                       or one("accounts", phone=identity))
            if not account:
                raise ApiError(401, "Email/phone or password is incorrect.")
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(account["password_salt"]), 310_000).hex()
            if not hmac.compare_digest(digest, account["password_hash"]):
                raise ApiError(401, "Email/phone or password is incorrect.")
            token = new_session(account["id"])
            return self.send_json(200, {"token": token, "account": {"name": account["full_name"], "email": account.get("email"), "phone": account.get("phone")}})

        if path == "/api/signup/start":
            phone = normalize_phone(str(body.get("phone", "")))
            name = str(body.get("name", "")).strip()
            email = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            if not name or not email or "@" not in email:
                raise ApiError(400, "Enter your name and a valid email address.")
            if len(password) < 8:
                raise ApiError(400, "Choose a password with at least 8 characters.")
            if one("accounts", phone=phone) or one("accounts", email=email):
                raise ApiError(409, "An account already uses that phone or email.")
            last = one("otp_rate_limits", phone=phone)
            wait = int(last["requested_at"]) + 60 - int(time.time()) if last else 0
            if wait > 0:
                raise ApiError(429, f"Please wait {wait} seconds before requesting another code.")
            import hashlib as _hashlib
            salt = secrets.token_hex(16)
            password_hash = _hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310_000).hex()
            twilio_verify("Verifications", {"To": phone, "Channel": "sms"})
            now = int(time.time())
            rest("otp_rate_limits", "POST", {"on_conflict": "phone"}, {"phone": phone, "requested_at": now}, "resolution=merge-duplicates,return=minimal")
            rest("pending_signups", "POST", {"on_conflict": "phone"}, {
                "phone": phone, "full_name": name, "email": email,
                "password_salt": salt, "password_hash": password_hash, "expires_at": now + 600,
            }, "resolution=merge-duplicates,return=minimal")
            return self.send_json(200, {"sent": True, "phone": phone})

        if path == "/api/signup/verify":
            phone = normalize_phone(str(body.get("phone", "")))
            code = re.sub(r"\s", "", str(body.get("code", "")))
            if not re.fullmatch(r"\d{4,10}", code):
                raise ApiError(400, "Enter the verification code from your text message.")
            pending = one("pending_signups", phone=phone)
            if not pending or int(pending["expires_at"]) < int(time.time()):
                if pending:
                    rest("pending_signups", "DELETE", {"phone": f"eq.{phone}"})
                raise ApiError(400, "This signup code expired. Request a new code.")
            result = twilio_verify("VerificationCheck", {"To": phone, "Code": code})
            if result.get("status") != "approved":
                raise ApiError(400, "That code is incorrect or expired. Try again.")
            account = rest("accounts", "POST", payload={
                "full_name": pending["full_name"], "email": pending["email"], "phone": phone,
                "password_salt": pending["password_salt"], "password_hash": pending["password_hash"],
                "created_at": int(time.time()),
            }, prefer="return=representation")[0]
            token = new_session(account["id"])
            rest("pending_signups", "DELETE", {"phone": f"eq.{phone}"})
            return self.send_json(201, {"token": token, "account": {"name": account["full_name"], "email": account["email"], "phone": phone}})

        raise ApiError(404, "Not found.")

    def do_PUT(self):
        self.run(self._put)

    def _put(self):
        if urlparse(self.path).path != "/api/data":
            raise ApiError(404, "Not found.")
        self.signed_in()
        payload = self.read_json()
        courses = payload.get("courses", [])
        students = payload.get("students", [])
        if not isinstance(courses, list) or not isinstance(students, list):
            raise ApiError(400, "Courses and students must be lists.")
        for course in courses:
            payment_url = str(course.get("payment_url", "")).strip()
            if payment_url:
                parsed = urlparse(payment_url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise ApiError(400, "Payment links must use a secure https:// address.")

        clean_courses = [{
            "name": str(c.get("name", "")).strip(), "category": str(c.get("category", "Technology")),
            "fee": float(c.get("fee", 0) or 0), "timing": str(c.get("timing", "")),
            "hours": float(c.get("hours", 0) or 0), "classes": int(c.get("classes", 0) or 0),
            "seats": int(c.get("seats", 0) or 0), "payment_url": str(c.get("payment_url", "")).strip(),
        } for c in courses if str(c.get("name", "")).strip()]
        clean_students = [{
            "id": int(s.get("id") or index + 1), "name": str(s.get("name", "")).strip(),
            "phone": str(s.get("phone", "")), "email": str(s.get("email", "")),
            "course": str(s.get("course", "")), "payment": float(s.get("payment", 0) or 0),
            "date": str(s.get("date", "")),
        } for index, s in enumerate(students) if str(s.get("name", "")).strip()]

        if clean_courses:
            rest("courses", "POST", {"on_conflict": "name"}, clean_courses, "resolution=merge-duplicates,return=minimal")
        wanted_course_names = {c["name"] for c in clean_courses}
        for old in rest("courses", params={"select": "name"}):
            if old["name"] not in wanted_course_names:
                rest("courses", "DELETE", {"name": f"eq.{old['name']}"})

        if clean_students:
            rest("students", "POST", {"on_conflict": "id"}, clean_students, "resolution=merge-duplicates,return=minimal")
        wanted_student_ids = {s["id"] for s in clean_students}
        for old in rest("students", params={"select": "id"}):
            if old["id"] not in wanted_student_ids:
                rest("students", "DELETE", {"id": f"eq.{old['id']}"})
        self.send_json(200, {"saved": True})


class handler(ClassroomApiHandler):
    pass
