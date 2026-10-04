// Keep course and student records in SQLite through the local app server.
const CLASSROOM_API = location.protocol === "file:"
  ? "http://127.0.0.1:8000/api/data"
  : "/api/data";

function readLegacy(key) {
  try { return JSON.parse(localStorage.getItem(key) || "[]"); }
  catch { return []; }
}

async function saveDatabaseSnapshot() {
  const response = await fetch(CLASSROOM_API, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ courses, students }),
  });
  if (!response.ok) throw new Error("The database could not save your changes.");
}

window.load = async function loadFromDatabase() {
  const response = await fetch(CLASSROOM_API);
  if (!response.ok) throw new Error("The database could not be reached.");
  const data = await response.json();
  courses = data.courses || [];
  students = data.students || [];

  // Import the existing browser-only records once, when the database is empty.
  if (localStorage.getItem("classroomDbMigrated") !== "yes") {
    const oldCourses = readLegacy("classroomCourses");
    const oldStudents = readLegacy("classroomCustomers");
    let changed = false;
    if (oldCourses.length) {
      const byName = new Map(courses.map(course => [course.name, course]));
      oldCourses.forEach(course => byName.set(course.name, course));
      courses = [...byName.values()];
      changed = true;
    }
    if (!students.length && oldStudents.length) {
      students = oldStudents;
      changed = true;
    }
    if (changed) await saveDatabaseSnapshot();
    localStorage.setItem("classroomDbMigrated", "yes");
  }
};

window.persist = saveDatabaseSnapshot;

function updateRequiredFields(activeSection) {
  document.querySelectorAll("#courseFields [required]").forEach(field => {
    field.required = activeSection === "courses";
  });
  document.querySelectorAll("#studentFields [required]").forEach(field => {
    field.required = activeSection === "students";
  });
}

document.querySelector("#addCourse").addEventListener("click", () => updateRequiredFields("courses"), true);
document.querySelector("#addStudent").addEventListener("click", () => updateRequiredFields("students"), true);

async function openDashboard(userKey) {
  sessionStorage.setItem("classUser", userKey);
  document.querySelector("#login").style.display = "none";
  document.querySelector("#app").style.display = "block";
  try {
    await load();
    render();
    document.querySelector("#error").textContent = "";
    restorePendingFormAfterSignin();
  } catch (error) {
    document.querySelector("#error").textContent = `${error.message} Start server.py and reload.`;
  }
}

document.querySelector("#loginForm").onsubmit = async event => {
  event.preventDefault();
  const message = document.querySelector("#error");
  const password = document.querySelector("#password").value;
  if (authMode === "signup") {
    const name = document.querySelector("#newName").value.trim();
    const phone = document.querySelector("#newPhone").value.trim();
    const email = document.querySelector("#newEmail").value.trim().toLowerCase();
    const accounts = savedAccounts();
    if (password.length < 6) {
      message.textContent = "Use a password with at least 6 characters.";
      return;
    }
    if (accounts.some(account => account.email === email || account.phone === phone)) {
      message.textContent = "An account already uses that email or phone number.";
      return;
    }
    accounts.push({ name, phone, email, password });
    localStorage.setItem("classroomAccounts", JSON.stringify(accounts));
    document.querySelector("#username").value = email;
    document.querySelector("#password").value = password;
    setAuthMode("signin");
    message.textContent = "Account created. Sign in with your email or phone.";
    return;
  }

  const identity = document.querySelector("#username").value.trim().toLowerCase();
  const demoAccounts = {
    admin: ["learn123", "Alex Morgan"],
    teacher: ["class123", "Jordan Lee"],
    staff: ["welcome1", "Taylor Kim"],
  };
  const account = savedAccounts().find(item => item.email === identity || item.phone === identity);
  const demo = demoAccounts[identity];
  if ((demo && demo[0] === password) || (account && account.password === password)) {
    await openDashboard(demo ? identity : account.email);
  } else {
    message.textContent = "Email/phone or password is incorrect.";
  }
};

document.querySelector("#dataForm").onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = activeFormValues();
  if (mode === "courses") {
    const updatedCourse = {
      ...(editingCourseIndex === null ? {} : courses[editingCourseIndex]),
      ...values,
      fee: Number(values.fee),
      hours: Number(values.hours),
      classes: Number(values.classes),
      seats: Number(values.seats),
      payment_url: String(values.payment_url || "").trim(),
    };
    if (editingCourseIndex === null) courses.push(updatedCourse);
    else courses[editingCourseIndex] = updatedCourse;
  } else {
    students.push({
      ...values,
      payment: Number(values.payment || 0),
      id: Date.now(),
      date: new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric" }).format(new Date()),
    });
  }
  try {
    await persist();
    sessionStorage.removeItem("classroomPendingForm");
    sessionStorage.removeItem("classroomPendingIdentity");
    form.reset();
    document.querySelector("#modal").classList.add("hidden");
    render();
  } catch (error) {
    if (error.code === "SESSION_EXPIRED") returnToSigninAfterExpiredSession();
    else alert(error.message);
  }
};

document.body.onclick = null;
document.body.addEventListener("click", async event => {
  const editButton = event.target.closest("[data-edit-course]");
  if (editButton) {
    editingCourseIndex = Number(editButton.dataset.editCourse);
    const course = courses[editingCourseIndex];
    open("courses");
    for (const [key, value] of Object.entries(course)) {
      const sectionSelector = pending.mode === "courses" ? "#courseFields" : "#studentFields";
    const field = document.querySelector(`${sectionSelector} [name="${key}"]`);
      if (field) field.value = value ?? "";
    }
    document.querySelector("#modalTitle").textContent = "Edit course";
    document.querySelector("#save").textContent = "Save changes";
    return;
  }
  const shareButton = event.target.closest("[data-share-course]");
  if (shareButton) {
    const base = location.protocol === "file:" ? "http://127.0.0.1:8000/" : location.origin + "/";
    const link = new URL("enroll.html", base);
    link.searchParams.set("course", shareButton.dataset.shareCourse);
    try {
      await navigator.clipboard.writeText(link.href);
      shareButton.textContent = "Copied!";
      setTimeout(() => { shareButton.textContent = "Copy link"; }, 1800);
    } catch {
      window.prompt("Copy this student enrollment link:", link.href);
    }
    return;
  }
  const courseButton = event.target.closest("[data-del-course]");
  const studentButton = event.target.closest("[data-del-student]");
  if (!courseButton && !studentButton) return;
  if (courseButton) courses.splice(Number(courseButton.dataset.delCourse), 1);
  if (studentButton) students = students.filter(student => student.id !== Number(studentButton.dataset.delStudent));
  try {
    await persist();
    render();
  } catch (error) {
    alert(error.message);
  }
});

// The original demo can show cached data at startup. Replace it with database data.
if (sessionStorage.getItem("classUser")) {
  openDashboard(sessionStorage.getItem("classUser"));
}

// Account creation and sign-in use the server. New accounts require an SMS OTP.
const CLASSROOM_API_ROOT = location.protocol === "file:" ? "http://127.0.0.1:8000/api" : "/api";
let signupStage = "details";
let savedSnapshot = { courses: [], students: [] };

async function classroomApi(path, payload, authenticated = false) {
  const headers = { "Content-Type": "application/json" };
  const token = sessionStorage.getItem("classroomToken");
  if (authenticated && token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${CLASSROOM_API_ROOT}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "The server could not complete that request.");
  return result;
}

async function saveSqliteSnapshot() {
  const deletedCourseNames = savedSnapshot.courses
    .filter(old => !courses.some(current => current.name === old.name))
    .map(course => course.name);
  const deletedStudentIds = savedSnapshot.students
    .filter(old => !students.some(current => Number(current.id) === Number(old.id)))
    .map(student => Number(student.id));
  const response = await fetch(CLASSROOM_API_ROOT + "/data/changes", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${sessionStorage.getItem("classroomToken") || ""}`,
    },
    body: JSON.stringify({ courses, students, deletedCourseNames, deletedStudentIds }),
  });
  const result = await response.json().catch(() => ({}));
  if (response.status === 401) {
    const error = new Error(result.error || "Your sign-in has expired.");
    error.code = "SESSION_EXPIRED";
    throw error;
  }
  if (!response.ok) throw new Error(result.error || "The database could not save your changes.");
  savedSnapshot = JSON.parse(JSON.stringify({ courses, students }));
}

window.load = async function loadAuthenticatedDatabase() {
  const response = await fetch(CLASSROOM_API_ROOT + "/data", {
    headers: { Authorization: `Bearer ${sessionStorage.getItem("classroomToken") || ""}` },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The database could not be reached.");
  courses = data.courses || [];
  students = data.students || [];
  if (localStorage.getItem("classroomDbMigrated") !== "yes") {
    const oldCourses = readLegacy("classroomCourses");
    const oldStudents = readLegacy("classroomCustomers");
    let changed = false;
    if (oldCourses.length) {
      const byName = new Map(courses.map(course => [course.name, course]));
      oldCourses.forEach(course => byName.set(course.name, course));
      courses = [...byName.values()];
      changed = true;
    }
    if (!students.length && oldStudents.length) {
      students = oldStudents;
      changed = true;
    }
    if (changed) await saveSqliteSnapshot();
    localStorage.setItem("classroomDbMigrated", "yes");
  }
  savedSnapshot = JSON.parse(JSON.stringify({ courses, students }));
};
window.persist = saveSqliteSnapshot;

function activeFormValues() {
  const section = document.querySelector(mode === "courses" ? "#courseFields" : "#studentFields");
  return Object.fromEntries(
    Array.from(section.querySelectorAll("[name]"), field => [field.name, field.value]),
  );
}

let pendingFormAfterSignin = null;

function returnToSigninAfterExpiredSession() {
  pendingFormAfterSignin = {
    mode,
    editingCourseIndex,
    retrySave: true,
    values: activeFormValues(),
  };
  sessionStorage.setItem("classroomPendingForm", JSON.stringify(pendingFormAfterSignin));
  document.querySelector("#modal").classList.add("hidden");
  document.querySelector("#app").style.display = "none";
  document.querySelector("#login").style.display = "grid";
  setAuthMode("signin");
  const previousIdentity = sessionStorage.getItem("classUser") || "";
  if (previousIdentity) {
    sessionStorage.setItem("classroomPendingIdentity", previousIdentity);
    document.querySelector("#username").value = previousIdentity;
  }
  document.querySelector("#error").textContent = "Your sign-in expired. Enter your password and sign in to retry saving the form.";
  sessionStorage.removeItem("classroomToken");
  sessionStorage.removeItem("classUser");
}

function restorePendingFormAfterSignin() {
  if (!pendingFormAfterSignin) {
    try { pendingFormAfterSignin = JSON.parse(sessionStorage.getItem("classroomPendingForm") || "null"); }
    catch { pendingFormAfterSignin = null; }
  }
  if (!pendingFormAfterSignin) return;
  const pending = pendingFormAfterSignin;
  pendingFormAfterSignin = null;
  updateRequiredFields(pending.mode);
  open(pending.mode);
  editingCourseIndex = pending.editingCourseIndex;
  for (const [key, value] of Object.entries(pending.values)) {
    const field = document.querySelector(`#dataForm [name="${key}"]`);
    if (field) field.value = value;
  }
  if (pending.mode === "courses" && pending.editingCourseIndex !== null) {
    const currentCourse = courses[pending.editingCourseIndex];
    const courseName = document.querySelector('#courseFields [name="name"]');
    if (currentCourse && !courseName.value) courseName.value = currentCourse.name;
    document.querySelector("#modalTitle").textContent = "Edit course";
    document.querySelector("#save").textContent = "Save changes";
  }
  if (pending.retrySave !== false) {
    document.querySelector("#dataForm").requestSubmit();
  }
}

window.setAuthMode = function setPhoneOtpMode(next) {
  authMode = next;
  signupStage = "details";
  const signup = next === "signup";
  document.querySelector("#authEyebrow").textContent = signup ? "GET STARTED" : "WELCOME BACK";
  document.querySelector("#authTitle").textContent = signup ? "Create your account" : "Sign in";
  document.querySelector("#authHelp").textContent = signup
    ? "Verify your mobile number to create your account."
    : "Sign in with your email address or phone number.";
  document.querySelector("#signupFields").classList.toggle("hidden", !signup);
  document.querySelector("#otpFields").classList.add("hidden");
  document.querySelector("#identityField").classList.toggle("hidden", signup);
  document.querySelector("#identityField input").required = !signup;
  document.querySelector("#passwordField").classList.remove("hidden");
  document.querySelector("#password").required = true;
  document.querySelector("#otpCode").required = false;
  ["#newName", "#newPhone", "#newEmail"].forEach(selector => {
    document.querySelector(selector).required = signup;
  });
  document.querySelector("#submitAuth").textContent = signup ? "Send verification code →" : "Sign in →";
  document.querySelector("#authToggle").textContent = signup
    ? "Already have an account? Sign in"
    : "New here? Create an account";
  document.querySelector("#error").textContent = "";
};

const showLoginButton = document.querySelector("#showLogin");
if (showLoginButton) showLoginButton.addEventListener("click", event => {
  const form = document.querySelector("#loginForm");
  const loginPage = document.querySelector("#login");
  const button = event.currentTarget;
  const opening = form.classList.contains("hidden");
  if (opening) {
    setAuthMode("signin");
    form.classList.remove("hidden");
    loginPage.classList.add("form-open");
    button.textContent = "Hide login";
    button.setAttribute("aria-expanded", "true");
    document.querySelector("#username").focus();
  } else {
    form.classList.add("hidden");
    loginPage.classList.remove("form-open");
    button.textContent = "Login";
    button.setAttribute("aria-expanded", "false");
    button.focus();
  }
});

function showOtpEntry(phone) {
  signupStage = "otp";
  document.querySelector("#signupFields").classList.add("hidden");
  document.querySelector("#passwordField").classList.add("hidden");
  document.querySelector("#otpFields").classList.remove("hidden");
  document.querySelector("#otpCode").required = true;
  document.querySelector("#otpMessage").textContent = `Enter the verification code sent to ${phone}.`;
  document.querySelector("#submitAuth").textContent = "Verify code and create account →";
  document.querySelector("#otpCode").focus();
}

async function enterWithSession(result) {
  sessionStorage.setItem("classroomToken", result.token);
  sessionStorage.setItem("classUser", result.account.email || result.account.phone);
  try {
    await load();
    document.querySelector("#login").style.display = "none";
    document.querySelector("#app").style.display = "block";
    render();
    document.querySelector("#error").textContent = "";
    restorePendingFormAfterSignin();
  } catch (error) {
    sessionStorage.removeItem("classroomToken");
    sessionStorage.removeItem("classUser");
    document.querySelector("#error").textContent = error.message;
  }
}

document.querySelector("#loginForm").onsubmit = async event => {
  event.preventDefault();
  const errorBox = document.querySelector("#error");
  const button = document.querySelector("#submitAuth");
  button.disabled = true;
  try {
    if (authMode === "signup" && signupStage === "details") {
      const result = await classroomApi("/signup/start", {
        name: document.querySelector("#newName").value.trim(),
        phone: document.querySelector("#newPhone").value.trim(),
        email: document.querySelector("#newEmail").value.trim().toLowerCase(),
        password: document.querySelector("#password").value,
      });
      showOtpEntry(result.phone);
    } else if (authMode === "signup") {
      const result = await classroomApi("/signup/verify", {
        phone: document.querySelector("#newPhone").value.trim(),
        code: document.querySelector("#otpCode").value.trim(),
      });
      await enterWithSession(result);
    } else {
      const result = await classroomApi("/login", {
        identity: document.querySelector("#username").value.trim(),
        password: document.querySelector("#password").value,
      });
      await enterWithSession(result);
    }
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

document.querySelector("#changePhone").addEventListener("click", () => {
  signupStage = "details";
  document.querySelector("#otpFields").classList.add("hidden");
  document.querySelector("#signupFields").classList.remove("hidden");
  document.querySelector("#passwordField").classList.remove("hidden");
  document.querySelector("#otpCode").required = false;
  document.querySelector("#submitAuth").textContent = "Send verification code →";
  document.querySelector("#error").textContent = "";
});

document.querySelector("#logout").onclick = () => {
  sessionStorage.removeItem("classUser");
  sessionStorage.removeItem("classroomToken");
  location.reload();
};

if (sessionStorage.getItem("classroomToken")) {
  enterWithSession({
    token: sessionStorage.getItem("classroomToken"),
    account: { email: sessionStorage.getItem("classUser") },
  });
} else {
  sessionStorage.removeItem("classUser");
  document.querySelector("#app").style.display = "none";
  document.querySelector("#login").style.display = "grid";
  setAuthMode("signin");
  const pendingIdentity = sessionStorage.getItem("classroomPendingIdentity");
  if (pendingIdentity) {
    document.querySelector("#username").value = pendingIdentity;
    document.querySelector("#error").textContent = "Your sign-in expired. Enter your password and sign in to retry saving the form.";
  }
}

// Broadcast SMS and WhatsApp notifications to students who have valid phone numbers.
function studentNotificationPhoneCount() {
  return students.filter(student => {
    const phone = String(student.phone || "").replace(/[\s().-]/g, "");
    return /^\+[1-9]\d{7,14}$/.test(phone) || /^[6-9]\d{9}$/.test(phone);
  }).length;
}

function updateStudentNotificationCount() {
  const box = document.querySelector("#notificationRecipients");
  if (!box) return;
  const valid = studentNotificationPhoneCount();
  box.textContent = `${valid} student${valid === 1 ? "" : "s"} can receive this message. Ten-digit Indian mobile numbers will use +91 automatically; other numbers need an international country code.`;
}

async function loadStudentNotificationHistory() {
  const response = await fetch(`${CLASSROOM_API_ROOT}/notifications`, {
    headers: { Authorization: `Bearer ${sessionStorage.getItem("classroomToken") || ""}` },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "Could not load notification history.");
  const rows = result.notifications || [];
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const labels = { general: "General", announcement: "Announcement", schedule: "Schedule", payment: "Payment" };
  document.querySelector("#notificationHistory").innerHTML = rows.map(row => `<tr><td>${new Date(row.created_at * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</td><td>${labels[row.kind] || escape(row.kind)}</td><td>${escape(row.channel)}</td><td title="${escape(row.message)}">${escape(row.message)}</td><td>${row.target_count}</td><td>${row.queued_count}</td><td>${row.failed_count}</td></tr>`).join("");
  document.querySelector("#notificationHistoryEmpty").classList.toggle("hidden", rows.length > 0);
}

const notificationComposer = document.querySelector("#notificationForm");
if (notificationComposer) {
  notificationComposer.addEventListener("submit", async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(notificationComposer));
    const messageBox = document.querySelector("#notificationStatus");
    const sendButton = document.querySelector("#sendNotification");
    const count = studentNotificationPhoneCount();
    if (!count) {
      messageBox.textContent = "No students have a valid phone number with a country code yet.";
      return;
    }
    const channelName = values.channel === "whatsapp" ? "WhatsApp" : "SMS";
    if (!window.confirm(`Send this ${channelName} message to ${count} student${count === 1 ? "" : "s"}? Your messaging provider may charge for messages.`)) return;
    sendButton.disabled = true;
    messageBox.textContent = "Submitting messages…";
    try {
      const result = await classroomApi("/notifications/send", {
        ...values,
        permission_confirmed: values.permission_confirmed === "on",
      }, true);
      messageBox.className = "success";
      messageBox.textContent = `Submitted to Twilio: ${result.queuedCount} queued, ${result.failedCount} failed. Queued does not confirm delivery.`;
      notificationComposer.elements.message.value = "";
      await loadStudentNotificationHistory();
    } catch (error) {
      messageBox.className = "err";
      messageBox.textContent = error.message;
    } finally {
      sendButton.disabled = false;
    }
  });
  document.querySelectorAll('.nav[data-page="students"]').forEach(button => button.addEventListener("click", async () => {
    updateStudentNotificationCount();
    try { await loadStudentNotificationHistory(); }
    catch (error) { document.querySelector("#notificationStatus").textContent = error.message; }
  }));
  const originalRender = window.render;
  if (originalRender) {
    window.render = function renderWithNotificationCount(...args) {
      const output = originalRender.apply(this, args);
      updateStudentNotificationCount();
      return output;
    };
  }
}
