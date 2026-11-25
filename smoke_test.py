import http.cookiejar
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = "http://127.0.0.1:5057"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(jar),
    type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
         {"redirect_request": lambda *a, **k: None})(),
)


def req(path, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    r = opener.open(urllib.request.Request(BASE + path, data=body))
    return r.status, r.headers, r.read().decode()


def try_req(path, data=None):
    try:
        return req(path, data)
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), name, detail)


# 1. unauthenticated redirect
code, headers, _ = try_req("/")
check("unauth / redirects to login", code == 302 and "/login" in (headers.get("Location") or ""), f"code={code} loc={headers.get('Location')}")

# 2. register
code, headers, body = try_req("/register", {"username": "alice", "password": "password123", "confirm": "password123"})
check("register redirects to /", code == 302 and headers.get("Location", "").endswith("/"), f"code={code}")
check("session cookie set", any(c.name == "quest_session" for c in jar), str([c.name for c in jar]))

# 3. dashboard loads + csrf
code, _, body = req("/")
check("dashboard 200", code == 200)
m = re.search(r'name="csrf" value="([^"]+)"', body)
csrf = m.group(1) if m else ""
check("csrf token present", bool(csrf))

# 4. add task due tomorrow (normal)
due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
code, _, _ = try_req("/tasks/add", {"csrf": csrf, "title": "Write report", "due": due, "difficulty": "normal", "description": "Q3 report"})
check("add task 1", code == 302, f"code={code}")

# 5. complete on time -> 20 * 1.25 + streak 1 = 26
code, _, _ = try_req("/tasks/1/complete", {"csrf": csrf})
check("complete task 1", code == 302, f"code={code}")
_, _, body = req("/")
check("flash shows +26 pts", "+26 pts" in body)

# 6. hard task, postpone x2, complete -> 40*0.64 + 40*0.25 + 1 = 37
code, _, _ = try_req("/tasks/add", {"csrf": csrf, "title": "Refactor module", "due": due, "difficulty": "hard"})
check("add task 2", code == 302)
try_req("/tasks/2/postpone", {"csrf": csrf})
try_req("/tasks/2/postpone", {"csrf": csrf})
code, _, _ = try_req("/tasks/2/complete", {"csrf": csrf})
check("complete postponed task 2", code == 302, f"code={code}")
_, _, body = req("/")
check("flash shows +37 pts", "+37 pts" in body)
check("postpone counter shown", "postponed ×2" in body)

# 7. overdue task completed late -> 20*0.5 + 1 = 11
past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
try_req("/tasks/add", {"csrf": csrf, "title": "Old task", "due": past, "difficulty": "normal"})
code, _, _ = try_req("/tasks/3/complete", {"csrf": csrf})
check("complete late task 3", code == 302, f"code={code}")
_, _, body = req("/")
check("flash shows +11 pts (late)", "+11 pts" in body and "after the deadline" in body)
check("badge First Steps shown", "First Steps" in body)

# 8. DB verification
db = sqlite3.connect("app.db")
db.row_factory = sqlite3.Row
u = db.execute("SELECT * FROM users WHERE username='alice'").fetchone()
tasks = db.execute("SELECT id, status, postpones, points_awarded FROM tasks ORDER BY id").fetchall()
badges = [r["badge"] for r in db.execute("SELECT badge FROM user_badges").fetchall()]
check("user points = 74", u["points"] == 74, f"points={u['points']}")
check("tasks_done = 3, on_time = 2, streak = 1", (u["tasks_done"], u["on_time_done"], u["streak"]) == (3, 2, 1), f"{u['tasks_done']},{u['on_time_done']},{u['streak']}")
check("task points awarded 26/37/11", [t["points_awarded"] for t in tasks] == [26, 37, 11], str([dict(t) for t in tasks]))
check("task2 postpones = 2", tasks[1]["postpones"] == 2)
check("first_task + resilient badges", set(badges) >= {"first_task", "resilient"}, str(badges))
check("password stored as pbkdf2 hash", u["password_hash"].startswith("pbkdf2_sha256$"), u["password_hash"][:30] + "...")
sess = db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
check("session row exists", sess >= 1, f"sessions={sess}")
db.close()

# 9. CSRF enforcement
code, _, _ = try_req("/tasks/add", {"title": "x", "due": due, "difficulty": "easy"})
check("POST without csrf -> 400", code == 400, f"code={code}")

# 10. logout then login with wrong password, then correct
try_req("/logout", {"csrf": csrf})
code, _, _ = try_req("/")
check("after logout / redirects", code == 302)
code, _, body = try_req("/login", {"username": "alice", "password": "wrongpass"})
check("wrong password rejected", code == 200 and "Invalid username or password" in body)
code, headers, _ = try_req("/login", {"username": "alice", "password": "password123"})
check("correct password logs in", code == 302 and headers.get("Location", "").endswith("/"), f"code={code}")

# 11. duplicate registration
code, _, body = try_req("/register", {"username": "alice", "password": "password123", "confirm": "password123"})
check("duplicate username rejected", "already taken" in body)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
raise SystemExit(1 if failed else 0)
