import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("QUESTLOG_DB", os.path.join(BASE_DIR, "app.db"))

DIFFICULTY_POINTS = {"easy": 10, "normal": 20, "hard": 40}
DIFFICULTY_LABELS = {"easy": "Easy (10 pts)", "normal": "Normal (20 pts)",
                     "hard": "Hard (40 pts)"}
POSTPONE_DECAY = 0.8      # each postpone keeps 80% of the base points
ON_TIME_BONUS = 0.25      # +25% of base when finished before the deadline
LATE_PENALTY = 0.5        # 50% when finished after the deadline
STREAK_BONUS_CAP = 10     # +1 pt per streak day, capped
SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

BADGES = {
    "first_task": ("First Steps", "Complete your first task"),
    "on_time_10": ("Punctual Pro", "Complete 10 tasks on time"),
    "streak_7": ("Week Warrior", "Reach a 7-day streak"),
    "streak_30": ("Unstoppable", "Reach a 30-day streak"),
    "points_100": ("Century Club", "Earn 100 points"),
    "points_1000": ("Quest Legend", "Earn 1,000 points"),
    "resilient": ("Resilient", "Finish a task after postponing it"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0,
    last_active_date TEXT,
    tasks_done INTEGER NOT NULL DEFAULT 0,
    on_time_done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL DEFAULT 'normal',
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    postpones INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    points_awarded INTEGER
);
CREATE TABLE IF NOT EXISTS user_badges (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    PRIMARY KEY (user_id, badge)
);
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def hash_password(password, iterations=600_000):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        _algo, iterations, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db = get_db()
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf, expires_at) VALUES (?,?,?,?)",
        (token_hash(token), user_id, csrf, expires.isoformat()))
    db.commit()
    return token, expires


def set_session_cookie(resp, token, expires):
    resp.set_cookie("quest_session", token, httponly=True, samesite="Lax",
                    secure=request.is_secure, expires=expires, path="/")


def get_user():
    if "user" not in g:
        g.user = None
        token = request.cookies.get("quest_session")
        if token:
            row = get_db().execute(
                "SELECT u.*, s.csrf, s.expires_at FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (token_hash(token),)).fetchone()
            if row and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc):
                g.user = row
    return g.user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_user()
        if user is None:
            return redirect(url_for("login"))
        return f(user, *args, **kwargs)
    return wrapper


def check_csrf(user):
    if request.form.get("csrf") != user["csrf"]:
        abort(400, "Invalid CSRF token")


@app.context_processor
def inject_context():
    user = get_user()
    return {"current_user": user, "csrf": user["csrf"] if user else None}


def compute_award(base, postpones, on_time, streak):
    pts = base * (POSTPONE_DECAY ** postpones)
    if on_time:
        pts += base * ON_TIME_BONUS
    else:
        pts *= LATE_PENALTY
    pts += min(streak, STREAK_BONUS_CAP)
    return max(1, int(round(pts)))


def level_for(points):
    return int(math.sqrt(max(points, 0) / 50)) + 1


def level_bounds(points):
    lvl = level_for(points)
    return 50 * (lvl - 1) ** 2, 50 * lvl ** 2, lvl


def award_badges(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    earned = {r["badge"] for r in db.execute(
        "SELECT badge FROM user_badges WHERE user_id = ?", (user_id,)).fetchall()}
    checks = {
        "first_task": user["tasks_done"] >= 1,
        "on_time_10": user["on_time_done"] >= 10,
        "streak_7": user["streak"] >= 7,
        "streak_30": user["streak"] >= 30,
        "points_100": user["points"] >= 100,
        "points_1000": user["points"] >= 1000,
    }
    new = []
    for badge, cond in checks.items():
        if cond and badge not in earned:
            new.append(badge)
    db.execute("SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND status = 'done' AND postpones > 0",
               (user_id,))
    if db.execute("SELECT 1 FROM tasks WHERE user_id = ? AND status = 'done' AND postpones > 0 LIMIT 1",
                  (user_id,)).fetchone() and "resilient" not in earned:
        new.append("resilient")
    for badge in new:
        db.execute("INSERT INTO user_badges(user_id, badge, earned_at) VALUES (?,?,?)",
                   (user_id, badge, datetime.now(timezone.utc).isoformat()))
    db.commit()
    return new


@app.template_filter("localdt")
def localdt(iso):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%b %d, %Y %H:%M")


@app.route("/")
@login_required
def index(user):
    db = get_db()
    open_tasks = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'open' ORDER BY due_at",
        (user["id"],)).fetchall()
    done_tasks = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' "
        "ORDER BY completed_at DESC LIMIT 10", (user["id"],)).fetchall()
    badges = db.execute(
        "SELECT badge, earned_at FROM user_badges WHERE user_id = ? ORDER BY earned_at",
        (user["id"],)).fetchall()
    now = datetime.now(timezone.utc)
    tasks_view = []
    for t in open_tasks:
        worth = compute_award(DIFFICULTY_POINTS[t["difficulty"]], t["postpones"],
                              True, user["streak"])
        overdue = datetime.fromisoformat(t["due_at"]) < now
        tasks_view.append({**dict(t), "worth_now": worth, "overdue": overdue})
    lo, hi, lvl = level_bounds(user["points"])
    progress = int(100 * (user["points"] - lo) / (hi - lo))
    on_time_rate = round(100 * user["on_time_done"] / user["tasks_done"]) if user["tasks_done"] else 0
    return render_template(
        "index.html", tasks=tasks_view, done_tasks=done_tasks, badges=badges,
        level=lvl, lo=lo, hi=hi, progress=progress, on_time_rate=on_time_rate,
        difficulties=DIFFICULTY_LABELS, badges_def=BADGES,
        on_time_bonus=ON_TIME_BONUS, late_penalty=LATE_PENALTY,
        decay_pct=int((1 - POSTPONE_DECAY) * 100), streak_cap=STREAK_BONUS_CAP)


@app.route("/tasks/add", methods=["POST"])
@login_required
def add_task(user):
    check_csrf(user)
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    difficulty = request.form.get("difficulty", "normal")
    due_raw = request.form.get("due", "").strip()
    if not title or len(title) > 120:
        flash("Title is required (max 120 characters).", "error")
        return redirect(url_for("index"))
    if difficulty not in DIFFICULTY_POINTS:
        difficulty = "normal"
    try:
        due = datetime.fromisoformat(due_raw)
    except ValueError:
        flash("Invalid due date.", "error")
        return redirect(url_for("index"))
    if due.tzinfo is None:
        due = due.astimezone()
    db = get_db()
    db.execute(
        "INSERT INTO tasks(user_id, title, description, difficulty, due_at, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user["id"], title, description, difficulty,
         due.astimezone(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()))
    db.commit()
    flash(f"Quest added: “{title}” — worth up to {DIFFICULTY_POINTS[difficulty]} base pts.", "success")
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_task(user, task_id):
    check_csrf(user)
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                      (task_id, user["id"])).fetchone()
    if not task or task["status"] == "done":
        abort(404)
    now = datetime.now(timezone.utc)
    on_time = now <= datetime.fromisoformat(task["due_at"])
    today = now.date().isoformat()
    if user["last_active_date"] == today:
        streak = user["streak"]
    elif user["last_active_date"] == (now.date() - timedelta(days=1)).isoformat():
        streak = user["streak"] + 1
    else:
        streak = 1
    base = DIFFICULTY_POINTS[task["difficulty"]]
    pts = compute_award(base, task["postpones"], on_time, streak)
    db.execute("UPDATE tasks SET status = 'done', completed_at = ?, points_awarded = ? WHERE id = ?",
               (now.isoformat(), pts, task_id))
    db.execute(
        "UPDATE users SET points = points + ?, streak = ?, last_active_date = ?, "
        "tasks_done = tasks_done + 1, on_time_done = on_time_done + ? WHERE id = ?",
        (pts, streak, today, 1 if on_time else 0, user["id"]))
    new_badges = award_badges(user["id"])
    db.commit()
    msg = f"Quest complete! +{pts} pts"
    msg += " 🎉 on time" if on_time else " (after the deadline, halved)"
    if task["postpones"]:
        msg += f" · postponed ×{task['postpones']} (−{int((1 - POSTPONE_DECAY ** task['postpones']) * 100)}% of base)"
    if streak > 1:
        msg += f" · 🔥 {streak}-day streak"
    flash(msg, "success")
    for badge in new_badges:
        flash(f"Badge earned: {BADGES[badge][0]} — {BADGES[badge][1]}", "badge")
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/postpone", methods=["POST"])
@login_required
def postpone_task(user, task_id):
    check_csrf(user)
    db = get_db()
    task = db.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ? AND status = 'open'",
        (task_id, user["id"])).fetchone()
    if not task:
        abort(404)
    new_due = datetime.fromisoformat(task["due_at"]) + timedelta(days=1)
    db.execute("UPDATE tasks SET due_at = ?, postpones = postpones + 1 WHERE id = ?",
               (new_due.astimezone(timezone.utc).isoformat(), task_id))
    db.commit()
    flash(f"Postponed to {new_due.astimezone().strftime('%b %d %H:%M')}. "
          f"Each postpone costs {int((1 - POSTPONE_DECAY) * 100)}% of the base points.", "info")
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_task(user, task_id):
    check_csrf(user)
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ? AND status = 'open'",
               (task_id, user["id"]))
    db.commit()
    flash("Open quest deleted.", "info")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not USERNAME_RE.match(username):
            flash("Username must be 3-20 characters (letters, digits, underscore).", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif get_db().execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash("Username is already taken.", "error")
        else:
            db = get_db()
            cur = db.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                             (username, hash_password(password),
                              datetime.now(timezone.utc).isoformat()))
            db.commit()
            token, expires = create_session(cur.lastrowid)
            resp = redirect(url_for("index"))
            set_session_cookie(resp, token, expires)
            flash(f"Welcome, {username}! Your quest log is ready.", "success")
            return resp
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            flash("Invalid username or password.", "error")
        else:
            now = datetime.now(timezone.utc)
            db = get_db()
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            token, expires = create_session(row["id"])
            resp = redirect(url_for("index"))
            set_session_cookie(resp, token, expires)
            flash(f"Welcome back, {username}!", "success")
            return resp
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get("quest_session")
    if token:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        db.commit()
    resp = redirect(url_for("login"))
    resp.delete_cookie("quest_session", path="/")
    flash("Logged out. See you on your next quest!", "info")
    return resp


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
