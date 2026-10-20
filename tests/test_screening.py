"""Profile screening, eligibility, age gate, gating and compatibility tests (in-memory fake Supabase)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime, timedelta, timezone
from test_api import env, scr_row  # noqa: F401  (pytest fixture import)

FULL = dict(age_confirmed=True, gender="man", interested_in="woman", relationship_goal="marriage", monogamy="monogamous", marriage_timeline="1_2y",
            children_plans="want", age_pref_min=24, age_pref_max=35, distance_pref=60, city="Chennai", dealbreakers=["smoker"],
            lifestyle=["non_smoker"], values=["family", "honesty"], interests=["music"], comm_style="direct", emotional_readiness="ready",
            consent={"guidelines": True, "privacy": True, "safety": True})
J = lambda c, m, url, h, body=None: getattr(c, m)(url, json=body, headers=h)

def dob_years_ago(years, extra_days=0):
    t = datetime.now(timezone.utc).date(); y = t.replace(year=t.year - years) if not (t.month == 2 and t.day == 29) else t.replace(year=t.year - years, day=28)
    return (y - timedelta(days=extra_days)).isoformat()

def signup(c, **kw):
    b = dict(first_name="N", last_name="U", email="n%d@t.co" % abs(hash(str(kw))), phone="1", password="password1", dob="1998-01-01", gender="man", interested_in="woman"); b.update(kw)
    return c.post("/api/auth/signup", json=b)

# ── age gate (backend) ──
def test_signup_blocks_under_18_exactly(env):
    m, db, c, h = env
    assert signup(c, dob=dob_years_ago(17)).status_code == 403
    assert signup(c, dob=dob_years_ago(18, -1)).status_code == 403          # turns 18 tomorrow
    assert signup(c, dob=dob_years_ago(18)).status_code == 201              # 18 today
    assert signup(c, dob=dob_years_ago(18, 400)).status_code == 201
    assert signup(c, dob=(datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()).status_code == 400   # future
    assert signup(c, dob="not-a-date").status_code == 400
    assert signup(c, dob="1800-01-01").status_code == 400
    assert not any(u.get("age", 99) < 18 for u in db.t["users"])

def test_signup_eligibility_messages(env):
    m, db, c, h = env
    for g, i in (("man", "man"), ("woman", "woman"), ("man", "everyone"), ("other", "woman"), (None, None)):
        r = signup(c, gender=g, interested_in=i); j = r.get_json()
        assert r.status_code == 422 and j["code"] == "ineligible" and "women and men" in j["error"], (g, i)
    assert signup(c, gender="woman", interested_in="man").status_code == 201

def test_signup_stores_canonical_values_and_incomplete(env):
    m, db, c, h = env
    r = signup(c, gender="male", interested_in="women"); assert r.status_code == 201
    u = db.t["users"][-1]; assert (u["gender"], u["interested_in"]) == ("man", "woman") and not u.get("screening_complete")

# ── screening API: validation ──
def test_screening_requires_auth_and_json(env):
    m, db, c, h = env
    assert c.get("/api/screening").status_code == 401 and c.put("/api/screening", json={}).status_code == 401
    assert c.put("/api/screening", data="[1,2]", content_type="application/json", headers=h("u1")).status_code == 400
    assert c.put("/api/screening", data="{bad", content_type="application/json", headers=h("u1")).status_code in (400,)

def test_screening_rejects_invalid_answers(env):
    m, db, c, h = env
    for bad in ({"age_pref_min": 17}, {"age_pref_max": 17}, {"age_pref_min": 100}, {"age_pref_min": "abc"}, {"age_pref_min": True}, {"age_pref_min": 40, "age_pref_max": 30},
                {"relationship_goal": "hookup"}, {"monogamy": ""}, {"children_plans": 5}, {"distance_pref": 0}, {"distance_pref": 99999}, {"city": "<script>"},
                {"city": ""}, {"lifestyle": ["flying"]}, {"values": "family"}, {"gender": "alien"}, {"interested_in": "nobody"}):
        r = J(c, "put", "/api/screening", h("u1"), bad); assert r.status_code == 400, bad
        assert r.get_json()["success"] is False

def test_under_18_stored_account_cannot_screen(env):
    m, db, c, h = env
    next(u for u in db.t["users"] if u["id"] == "u1")["dob"] = dob_years_ago(17)
    assert J(c, "put", "/api/screening", h("u1"), FULL).status_code == 403

# ── screening API: saving, incomplete state, completion ──
def new_user(c, db, h, **kw):
    r = signup(c, **kw).get_json()["data"]; return r["user"]["id"], {"Authorization": "Bearer " + r["token"]}

def test_incomplete_until_everything_valid(env):
    m, db, c, h = env
    uid, hh = new_user(c, db, h, email="x@t.co")
    U = lambda: next(u for u in db.t["users"] if u["id"] == uid)
    assert J(c, "put", "/api/screening", hh, {"relationship_goal": "marriage"}).status_code == 200
    assert U().get("screening_complete") is not True
    g = J(c, "get", "/api/screening", hh).get_json()["data"]
    assert g["status"] == "incomplete" and "consent" in g["missing"] and g["answers"]["relationship_goal"] == "marriage"
    r = J(c, "put", "/api/screening", hh, {"submit": True}); j = r.get_json()
    assert r.status_code == 400 and "consent" in j["missing"] and U().get("screening_complete") is not True
    partial = {k: v for k, v in FULL.items() if k not in ("age_confirmed", "consent")}
    r = J(c, "put", "/api/screening", hh, {**partial, "submit": True}); assert r.status_code == 400 and set(r.get_json()["missing"]) == {"consent", "age_confirmed"}
    r = J(c, "put", "/api/screening", hh, {**FULL, "consent": {"guidelines": True, "privacy": True, "safety": False}, "submit": True}); assert r.status_code == 400
    assert U().get("screening_complete") is not True
    r = J(c, "put", "/api/screening", hh, {**FULL, "submit": True}); assert r.status_code == 200 and r.get_json()["data"]["status"] == "complete"
    assert U()["screening_complete"] is True
    p = next(x for x in db.t["preferences"] if x["user_id"] == uid); assert (p["age_min"], p["age_max"], p["max_distance"], p["looking_for"]) == (24, 35, 60, "Marriage")
    pp = next(x for x in db.t["personality_profiles"] if x["user_id"] == uid); assert pp["communication_type"] == "direct" and pp["wants_children"] == "yes"
    s = next(x for x in db.t["screening_profiles"] if x["user_id"] == uid); assert s["status"] == "complete" and s["consent_at"] and s["consent_version"]
    assert len([x for x in db.t["screening_profiles"] if x["user_id"] == uid]) == 1   # upsert, no duplicates

def test_ineligible_answers_saved_incomplete_with_respectful_message(env):
    m, db, c, h = env
    uid, hh = new_user(c, db, h, email="y@t.co")
    r = J(c, "put", "/api/screening", hh, {**FULL, "interested_in": "man", "submit": True}); j = r.get_json()
    assert r.status_code == 422 and j["code"] == "ineligible" and "change them" in j["error"]
    assert next(u for u in db.t["users"] if u["id"] == uid).get("screening_complete") is not True
    assert next(x for x in db.t["screening_profiles"] if x["user_id"] == uid)["status"] == "incomplete"
    assert J(c, "put", "/api/screening", hh, {**FULL, "submit": True}).status_code == 200   # fixing the answers works

def test_completed_profile_reopened_with_bad_edit_becomes_incomplete(env):
    m, db, c, h = env
    assert J(c, "put", "/api/screening", h("u1"), {"interested_in": "man"}).status_code == 422
    assert next(u for u in db.t["users"] if u["id"] == "u1")["screening_complete"] is False

def test_cannot_set_privileged_fields_through_screening(env):
    m, db, c, h = env
    J(c, "put", "/api/screening", h("u1"), {"screening_complete": True, "status": "complete", "is_verified": True, "user_id": "u2", "age": 99})
    assert not any(u.get("is_verified") for u in db.t["users"]) and next(u for u in db.t["users"] if u["id"] == "u1")["age"] == 30
    assert all(x["user_id"] != "u2" or x["gender"] == "woman" for x in db.t["screening_profiles"])

# ── gating / bypass attempts ──
def test_matching_requires_completed_screening(env):
    m, db, c, h = env
    uid, hh = new_user(c, db, h, email="z@t.co", gender="woman", interested_in="man")
    db.t["users"][-1]["onboarded"] = True
    r = J(c, "get", "/api/matches", hh); assert r.status_code == 200 and r.get_json()["data"] == []
    assert J(c, "post", "/api/likes", hh, {"liked_id": "u1"}).status_code == 403
    assert J(c, "post", "/api/conversations/open", hh, {"other_user_id": "u1"}).status_code == 403
    assert J(c, "post", "/api/likes", h("u1"), {"liked_id": uid}).status_code == 403          # others can't like an unscreened user either
    assert J(c, "post", "/api/conversations/open", h("u1"), {"other_user_id": uid}).status_code == 403

def test_discovery_filters_age_dealbreakers_eligibility(env):
    m, db, c, h = env
    for u in db.t["users"]: u["onboarded"] = True
    ids = lambda uid: {x["id"] for x in J(c, "get", "/api/matches", h(uid)).get_json()["data"]}
    assert ids("u1") == {"u2", "admin1"} and ids("u2") == {"u1", "u3"}
    S = {x["user_id"]: x for x in db.t["screening_profiles"]}
    S["u1"]["age_pref_max"] = 28; assert ids("u1") == set()                                    # candidates are 30
    S["u1"]["age_pref_max"] = 40; S["u2"]["lifestyle"] = ["smoker"]; S["u1"]["dealbreakers"] = ["smoker"]
    assert ids("u1") == {"admin1"} and "u1" not in ids("u2")                                   # dealbreaker hides both ways
    r = J(c, "post", "/api/likes", h("u1"), {"liked_id": "u2"}); assert r.status_code == 403 and "smok" not in r.get_data(as_text=True).lower()
    S["u2"]["lifestyle"] = ["active"]; S["u1"]["dealbreakers"] = []
    db.t["users"][0]["interested_in"] = "man"; assert ids("u1") == set()                       # ineligible pair

def test_likes_between_eligible_screened_users_still_work(env):
    m, db, c, h = env
    assert J(c, "post", "/api/likes", h("u1"), {"liked_id": "u2"}).status_code == 200
    assert J(c, "post", "/api/likes", h("u2"), {"liked_id": "u1"}).status_code == 200
    assert J(c, "post", "/api/conversations/open", h("u1"), {"other_user_id": "admin1"}).status_code == 403   # no mutual like yet
    assert J(c, "post", "/api/likes", h("admin1"), {"liked_id": "u1"}).status_code == 200
    assert J(c, "post", "/api/conversations/open", h("u1"), {"other_user_id": "admin1"}).status_code in (200, 201)

def test_login_payload_reports_screening_state(env):
    m, db, c, h = env
    signup(c, email="l@t.co"); r = c.post("/api/auth/login", json={"email": "l@t.co", "password": "password1"}).get_json()
    assert not r["data"]["user"].get("screening_complete")

def test_onboarding_rejects_underage_preferences(env):
    m, db, c, h = env
    for p in ({"age_min": 17, "age_max": 30}, {"age_min": 30, "age_max": 20}, {"age_min": "x", "age_max": 30}, {"age_min": 18, "age_max": 100}, {"age_min": 20, "age_max": 30, "max_distance": 0}):
        assert J(c, "post", "/api/profile/onboard", h("u1"), {"preferences": p}).status_code == 400, p
    assert J(c, "post", "/api/profile/onboard", h("u1"), {"preferences": {"age_min": 18, "age_max": 99, "max_distance": 50}}).status_code == 201

# ── compatibility ──
def test_weights_sum_to_100_and_score_is_deterministic(env):
    m, db, c, h = env
    assert sum(m.SCREEN_WEIGHTS.values()) == 100
    a, b = scr_row("a"), scr_row("b", gender="woman", interested_in="man")
    s1 = m._match_score(a, {"openness": 80}, b, {"openness": 75}); s2 = m._match_score(a, {"openness": 80}, b, {"openness": 75})
    assert s1 == s2 and 5 <= s1[0] <= 99 and len(s1[1]) == len(m.SCREEN_WEIGHTS)

def test_priorities_move_the_score_and_are_explained(env):
    m, db, c, h = env
    P = {"openness": 80, "conscientiousness": 80, "agreeableness": 80, "emotional_stability": 80, "extraversion": 60, "communication_type": "direct", "wants_children": "yes"}
    a = scr_row("a"); same = m._match_score(a, P, scr_row("b"), P)
    assert same[0] >= 90 and {"Similar relationship goals", "Aligned family plans", "Shared values"} <= set(m._reasons(same[1]))
    goals = m._match_score(a, P, scr_row("b", relationship_goal="casual"), P)[0]
    kids = m._match_score(a, P, scr_row("b", children_plans="dont_want"), P)[0]
    vals = m._match_score(a, P, scr_row("b", values=["ambition"]), P)[0]
    assert goals <= 60 and kids <= 55 and vals < same[0] - 8          # opposite goals / family plans cap the score
    assert m._match_score(a, P, scr_row("b", relationship_goal="marriage"), P)[0] > goals       # long-term vs marriage closer than vs casual
    assert m._match_score(a, P, scr_row("b", city="Mumbai"), P)[0] < same[0]
    assert m._match_score({}, P, scr_row("b"), P)[0] == m._compute_compatibility(P, P)           # falls back without screening
    assert m._compute_compatibility({"openness": None}, {"openness": 70}) >= 60                 # null-safe

def test_discovery_returns_reasons_and_report_has_factors(env):
    m, db, c, h = env
    for u in db.t["users"]: u["onboarded"] = True
    r = J(c, "get", "/api/matches", h("u1")).get_json()["data"]
    assert r and r[0]["compat_reasons"] and r[0]["compat_factors"] and r[0]["compatibility"] >= 80
    assert J(c, "get", "/api/matches", h("u1")).get_json()["data"][0]["compatibility"] == r[0]["compatibility"]   # no randomness
    rep = J(c, "get", "/api/matches/u2/report", h("u1")).get_json()["data"]
    assert rep["factors"] and rep["overall"] == next(x for x in r if x["id"] == "u2")["compatibility"]