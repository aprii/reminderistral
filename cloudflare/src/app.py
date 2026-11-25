import hashlib
import hmac
import json
import math
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment

import templates_data
from db import get_database

DIFFICULTY_POINTS = {"easy": 10, "normal": 20, "hard": 40}
DIFFICULTY_LABELS = {"easy": "Easy (10 pts)", "normal": "Normal (20 pts)",
                     "hard": "Hard (40 pts)"}
POSTPONE_DECAY = 0.8
ON_TIME_BONUS = 0.25
LATE_PENALTY = 0.5
STREAK_BONUS_CAP = 10
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

ROUTE_PATHS = {
    "index": "/",
    "login": "/login",
    "register": "/register",
    "logout": "/logout",
    "add_task": "/tasks/add",
    "complete_task": "/tasks/{task_id}/complete",
    "postpone_task": "/tasks/{task_id}/postpone",
    "delete_task": "/tasks/{task_id}/delete",
}


def url_for(name, **kwargs):
    if name == "static":
        return "/static/" + kwargs["filename"]
    path = ROUTE_PATHS[name]
    for key, value in kwargs.items():
        path = path.replace("{" + key + "}", str(value))
    return path


def localdt(iso):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%b %d, %Y %H:%M")


jinja_env = Environment(loader=DictLoader(templates_data.TEMPLATES), autoescape=True)
jinja_env.globals["url_for"] = url_for
jinja_env.filters["localdt"] = localdt

app = FastAPI()


def render(request, name, status_code=200, flashes=None, **ctx):
    if flashes is None:
        flashes = []
        raw = request.query_params.get("flashes")
        if raw:
            try:
                flashes = [tuple(f) for f in json.loads(raw)]
            except (ValueError, TypeError):
                flashes = []
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    user = ctx["current_user"]
    ctx.setdefault("csrf", user["csrf"] if user else None)
    ctx["get_flashed_messages"] = lambda with_categories=False: flashes
    html = jinja_env.get_template(name).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


def redirect(url, flashes=None):
    if flashes:
        sep = "&" if "?" in url else "?"
        url += sep + "flashes=" + urllib.parse.quote(json.dumps(flashes))
    return RedirectResponse(url, status_code=302)


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


async def get_user(request):
    user = None
    token = request.cookies.get("quest_session")
    if token:
        db = await get_database(request)
        row = await db.prepare(
            "SELECT u.*, s.csrf, s.expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?"
        ).bind(token_hash(token)).first()
        if row and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc):
            user = row
    request.state.user = user
    return user


async def create_session(db, user_id):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.prepare(
        "INSERT INTO sessions(token_hash, user_id, csrf, expires_at) VALUES (?,?,?,?)"
    ).bind(token_hash(token), user_id, csrf, expires.isoformat()).run()
    return token, expires


async def award_badges(db, user_id):
    user = await db.prepare("SELECT * FROM users WHERE id = ?").bind(user_id).first()
    earned = {r["badge"] for r in await db.prepare(
        "SELECT badge FROM user_badges WHERE user_id = ?").bind(user_id).all()}
    checks = {
        "first_task": user["tasks_done"] >= 1,
        "on_time_10": user["on_time_done"] >= 10,
        "streak_7": user["streak"] >= 7,
        "streak_30": user["streak"] >= 30,
        "points_100": user["points"] >= 100,
        "points_1000": user["points"] >= 1000,
    }
    new = [badge for badge, cond in checks.items() if cond and badge not in earned]
    resilient = await db.prepare(
        "SELECT 1 FROM tasks WHERE user_id = ? AND status = 'done' AND postpones > 0 LIMIT 1"
    ).bind(user_id).first()
    if resilient and "resilient" not in earned:
        new.append("resilient")
    for badge in new:
        await db.prepare("INSERT INTO user_badges(user_id, badge, earned_at) VALUES (?,?,?)").bind(
            user_id, badge, datetime.now(timezone.utc).isoformat()).run()
    return new


@app.get("/static/{filename}")
async def static_file(filename: str):
    if filename == "style.css":
        return Response(templates_data.STATIC_CSS, media_type="text/css")
    return Response("not found", status_code=404)


@app.get("/")
async def index(request: Request):
    user = await get_user(request)
    if user is None:
        return redirect("/login")
    db = await get_database(request)
    open_tasks = await db.prepare(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'open' ORDER BY due_at"
    ).bind(user["id"]).all()
    done_tasks = await db.prepare(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' "
        "ORDER BY completed_at DESC LIMIT 10").bind(user["id"]).all()
    badges = await db.prepare(
        "SELECT badge, earned_at FROM user_badges WHERE user_id = ? ORDER BY earned_at"
    ).bind(user["id"]).all()
    now = datetime.now(timezone.utc)
    tasks_view = []
    for t in open_tasks:
        worth = compute_award(DIFFICULTY_POINTS[t["difficulty"]], t["postpones"],
                              True, user["streak"])
        overdue = datetime.fromisoformat(t["due_at"]) < now
        tasks_view.append({**t, "worth_now": worth, "overdue": overdue})
    lo, hi, lvl = level_bounds(user["points"])
    progress = int(100 * (user["points"] - lo) / (hi - lo))
    on_time_rate = round(100 * user["on_time_done"] / user["tasks_done"]) if user["tasks_done"] else 0
    return render(request, "index.html", tasks=tasks_view, done_tasks=done_tasks,
                  badges=badges, level=lvl, lo=lo, hi=hi, progress=progress,
                  on_time_rate=on_time_rate, difficulties=DIFFICULTY_LABELS,
                  badges_def=BADGES, on_time_bonus=ON_TIME_BONUS, late_penalty=LATE_PENALTY,
                  decay_pct=int((1 - POSTPONE_DECAY) * 100), streak_cap=STREAK_BONUS_CAP)


@app.post("/tasks/add")
async def add_task(request: Request, csrf: str = Form(""), title: str = Form(""),
                   description: str = Form(""), difficulty: str = Form("normal"),
                   due: str = Form("")):
    user = await get_user(request)
    if user is None:
        return redirect("/login")
    if csrf != user["csrf"]:
        return Response("Invalid CSRF token", status_code=400)
    title = title.strip()
    if not title or len(title) > 120:
        return redirect("/", [("error", "Title is required (max 120 characters).")])
    if difficulty not in DIFFICULTY_POINTS:
        difficulty = "normal"
    try:
        due_dt = datetime.fromisoformat(due.strip())
    except ValueError:
        return redirect("/", [("error", "Invalid due date.")])
    if due_dt.tzinfo is None:
        due_dt = due_dt.astimezone()
    db = await get_database(request)
    await db.prepare(
        "INSERT INTO tasks(user_id, title, description, difficulty, due_at, created_at) "
        "VALUES (?,?,?,?,?,?)"
    ).bind(user["id"], title, description.strip(), difficulty,
           due_dt.astimezone(timezone.utc).isoformat(),
           datetime.now(timezone.utc).isoformat()).run()
    return redirect("/", [("success",
                           f"Quest added: “{title}” — worth up to {DIFFICULTY_POINTS[difficulty]} base pts.")])


@app.post("/tasks/{task_id}/complete")
async def complete_task(request: Request, task_id: int, csrf: str = Form("")):
    user = await get_user(request)
    if user is None:
        return redirect("/login")
    if csrf != user["csrf"]:
        return Response("Invalid CSRF token", status_code=400)
    db = await get_database(request)
    task = await db.prepare("SELECT * FROM tasks WHERE id = ? AND user_id = ?").bind(
        task_id, user["id"]).first()
    if not task or task["status"] == "done":
        return Response("not found", status_code=404)
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
    await db.prepare("UPDATE tasks SET status = 'done', completed_at = ?, points_awarded = ? WHERE id = ?").bind(
        now.isoformat(), pts, task_id).run()
    await db.prepare(
        "UPDATE users SET points = points + ?, streak = ?, last_active_date = ?, "
        "tasks_done = tasks_done + 1, on_time_done = on_time_done + ? WHERE id = ?"
    ).bind(pts, streak, today, 1 if on_time else 0, user["id"]).run()
    new_badges = await award_badges(db, user["id"])
    flashes = [("success", f"Quest complete! +{pts} pts"
                + (" 🎉 on time" if on_time else " (after the deadline, halved)"))]
    if task["postpones"]:
        flashes[0] = (flashes[0][0], flashes[0][1] + f" · postponed ×{task['postpones']} "
                      f"(−{int((1 - POSTPONE_DECAY ** task['postpones']) * 100)}% of base)")
    if streak > 1:
        flashes[0] = (flashes[0][0], flashes[0][1] + f" · 🔥 {streak}-day streak")
    for badge in new_badges:
        flashes.append(("badge", f"Badge earned: {BADGES[badge][0]} — {BADGES[badge][1]}"))
    return redirect("/", flashes)


@app.post("/tasks/{task_id}/postpone")
async def postpone_task(request: Request, task_id: int, csrf: str = Form("")):
    user = await get_user(request)
    if user is None:
        return redirect("/login")
    if csrf != user["csrf"]:
        return Response("Invalid CSRF token", status_code=400)
    db = await get_database(request)
    task = await db.prepare(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ? AND status = 'open'"
    ).bind(task_id, user["id"]).first()
    if not task:
        return Response("not found", status_code=404)
    new_due = datetime.fromisoformat(task["due_at"]) + timedelta(days=1)
    await db.prepare("UPDATE tasks SET due_at = ?, postpones = postpones + 1 WHERE id = ?").bind(
        new_due.astimezone(timezone.utc).isoformat(), task_id).run()
    return redirect("/", [("info",
                           f"Postponed to {new_due.astimezone().strftime('%b %d %H:%M')}. "
                           f"Each postpone costs {int((1 - POSTPONE_DECAY) * 100)}% of the base points.")])


@app.post("/tasks/{task_id}/delete")
async def delete_task(request: Request, task_id: int, csrf: str = Form("")):
    user = await get_user(request)
    if user is None:
        return redirect("/login")
    if csrf != user["csrf"]:
        return Response("Invalid CSRF token", status_code=400)
    db = await get_database(request)
    await db.prepare("DELETE FROM tasks WHERE id = ? AND user_id = ? AND status = 'open'").bind(
        task_id, user["id"]).run()
    return redirect("/", [("info", "Open quest deleted.")])


@app.get("/register")
async def register_form(request: Request):
    if await get_user(request) is not None:
        return redirect("/")
    return render(request, "register.html")


@app.post("/register")
async def register(request: Request, username: str = Form(""), password: str = Form(""),
                   confirm: str = Form("")):
    if await get_user(request) is not None:
        return redirect("/")
    username = username.strip()
    db = await get_database(request)
    if not USERNAME_RE.match(username):
        return render(request, "register.html",
                      flashes=[("error", "Username must be 3-20 characters (letters, digits, underscore).")])
    if len(password) < 8:
        return render(request, "register.html",
                      flashes=[("error", "Password must be at least 8 characters.")])
    if password != confirm:
        return render(request, "register.html",
                      flashes=[("error", "Passwords do not match.")])
    if await db.prepare("SELECT 1 FROM users WHERE username = ?").bind(username).first():
        return render(request, "register.html",
                      flashes=[("error", "Username is already taken.")])
    await db.prepare("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)").bind(
        username, hash_password(password), datetime.now(timezone.utc).isoformat()).run()
    row = await db.prepare("SELECT last_insert_rowid() AS id").first()
    user_id = row["id"]
    token, expires = await create_session(db, user_id)
    resp = redirect("/", [("success", f"Welcome, {username}! Your quest log is ready.")])
    resp.set_cookie("quest_session", token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", expires=expires, path="/")
    return resp


@app.get("/login")
async def login_form(request: Request):
    if await get_user(request) is not None:
        return redirect("/")
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if await get_user(request) is not None:
        return redirect("/")
    db = await get_database(request)
    row = await db.prepare("SELECT * FROM users WHERE username = ?").bind(username.strip()).first()
    if not row or not verify_password(password, row["password_hash"]):
        return render(request, "login.html",
                      flashes=[("error", "Invalid username or password.")])
    await db.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(
        datetime.now(timezone.utc).isoformat()).run()
    token, expires = await create_session(db, row["id"])
    resp = redirect("/", [("success", f"Welcome back, {username.strip()}!")])
    resp.set_cookie("quest_session", token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", expires=expires, path="/")
    return resp


@app.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    token = request.cookies.get("quest_session")
    if token:
        db = await get_database(request)
        await db.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(token_hash(token)).run()
    resp = redirect("/login", [("info", "Logged out. See you on your next quest!")])
    resp.delete_cookie("quest_session", path="/")
    return resp
