"""21-day challenge, movie analysis, strict pre-match privacy and exclusive pairing. In-memory fake Supabase (see test_api)."""
import io, json, threading
from datetime import date, datetime, timedelta, timezone
import pytest
from test_api import env, scr_row  # noqa: F401  (pytest fixture import)
import challenge as ch

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
def png(n): return PNG + bytes([n % 256, n // 256])      # distinct bytes => distinct SHA-256
def H(m, u): return {"Authorization": "Bearer " + m.make_token(u)}
def J(c, verb, url, h, body=None, **kw): return getattr(c, verb)(url, json=body, headers=h, **kw)

ONBOARD = {"hobby": "fitness", "timezone": "Asia/Kolkata", "routine": {"sleep": "11pm-6am", "participation": "daily"},
           "genres_like": ["drama"], "languages": ["tamil"]}

def onboard_all(db):
    for u in db.t["users"]: u["onboarded"] = True        # discovery only lists onboarded users

def lock_out(db, *uids):
    for u in db.t["users"]:
        if u["id"] in uids: u["challenge_complete"] = False

def submit(c, m, uid, n=1, ctype="image/png", data=None):
    return c.post("/api/challenge/submit", headers=H(m, uid), content_type="multipart/form-data",
                  data={"proof": (io.BytesIO(data if data is not None else png(n)), "p.png", ctype)})

def seed_days(db, uid, dates, status="approved"):
    for i, d in enumerate(dates):
        db.t.setdefault("challenge_submissions", []).append({"id": f"s-{uid}-{i}-{status}", "user_id": uid, "local_date": d, "status": status,
                                                              "created_at": "2026-01-01T00:00:00+00:00"})

def seed_movies(db, uid, rows):
    for i, (t, g, r, lang) in enumerate(rows):
        db.t.setdefault("movie_logs", []).append({"id": f"mv-{uid}-{i}", "user_id": uid, "title": t, "title_key": t.lower(), "genres": g, "rating": r, "language": lang})

GOOD_MOVIES = [("A", ["drama"], 5, "tamil"), ("B", ["drama", "romance"], 4, "tamil"), ("C", ["comedy"], 4, "hindi"), ("D", ["comedy"], 5, "hindi"), ("E", ["thriller"], 2, "english")]

# ── pure logic ──
def test_streak_resets_after_a_missed_day_and_needs_21_approved():
    t = date(2026, 3, 30)
    rows = [{"local_date": t - timedelta(days=i), "status": "approved"} for i in range(21)]
    assert ch.compute_progress(rows, t)["complete"] is True
    rows = [{"local_date": t - timedelta(days=i), "status": "approved"} for i in range(21) if i != 10]       # one missed day in the middle
    p = ch.compute_progress(rows, t)
    assert p["complete"] is False and p["best_streak"] == 10 and p["current_streak"] == 10
    rows = [{"local_date": t - timedelta(days=i), "status": "approved" if i else "pending"} for i in range(21)]  # pending day does not count for eligibility
    assert ch.compute_progress(rows, t)["complete"] is False
    assert ch.compute_progress([{"local_date": t - timedelta(days=3), "status": "approved"}], t)["current_streak"] == 0   # streak already broken

def test_local_day_uses_timezone_and_cutoff():
    now = datetime(2026, 3, 30, 20, 0, tzinfo=timezone.utc)                  # 01:30 on 31 Mar in Kolkata
    assert ch.local_day(now, "Asia/Kolkata", 4) == date(2026, 3, 30)         # before the 04:00 cutoff => still the 30th
    assert ch.local_day(now, "Asia/Kolkata", 0) == date(2026, 3, 31)
    assert ch.local_day(now, "No/Such_Zone", 0) == date(2026, 3, 30)         # bad tz falls back to UTC, never crashes

def test_movie_insights_need_repeats_and_compat_is_transparent():
    ins = ch.movie_insights([{"genres": ["drama"], "rating": 5}, {"genres": ["drama"], "rating": 4}, {"genres": ["horror"], "rating": 1}])
    assert ins["likes"] == ["drama"] and "horror" in ins["uncertain"] and ins["dislikes"] == []       # one low rating is not a dislike
    a = {"likes": ["drama", "comedy"], "dislikes": ["horror"], "languages": ["tamil"]}
    b = {"likes": ["drama"], "dislikes": [], "languages": ["tamil"]}
    assert ch.movie_compat(a, b) == (100, ["drama"], ["tamil"])
    assert ch.movie_compat(a, {"likes": ["horror"], "dislikes": ["drama"], "languages": []})[0] < 50

# ── Stage A/B: onboarding + submissions ──
def test_incomplete_user_cannot_submit_until_onboarding(env):
    m, db, c, h = env; lock_out(db, "u1")
    assert submit(c, m, "u1").status_code == 403
    assert J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD).status_code == 200
    assert submit(c, m, "u1").status_code == 201

def test_submission_rules_duplicates_replay_and_file_checks(env):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    assert submit(c, m, "u1", ctype="text/html", data=b"<script>").status_code == 400
    assert submit(c, m, "u1", data=b"not an image at all").status_code == 400               # bad magic bytes despite image mimetype
    assert submit(c, m, "u1", data=b"").status_code == 400
    assert submit(c, m, "u1", data=PNG + b"x" * (6 * 1024 * 1024)).status_code == 400        # too large
    r = submit(c, m, "u1", 1); assert r.status_code == 201 and r.get_json()["data"]["status"] == "pending"
    assert submit(c, m, "u1", 2).status_code == 409                                          # second proof the same day
    s = db.t["challenge_submissions"][0]
    assert "u1" in s["file_path"] and s["file_path"] in db.storage.files
    s["status"] = "rejected"; s["created_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert submit(c, m, "u1", 1).status_code == 409                                          # replaying the rejected file is refused
    assert submit(c, m, "u1", 3).status_code == 201                                          # a fresh file is a valid resubmission

def test_client_cannot_choose_date_or_streak(env):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    r = c.post("/api/challenge/submit", headers=H(m, "u1"), content_type="multipart/form-data",
               data={"proof": (io.BytesIO(png(1)), "p.png", "image/png"), "local_date": "2020-01-01", "streak": "21", "status": "approved"})
    row = db.t["challenge_submissions"][0]
    assert r.status_code == 201 and row["status"] == "pending" and row["local_date"] != "2020-01-01"
    st = c.get("/api/challenge/status", headers=H(m, "u1")).get_json()["data"]
    assert st["approved_in_run"] == 0 and st["eligible"] is False

def test_timezone_change_is_locked_for_a_week(env):
    m, db, c, h = env; lock_out(db, "u1")
    assert J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD).status_code == 200
    assert J(c, "put", "/api/challenge/profile", H(m, "u1"), {"timezone": "America/New_York"}).status_code == 200      # first change ok
    assert J(c, "put", "/api/challenge/profile", H(m, "u1"), {"timezone": "Asia/Tokyo"}).status_code == 400          # then locked

def test_only_admin_reviews_and_approval_unlocks_after_21_days_and_movies(env):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    today = ch.local_day(datetime.now(timezone.utc), "Asia/Kolkata", 4)
    seed_days(db, "u1", [(today - timedelta(days=i)).isoformat() for i in range(1, 21)])       # 20 approved days already
    assert submit(c, m, "u1").status_code == 201
    sid = db.t["challenge_submissions"][-1]["id"]
    assert J(c, "post", f"/api/admin/challenge/{sid}/review", H(m, "u1"), {"decision": "approved"}).status_code == 403   # users cannot approve themselves
    seed_movies(db, "u1", GOOD_MOVIES)
    assert J(c, "post", f"/api/admin/challenge/{sid}/review", H(m, "admin1"), {"decision": "approved"}).status_code == 200
    assert next(u for u in db.t["users"] if u["id"] == "u1")["challenge_complete"] is True
    assert any(n["type"] == "challenge_complete" for n in db.t["notifications"])

def test_21_days_without_movie_analysis_is_not_enough_and_prefs_edit_keeps_progress(env):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    today = ch.local_day(datetime.now(timezone.utc), "Asia/Kolkata", 4)
    seed_days(db, "u1", [(today - timedelta(days=i)).isoformat() for i in range(0, 21)])
    J(c, "put", "/api/challenge/profile", H(m, "u1"), {"genres_like": ["comedy"]})           # editing preferences
    assert next(u for u in db.t["users"] if u["id"] == "u1")["challenge_complete"] is False
    assert len(db.t["challenge_submissions"]) == 21                                           # nothing reset by the edit
    seed_movies(db, "u1", GOOD_MOVIES)
    J(c, "put", "/api/challenge/profile", H(m, "u1"), {"genres_like": ["drama"]})
    assert next(u for u in db.t["users"] if u["id"] == "u1")["challenge_complete"] is True

def test_movie_journal_validation_and_dedupe(env):
    m, db, c, h = env
    ok_ = {"title": "Vikram", "genres": ["action", "Sci Fi"], "rating": 4, "language": "Tamil"}
    assert J(c, "post", "/api/movies", H(m, "u1"), ok_).status_code == 201
    assert J(c, "post", "/api/movies", H(m, "u1"), {**ok_, "title": " vikram "}).status_code == 409
    for bad in ({**ok_, "title": "<b>x</b>"}, {**ok_, "rating": 9}, {**ok_, "rating": True}, {**ok_, "title": "Z", "genres": ["nonsense"]}):
        assert J(c, "post", "/api/movies", H(m, "u1"), bad).status_code == 400
    assert db.t["movie_logs"][0]["genres"] == ["action", "sci-fi"]

# ── Stage D: strict pre-match privacy ──
def test_incomplete_users_are_invisible_and_see_nobody(env):
    m, db, c, h = env; onboard_all(db); db.t["matches"].clear()
    assert "u2" in [x["id"] for x in c.get("/api/matches", headers=h("u1")).get_json()["data"]]     # visible while both are complete
    lock_out(db, "u2")
    assert "u2" not in [x["id"] for x in c.get("/api/matches", headers=h("u1")).get_json()["data"]]   # u2 is hidden from u1
    assert J(c, "post", "/api/likes", h("u1"), {"liked_id": "u2"}).status_code == 403          # ...and cannot be liked
    assert J(c, "post", "/api/likes", h("u2"), {"liked_id": "u1"}).status_code == 403          # u2 cannot like out
    r = c.get("/api/matches", headers=h("u2")).get_json(); assert r["data"] == [] and "challenge" in r["message"].lower()
    assert c.get("/api/matches/mutual", headers=h("u2")).get_json()["data"] == []

def test_direct_urls_cannot_bypass(env):
    m, db, c, h = env
    db.t["conversations"].clear(); db.t["matches"].clear()                                    # no existing relationship
    lock_out(db, "u2")
    assert c.get("/api/users/u2", headers=h("u1")).status_code == 404                          # profile of an incomplete user
    assert c.get("/api/users/u1", headers=h("u2")).status_code == 404                          # incomplete user viewing a completed one
    assert c.get("/api/matches/u2/report", headers=h("u1")).status_code == 403
    assert c.get("/api/compat/u2", headers=h("u1")).status_code == 404
    assert J(c, "post", "/api/conversations/open", h("u1"), {"other_user_id": "u2"}).status_code == 403
    assert c.get("/api/users/u1", headers=h("u1")).status_code == 200                          # own data stays available
    assert c.get("/api/challenge/status", headers=h("u2")).status_code == 200                  # dashboard/challenge stay available

# ── Stage E: mutual, exclusive matching ──
def like(c, m, a, b): return J(c, "post", "/api/likes", H(m, a), {"liked_id": b})

def test_one_sided_like_does_not_match_and_mutual_creates_one_pairing(env):
    m, db, c, h = env
    db.t["matches"].clear(); db.t["conversations"].clear()
    assert like(c, m, "u1", "u2").get_json()["data"]["is_mutual"] is False and not db.t.get("active_pairings")
    assert like(c, m, "u2", "u1").get_json()["data"]["is_mutual"] is True
    assert len(db.t["active_pairings"]) == 1 and c.get("/api/pairing", headers=h("u1")).get_json()["data"]["partner_id"] == "u2"
    assert like(c, m, "u2", "u1").status_code == 200 and len(db.t["active_pairings"]) == 1    # repeat click: idempotent

def test_pairing_removes_both_from_discovery_and_cancels_other_requests(env):
    m, db, c, h = env; onboard_all(db)
    db.t["matches"].clear(); db.t["conversations"].clear()
    db.t["users"].append({"id": "u4", "first_name": "u4", "last_name": "X", "gender": "woman", "interested_in": "man", "age": 30, "dob": "1996-01-01",
                          "screening_complete": True, "verification_status": "verified", "voice_verified": True, "challenge_complete": True})
    db.t["screening_profiles"].append(scr_row("u4", gender="woman", interested_in="man"))
    like(c, m, "u4", "u1")                                                                      # u4 -> u1 pending, then u1 pairs with u2
    like(c, m, "u1", "u2"); like(c, m, "u2", "u1")
    assert not any(l["liker_id"] == "u4" and l["liked_id"] == "u1" for l in db.t["likes"])      # pending request cancelled
    assert any(n["recipient_id"] == "u4" and n["type"] == "profile_unavailable" for n in db.t["notifications"])
    n = next(n for n in db.t["notifications"] if n["type"] == "profile_unavailable"); assert "u1" not in (n["title"] + n.get("body", ""))   # nothing disclosed
    assert c.get("/api/matches", headers=h("u1")).get_json()["data"] == []                     # paired users leave discovery
    assert "u1" not in [x["id"] for x in c.get("/api/matches", headers=h("u3")).get_json()["data"]]
    assert "u1" not in [x["id"] for x in c.get("/api/matches", headers=h("u4")).get_json()["data"]]
    assert like(c, m, "u4", "u1").status_code == 403 and like(c, m, "u3", "u2").status_code == 403   # no new requests to/from paired users
    assert like(c, m, "u1", "u4").status_code == 403

def test_concurrent_mutual_likes_create_exactly_one_pairing(env):
    m, db, c, h = env
    db.t["matches"].clear(); db.t["conversations"].clear()
    db.t["users"].append({"id": "u5", "first_name": "u5", "last_name": "X", "gender": "woman", "interested_in": "man", "age": 30, "dob": "1996-01-01",
                          "screening_complete": True, "verification_status": "verified", "voice_verified": True, "challenge_complete": True})
    db.t["screening_profiles"].append(scr_row("u5", gender="woman", interested_in="man"))
    # u1 and u3 (men) both have mutual interest with u2 and u5; race the four calls
    for a, b in (("u1", "u2"), ("u3", "u2"), ("u1", "u5"), ("u3", "u5")): db.t["likes"] = db.t.get("likes", []) + [{"liker_id": b, "liked_id": a}]
    res = []
    def go(a, b): res.append(like(c, m, a, b).status_code)
    ts = [threading.Thread(target=go, args=p) for p in (("u1", "u2"), ("u3", "u2"), ("u1", "u5"), ("u3", "u5"))]
    [t.start() for t in ts]; [t.join() for t in ts]
    members = [x for x in db.t["pairing_members"] if x["active"]]
    assert len({x["user_id"] for x in members}) == len(members) <= 4                           # nobody in two active pairings
    assert all(sum(1 for x in members if x["user_id"] == u) <= 1 for u in ("u1", "u2", "u3", "u5"))

def test_rpc_enforces_exclusivity_directly(env):
    m, db, c, h = env
    ok1, _ = m._form_pairing("u1", "u2"); ok2, why = m._form_pairing("u1", "u3")
    assert ok1 and not ok2 and why == "unavailable" and len(db.t["active_pairings"]) == 1

def test_end_flow_frees_both_users_only_after_explicit_unmatch(env):
    m, db, c, h = env
    db.t["matches"].clear(); db.t["conversations"].clear()
    like(c, m, "u1", "u2"); like(c, m, "u2", "u1")
    assert m._paired_with("u1") == "u2"
    assert like(c, m, "u1", "u3").status_code == 403
    assert J(c, "post", "/api/connections/u2/end", h("u1"), {}).status_code == 200
    assert m._paired_with("u1") is None and m._paired_with("u2") is None
    assert not [x for x in db.t["pairing_members"] if x["active"]]

def test_unmatched_users_cannot_read_or_send_private_chat(env):
    m, db, c, h = env
    assert c.get("/api/conversations/c1/messages", headers=h("u3")).status_code == 403
    assert J(c, "post", "/api/conversations/c1/messages", h("u3"), {"content": "hi"}).status_code == 403
    assert J(c, "post", "/api/conversations/c1/messages", h("u1"), {"content": "hi"}).status_code == 201

def test_photos_stay_locked_until_consent(env):
    m, db, c, h = env
    db.t["profiles"] = [{"user_id": "u2", "photos": ["https://p/1.jpg"], "city": "Chennai", "anonymous_mode": True}]
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == []         # locked before the partner accepts a reveal
    assert J(c, "post", "/api/conversations/c1/reveal", h("u1"), {"action": "request"}).status_code == 200
    assert c.get("/api/users/u2", headers=h("u1")).get_json()["data"]["photos"] == []         # a pending request reveals nothing


# ── automatic AI review of the daily proof ───────────────────────────────────
def _ai(monkeypatch, result):
    """Stub the Gemini call: result is a dict (model JSON), or an Exception to simulate an outage."""
    import app as appmod
    calls = []
    def fake(url, headers, body, timeout=60):
        calls.append(json.loads(body))
        if isinstance(result, Exception): raise result
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]}
    monkeypatch.setattr(appmod, "GEMINI_API_KEY", "test-key"); monkeypatch.setattr(appmod, "_http_json", fake)
    return calls

def test_ai_approves_and_is_cheap(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    calls = _ai(monkeypatch, {"ok": True, "r": ""})
    r = submit(c, m, "u1", 1); d = r.get_json()["data"]
    assert r.status_code == 201 and d["status"] == "approved"
    row = db.t["challenge_submissions"][0]
    assert row["status"] == "approved" and row.get("reviewed_by") is None and row["reviewed_at"]
    assert len(calls) == 1                                                       # exactly one model call
    cfg = calls[0]["generationConfig"]; assert cfg["maxOutputTokens"] <= 400 and cfg["thinkingConfig"]["thinkingBudget"] == 0
    assert len(json.dumps(calls[0]["contents"][0]["parts"][1]["text"])) < 700   # tiny prompt

def test_ai_rejects_with_reason_and_allows_retry(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    _ai(monkeypatch, {"ok": False, "r": "Looks like a screenshot"})
    d = submit(c, m, "u1", 1).get_json()["data"]; assert d["status"] == "rejected" and "screenshot" in d["reason"]
    st = c.get("/api/challenge/status", headers=H(m, "u1")).get_json()["data"]
    assert st["today_status"] == "rejected" and "screenshot" in st["today_reject_reason"]
    _ai(monkeypatch, {"ok": True, "r": ""})
    assert submit(c, m, "u1", 2).get_json()["data"]["status"] == "approved"      # fresh photo can be resubmitted

def test_ai_outage_falls_back_to_human_review(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    _ai(monkeypatch, RuntimeError("boom"))
    assert submit(c, m, "u1", 1).get_json()["data"]["status"] == "pending"

def test_ai_not_called_for_duplicates_or_bad_files(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    calls = _ai(monkeypatch, {"ok": True, "r": ""})
    assert submit(c, m, "u1", ctype="image/gif", data=b"GIF89a").status_code == 400
    assert submit(c, m, "u1", 1).status_code == 201 and len(calls) == 1
    assert submit(c, m, "u1", 2).status_code == 409 and len(calls) == 1          # already submitted today: no model call

def test_recheck_resolves_a_pending_proof(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1"); J(c, "put", "/api/challenge/profile", H(m, "u1"), ONBOARD)
    _ai(monkeypatch, RuntimeError("boom")); assert submit(c, m, "u1", 1).get_json()["data"]["status"] == "pending"
    assert J(c, "post", "/api/challenge/recheck", H(m, "u1")).status_code == 503          # still down: stays pending
    _ai(monkeypatch, {"ok": True, "r": ""})
    assert J(c, "post", "/api/challenge/recheck", H(m, "u1")).status_code == 200
    assert db.t["challenge_submissions"][0]["status"] == "approved"

def test_dev_skip_is_off_by_default_and_works_when_enabled(env, monkeypatch):
    m, db, c, h = env; lock_out(db, "u1")
    monkeypatch.delenv("DEV_SKIP_CHALLENGE", raising=False)
    assert J(c, "post", "/api/dev/challenge/complete", H(m, "u1")).status_code == 404
    assert next(u for u in db.t["users"] if u["id"] == "u1")["challenge_complete"] is False
    monkeypatch.setenv("DEV_SKIP_CHALLENGE", "1")
    assert J(c, "post", "/api/dev/challenge/complete", H(m, "u1")).status_code == 200
    assert next(u for u in db.t["users"] if u["id"] == "u1")["challenge_complete"] is True

def test_paired_user_cannot_message_a_stale_chat_with_someone_else(env):
    m, db, c, h = env
    db.t["matches"].clear(); db.t["conversations"].clear()
    like(c, m, "u1", "u2"); like(c, m, "u2", "u1")                      # u1 <-> u2 are now exclusively paired
    db.t["conversations"].append({"id": "old", "user1_id": "u1", "user2_id": "u3", "phase": "anon",
                                  "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
    r = J(c, "post", "/api/conversations/old/messages", h("u1"), {"content": "hi"})
    assert r.status_code == 403
    chats = c.get("/api/conversations", headers=h("u1")).get_json()["data"]
    assert all(x["other_user_id"] == "u2" for x in chats)               # only the partner is listed


def test_logout_revokes_the_token(env):
    m, db, c, h = env
    tok = m.make_token("u1"); hdr = {"Authorization": "Bearer " + tok}
    assert c.get("/api/pairing", headers=hdr).status_code == 200
    assert c.post("/api/auth/logout", headers=hdr).status_code == 200
    assert c.get("/api/pairing", headers=hdr).status_code == 401
