"""Runs against an in-memory fake Supabase (no network, no real data). Run: pytest -q"""
import os, sys, importlib
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k", JWT_SECRET="test-secret-123", ADMIN_USER_IDS="admin1")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest, supabase as _sb

class Q:
    def __init__(s, db, name): s.db, s.name, s.f, s.op, s.p, s.one, s.lim = db, name, [], "select", None, False, None
    def select(s, *a, **k): s.op = "select"; return s
    def insert(s, p): s.op, s.p = "insert", p; return s
    def upsert(s, p, **k): s.op, s.p, s.oc = "upsert", p, k.get("on_conflict"); return s
    def update(s, p): s.op, s.p = "update", p; return s
    def delete(s): s.op = "delete"; return s
    def eq(s, c, v): s.f.append(lambda r: r.get(c) == v); return s
    def neq(s, c, v): s.f.append(lambda r: r.get(c) != v); return s
    def in_(s, c, v): s.f.append(lambda r: r.get(c) in v); return s
    def gte(s, c, v): s.f.append(lambda r: r.get(c) is not None and r[c] >= v); return s
    def lt(s, c, v): s.f.append(lambda r: r.get(c) is not None and r[c] < v); return s
    def or_(s, expr):   # real PostgREST `or` semantics for the shapes app.py uses: a.eq.x,b.eq.y / and(a.eq.x,b.eq.y),and(...)
        def split(t):
            out, depth, cur = [], 0, ""
            for ch in t:
                depth += (ch == "(") - (ch == ")")
                if ch == "," and depth == 0: out.append(cur); cur = ""
                else: cur += ch
            return out + [cur]
        def cond(term):
            if term.startswith("and(") and term.endswith(")"):
                parts = [cond(x) for x in split(term[4:-1])]; return lambda r: all(p(r) for p in parts)
            col, op, val = term.split(".", 2); assert op == "eq", term
            return lambda r: str(r.get(col)) == val
        conds = [cond(t) for t in split(expr)]
        s.f.append(lambda r: any(c(r) for c in conds)); return s
    def order(s, *a, **k): return s
    def limit(s, n): s.lim = n; return s
    def single(s): s.one = True; return s
    def execute(s):
        rows = s.db.setdefault(s.name, [])
        hit = [r for r in rows if all(f(r) for f in s.f)]
        class R: pass
        r = R()
        if s.op == "insert":
            row = dict(s.p); row.setdefault("id", f"{s.name}-{len(rows)+1}"); rows.append(row); r.data = [row]
        elif s.op == "upsert":
            keys = [x.strip() for x in (s.oc or "").split(",") if x.strip()]
            ex = next((x for x in rows if keys and all(x.get(k) == s.p.get(k) for k in keys)), None)
            if ex: ex.update(s.p); r.data = [ex]
            else: rows.append(dict(s.p, id=s.p.get("id", f"{s.name}-{len(rows)+1}"))); r.data = [rows[-1]]
        elif s.op == "update":
            [x.update(s.p) for x in hit]; r.data = hit
        elif s.op == "delete":
            for x in hit: rows.remove(x)
            r.data = hit
        else:
            r.data = hit[: s.lim] if s.lim else hit
            if s.one: r.data = r.data[0] if r.data else None
        r.count = len(r.data) if isinstance(r.data, list) else (1 if r.data else 0)   # supabase count="exact"
        return r

def scr_row(uid, **kw):
    r = dict(user_id=uid, gender="man", interested_in="woman", relationship_goal="long_term", monogamy="monogamous", marriage_timeline="1_2y",
             children_plans="want", comm_style="thoughtful", emotional_readiness="ready", age_pref_min=25, age_pref_max=40, distance_pref=50,
             city="Chennai", lifestyle=["non_smoker", "active"], values=["family", "honesty", "growth"], interests=["music", "travel"],
             dealbreakers=[], consent={"guidelines": True, "privacy": True, "safety": True}, age_confirmed=True, status="complete")
    r.update(kw); return r

import threading

class _Storage:
    def __init__(s): s.files = {}
    def from_(s, b): return s
    def upload(s, path, data, opts=None): s.files[path] = data
    def download(s, path): return s.files[path]
    def create_signed_url(s, path, secs): return {"signedURL": "https://signed.example/" + path}

class _RPC:
    def __init__(s, db, name, args): s.db, s.name, s.args = db, name, args
    def execute(s):     # mirrors create_exclusive_pairing / end_exclusive_pairing in migration_challenge.sql (atomic via a lock)
        db, a, b = s.db, s.args["a"], s.args["b"]
        with db.lock:
            ap, pm = db.t.setdefault("active_pairings", []), db.t.setdefault("pairing_members", [])
            u1, u2 = min(a, b), max(a, b)
            if s.name == "create_exclusive_pairing":
                if any(p["status"] == "active" and p["user1_id"] == u1 and p["user2_id"] == u2 for p in ap): return None
                if any(m["active"] and m["user_id"] in (a, b) for m in pm): raise Exception("already_paired")
                pid = f"pair-{len(ap)+1}"; ap.append({"id": pid, "user1_id": u1, "user2_id": u2, "status": "active"})
                pm.extend([{"pairing_id": pid, "user_id": u1, "active": True}, {"pairing_id": pid, "user_id": u2, "active": True}])
            elif s.name == "end_exclusive_pairing":
                for p in ap:
                    if p["status"] == "active" and p["user1_id"] == u1 and p["user2_id"] == u2:
                        p["status"] = "ended"
                        for m in pm:
                            if m["pairing_id"] == p["id"]: m["active"] = False
            else: raise Exception("unknown rpc")

class FakeDB:
    def __init__(s): s.t = {}; s.lock = threading.Lock(); s.storage = _Storage()
    def table(s, n): return Q(s.t, n)
    def rpc(s, name, args): return _RPC(s, name, args)

@pytest.fixture()
def env():
    db = FakeDB(); _sb.create_client = lambda *a, **k: db
    import app as m; importlib.reload(m); m.supabase = db; getattr(m, "_RL", {}).clear()
    db.t["users"] = [{"id": i, "first_name": i, "last_name": "X", "gender": g, "interested_in": "woman" if g == "man" else "man", "age": 30,
                      "dob": "1996-01-01", "screening_complete": True, "verification_status": "verified", "voice_verified": True, "challenge_complete": True} for i, g in [("u1", "man"), ("u2", "woman"), ("u3", "man"), ("admin1", "woman")]]
    db.t["screening_profiles"] = [scr_row(u["id"], gender=u["gender"], interested_in=u["interested_in"]) for u in db.t["users"]]
    db.t["conversations"] = [{"id": "c1", "user1_id": "u1", "user2_id": "u2", "phase": "anon"}]
    db.t["messages"] = [{"id": "m1", "conversation_id": "c1", "sender_id": "u2", "content": "hi", "created_at": "2026-01-01"}]
    db.t["matches"] = [{"user1_id": "u1", "user2_id": "u2", "status": "active"}]
    c = m.app.test_client()
    h = lambda u: {"Authorization": "Bearer " + m.make_token(u)}
    return m, db, c, h

def test_unauth_and_bad_token(env):
    m, db, c, h = env
    assert c.get("/api/profile/me").status_code == 401
    assert c.get("/api/profile/me", headers={"Authorization": "Bearer junk"}).status_code == 401

def test_messages_isolated(env):
    m, db, c, h = env
    assert c.get("/api/conversations/c1/messages", headers=h("u3")).status_code == 403   # outsider (was a data leak)
    assert c.get("/api/conversations/c1/messages", headers=h("u1")).status_code == 200
    assert c.get("/api/conversations/c1/messages?limit=abc", headers=h("u1")).status_code == 400

def test_send_message_rules(env):
    m, db, c, h = env
    assert c.post("/api/conversations/c1/messages", json={"content": "x"}, headers=h("u3")).status_code == 403
    assert c.post("/api/conversations/nope/messages", json={"content": "x"}, headers=h("u1")).status_code == 403
    assert c.post("/api/conversations/c1/messages", json={"content": "x" * 2001}, headers=h("u1")).status_code == 400
    assert c.post("/api/conversations/c1/messages", json={"content": "hello"}, headers=h("u1")).status_code == 201
    db.t["matches"][0]["status"] = "ended"
    assert c.post("/api/conversations/c1/messages", json={"content": "hello"}, headers=h("u1")).status_code == 403

def test_reveal_cannot_self_accept(env):
    m, db, c, h = env
    assert c.post("/api/conversations/c1/reveal", json={"action": "request"}, headers=h("u1")).status_code == 200
    assert c.post("/api/conversations/c1/reveal", json={"action": "accept"}, headers=h("u1")).status_code == 409
    assert c.post("/api/conversations/c1/reveal", json={"action": "accept"}, headers=h("u2")).status_code == 200
    assert db.t["conversations"][0]["phase"] != "anon"

def test_like_validation_and_unlike(env):
    m, db, c, h = env
    assert c.post("/api/likes", json={"liked_id": "u1"}, headers=h("u1")).status_code == 400
    assert c.post("/api/likes", json={"liked_id": "x),and(a.eq.1"}, headers=h("u1")).status_code == 400   # filter injection
    assert c.post("/api/likes", json={"liked_id": "ghost"}, headers=h("u1")).status_code == 404
    assert c.delete("/api/likes/u2", headers=h("u1")).status_code == 200
    assert c.post("/api/conversations/open", json={"other_user_id": "a,b"}, headers=h("u1")).status_code == 400

def test_signup_validation(env):
    m, db, c, h = env
    base = dict(first_name="A", last_name="B", email="a@b.co", phone="1", password="longenough", dob="2000-01-01", gender="man", interested_in="woman")
    assert c.post("/api/auth/signup", json={**base, "password": "short"}).status_code == 400
    assert c.post("/api/auth/signup", json={**base, "email": "nope"}).status_code == 400
    assert c.post("/api/auth/signup", json={**base, "dob": "2015-01-01"}).status_code == 403
    assert c.post("/api/auth/signup", json={**base, "first_name": "<script>"}).status_code == 400
    assert c.post("/api/auth/signup", data="{bad", content_type="application/json").status_code == 400  # malformed JSON

def test_login_rate_limit_and_bad_creds(env):
    m, db, c, h = env
    codes = [c.post("/api/auth/login", json={"email": "z@z.co", "password": "x"}).status_code for _ in range(12)]
    assert codes[0] == 401 and codes[-1] == 429

def test_errors_are_json_no_stack(env):
    m, db, c, h = env
    r = c.get("/api/nope"); assert r.status_code == 404 and r.get_json()["success"] is False
    assert c.post("/api/health").status_code == 405

def test_cors_not_wildcard(env):
    m, db, c, h = env
    r = c.get("/api/health", headers={"Origin": "http://evil.example"})
    assert r.headers.get("Access-Control-Allow-Origin") != "*" and "evil" not in (r.headers.get("Access-Control-Allow-Origin") or "")
    assert c.get("/api/health", headers={"Origin": "http://localhost:5000"}).headers.get("Access-Control-Allow-Origin") == "http://localhost:5000"

def test_profile_and_gender_validation(env):
    m, db, c, h = env
    assert c.patch("/api/profile/gender", json={"gender": "x"}, headers=h("u1")).status_code == 400
    assert c.patch("/api/profile/gender", json={"interested_in": "women"}, headers=h("u1")).status_code == 200
    assert c.patch("/api/profile/update", json={"bio": "<img onerror=1>"}, headers=h("u1")).status_code == 400
    assert c.patch("/api/profile/update", json={"photos": "str"}, headers=h("u1")).status_code == 400

def test_admin_and_verification_cannot_be_self_approved(env):
    m, db, c, h = env
    assert c.get("/api/admin/verifications", headers=h("u1")).status_code == 403
    assert c.post("/api/admin/verifications/v1/review", json={"decision": "verified"}, headers=h("u1")).status_code == 403
    assert c.post("/api/admin/contests", json={"title": "t"}, headers=h("u1")).status_code == 403
    assert c.patch("/api/profile/update", json={"is_verified": True, "verification_status": "verified"}, headers=h("u1")).status_code == 200
    assert "is_verified" not in db.t.get("profiles", [{}])[0] if db.t.get("profiles") else True

def test_video_requires_match_and_consent(env):
    m, db, c, h = env
    assert c.post("/api/video/schedule", json={"guest_id": "u3"}, headers=h("u1")).status_code == 403   # not matched
    r = c.post("/api/video/schedule", json={"guest_id": "u2"}, headers=h("u1")); assert r.status_code == 201
    room = r.get_json()["data"]["room_id"]
    assert c.get(f"/api/video/room/{room}/join", headers=h("u2")).status_code == 403   # not accepted yet
    assert c.get(f"/api/video/room/{room}/join", headers=h("u3")).status_code == 404   # outsider

def test_dating_and_breakup(env):
    m, db, c, h = env
    assert c.post("/api/dating/request", json={"target_id": "u3"}, headers=h("u1")).status_code == 403
    r = c.post("/api/dating/request", json={"target_id": "u2"}, headers=h("u1")); assert r.status_code == 201
    rid = r.get_json()["data"]["id"]
    assert c.post(f"/api/dating/{rid}/respond", json={"action": "accept"}, headers=h("u1")).status_code == 403  # sender can't accept
    assert c.post(f"/api/dating/{rid}/respond", json={"action": "accept"}, headers=h("u2")).status_code == 200
    r = c.post("/api/connections/u2/end", json={"feedback": {"reason": "x", "comment": "private"}}, headers=h("u1")); assert r.status_code == 200
    assert db.t["matches"][0]["status"] == "ended"
    assert c.get("/api/breakup-feedback", headers=h("u2")).get_json()["data"] == []      # other person never sees it
    assert len(c.get("/api/breakup-feedback", headers=h("u1")).get_json()["data"]) == 1

def test_streak_needs_real_activity(env):
    m, db, c, h = env
    assert c.post("/api/streak/checkin", headers=h("u3")).status_code == 409

def test_compat_convo_route(env):
    m, db, c, h = env
    assert c.get("/api/conversations/c1/compatibility", headers=h("u3")).status_code == 403
    assert c.get("/api/conversations/c1/compatibility", headers=h("u1")).status_code == 200

def test_signup_db_errors_are_reported(env):
    m, db, c, h = env
    base = dict(first_name="A", last_name="B", email="n@b.co", phone="1", password="longenough", dob="2000-01-01", gender="man", interested_in="woman")
    orig = db.table
    class Boom(Exception): pass
    def table(n):
        q = orig(n)
        if n == "users":
            q.insert = lambda p: (_ for _ in ()).throw(Boom('duplicate key value violates unique constraint 23505'))
        return q
    db.table = table
    assert c.post("/api/auth/signup", json=base).status_code == 409


# ───────── Photo reveal flow ─────────
def _seed_profiles(db):
    db.t["profiles"] = [{"user_id": "u2", "bio": "hello there friend", "city": "Chennai", "photos": ["http://x/2.jpg"], "anonymous_mode": False},
                        {"user_id": "u1", "bio": "hi", "city": "Chennai", "photos": ["http://x/1.jpg"], "anonymous_mode": False}]

def test_reveal_request_needs_valid_receiver(env):
    m, db, c, h = env
    assert c.post("/api/reveal/request", json={}, headers=h("u1")).status_code == 400          # the reported bug: null receiver
    assert c.post("/api/reveal/request", json={"receiver_id": None}, headers=h("u1")).status_code == 400
    assert c.post("/api/reveal/request", json={"receiver_id": "u1"}, headers=h("u1")).status_code == 400   # self

def test_reveal_request_needs_conversation(env):
    m, db, c, h = env
    r = c.post("/api/reveal/request", json={"receiver_id": "admin1"}, headers=h("u1"))
    assert r.status_code == 403 and "conversation" in r.get_json()["error"]

def test_reveal_full_flow(env):
    m, db, c, h = env
    _seed_profiles(db)
    # photos hidden before reveal
    r = c.get("/api/users/u2", headers=h("u1")).get_json()["data"]
    assert r["photos"] == [] and r["photos_locked"] is True and r["photo_count"] == 1
    # u1 sends request -> u2 gets notified and sees it as pending_received
    assert c.post("/api/reveal/request", json={"receiver_id": "u2"}, headers=h("u1")).status_code == 201
    assert any(n["recipient_id"] == "u2" and n["type"] == "reveal_request" for n in db.t["notifications"])
    s1 = c.get("/api/reveal/status/u2", headers=h("u1")).get_json()["data"]
    s2 = c.get("/api/reveal/status/u1", headers=h("u2")).get_json()["data"]
    assert s1["state"] == "pending_sent" and s2["state"] == "pending_received"
    # duplicate blocked; sender cannot accept own request; outsider cannot answer
    assert c.post("/api/reveal/request", json={"receiver_id": "u2"}, headers=h("u1")).status_code == 409
    rid = s2["request_id"]
    assert c.post("/api/reveal/respond", json={"request_id": rid, "action": "accept"}, headers=h("u1")).status_code == 403
    assert c.post("/api/reveal/respond", json={"request_id": rid, "action": "accept"}, headers=h("u3")).status_code == 403
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == []   # still hidden
    # u2 accepts -> both directions unlocked, u1 notified, can't answer twice
    assert c.post("/api/reveal/respond", json={"request_id": rid, "action": "accept"}, headers=h("u2")).status_code == 200
    assert any(n["recipient_id"] == "u1" and n["type"] == "reveal_accepted" for n in db.t["notifications"])
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == ["http://x/2.jpg"]
    assert c.get("/api/users/u1", headers=h("u2")).get_json()["data"]["photos"] == ["http://x/1.jpg"]
    assert c.get("/api/users/u2", headers=h("u3")).get_json()["data"]["photos_locked"] is True   # others still locked
    assert c.get("/api/reveal/status/u2", headers=h("u1")).get_json()["data"]["state"] == "revealed"
    assert c.post("/api/reveal/respond", json={"request_id": rid, "action": "decline"}, headers=h("u2")).status_code == 409

def test_reveal_decline_cooldown(env):
    m, db, c, h = env
    c.post("/api/reveal/request", json={"receiver_id": "u2"}, headers=h("u1"))
    rid = c.get("/api/reveal/status/u1", headers=h("u2")).get_json()["data"]["request_id"]
    assert c.post("/api/reveal/respond", json={"request_id": rid, "action": "decline"}, headers=h("u2")).status_code == 200
    assert any(n["recipient_id"] == "u1" and n["type"] == "reveal_declined" for n in db.t["notifications"])
    assert c.get("/api/reveal/status/u2", headers=h("u1")).get_json()["data"]["state"] == "declined"
    assert c.post("/api/reveal/request", json={"receiver_id": "u2"}, headers=h("u1")).status_code == 429


# ───────── Ending a match ─────────
def test_end_match_locks_photos_hides_chat_blocks_messages(env):
    m, db, c, h = env
    _seed_profiles(db)
    c.post("/api/reveal/request", json={"receiver_id": "u2"}, headers=h("u1"))
    rid = c.get("/api/reveal/status/u1", headers=h("u2")).get_json()["data"]["request_id"]
    c.post("/api/reveal/respond", json={"request_id": rid, "action": "accept"}, headers=h("u2"))
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == ["http://x/2.jpg"]
    assert [x["id"] for x in c.get("/api/conversations", headers=h("u1")).get_json()["data"]] == ["c1"]
    r = c.post("/api/connections/u2/end", json={"feedback": {"reason": "not comfortable", "comment": "private"}}, headers=h("u1"))
    assert r.status_code == 200
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == []            # photo access revoked
    assert c.get("/api/users/u1", headers=h("u2")).get_json()["data"]["photos_locked"] is True   # both directions
    assert c.get("/api/conversations", headers=h("u1")).get_json()["data"] == []                 # gone for the one who ended
    assert c.get("/api/conversations", headers=h("u2")).get_json()["data"] == []                 # and for the other person
    assert c.post("/api/conversations/c1/messages", json={"content": "hi"}, headers=h("u2")).status_code == 403
    assert c.post("/api/reveal/request", json={"receiver_id": "u1"}, headers=h("u2")).status_code == 403
    assert any(n["recipient_id"] == "u2" and n["type"] == "connection_ended" for n in db.t["notifications"])
    assert not any("private" in str(n) for n in db.t["notifications"])                            # feedback never leaks
    assert c.post("/api/connections/u2/end", json={}, headers=h("u1")).status_code == 404         # can't end twice
    assert c.post("/api/connections/u3/end", json={}, headers=h("u1")).status_code == 404         # no connection