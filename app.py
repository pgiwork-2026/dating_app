"""
SoulSync AI - Flask Backend
===========================
Connects the SoulSync AI frontend to Supabase PostgreSQL.
Run: python app.py
API Base: http://localhost:5000/api
"""

import os
import jwt
import bcrypt
import random
import string
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)   # Render sits behind one proxy: use the real client IP for rate limits
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024   # reject oversized uploads early


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000").split(",") if o.strip()]
CORS(app, origins=CORS_ORIGINS)   # explicit origins only (Bearer-token auth, no cookies)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str = os.environ.get("SUPABASE_SERVICE_KEY", "")   # service role key (bypasses RLS)
JWT_SECRET:   str = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET or JWT_SECRET == "soulsync-secret-change-in-prod":
    raise RuntimeError("Set JWT_SECRET in .env to a long random value")
JWT_EXPIRY_HOURS = 24 * 7   # 7 days

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# In-memory OTP store  {phone: otp}  (use Redis in production)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def make_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    p = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    if p.get("purpose"):
        raise jwt.InvalidTokenError("not a login token")
    return p


def require_auth(f):
    """Decorator — validates Bearer JWT and injects user_id."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid token"}), 401
        try:
            payload = decode_token(auth.split(" ")[1])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(user_id=payload["sub"], *args, **kwargs)
    return decorated


def ok(data=None, message="Success", status=200):
    return jsonify({"success": True, "message": message, "data": data}), status


def err(message="An error occurred", status=400):
    return jsonify({"success": False, "error": message}), status


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── Audit fixes: shared helpers ──
import re
import time
from werkzeug.exceptions import HTTPException

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _valid_id(x) -> bool:
    """Reject ids with PostgREST filter metacharacters (, ( ) .) before they reach .or_() filters."""
    return isinstance(x, str) and bool(_ID_RE.match(x))


_RL = {}


def _rate_limit(n, per):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            key, now = (request.remote_addr, f.__name__), time.time()
            hits = [t for t in _RL.get(key, []) if now - t < per]
            if len(hits) >= n:
                return err("Too many attempts — try again later", 429)
            _RL[key] = hits + [now]
            if len(_RL) > 5000:   # prune stale keys so memory can't grow without bound
                for k in [k for k, v in _RL.items() if not v or now - v[-1] > 3600]:
                    _RL.pop(k, None)
            return f(*a, **k)
        return w
    return deco


def _user_in_convo(user_id, convo_id) -> bool:
    rows = supabase.table("conversations").select("user1_id, user2_id").eq("id", convo_id).execute().data
    return bool(rows) and user_id in (rows[0]["user1_id"], rows[0]["user2_id"])


def _connection_ended(a, b) -> bool:
    m = _get_match(a, b)
    return bool(m) and m.get("status") == "ended"


def _has_markup(*vals) -> bool:
    return any(re.search(r"[<>]", str(v or "")) for v in vals)


@app.errorhandler(HTTPException)
def _http_error(e):
    return jsonify({"success": False, "error": e.description if (e.code or 500) < 500 else "Server error"}), e.code


@app.errorhandler(Exception)
def _unhandled(e):
    app.logger.exception("Unhandled error")     # details only in server log, never in the response
    return jsonify({"success": False, "error": "Internal server error"}), 500





# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.route("/api/auth/signup", methods=["POST"])
@_rate_limit(10, 60)
def signup():
    d = request.json or {}

    required = [
        "first_name",
        "email",
        "password",
        "dob"
    ]
    # gender is optional — default to "other" if not provided
    if not all(d.get(k) for k in required):
        return err("All fields are required")

    existing = (
        supabase.table("users")
        .select("id")
        .eq("email", d["email"].lower())
        .execute()
    )

    if existing.data:
        return err("Email already registered", 409)

    password_hash = hash_password(d["password"])

    try:
        dob = datetime.strptime(str(d["dob"]), "%Y-%m-%d")
        age = _age_on(str(d["dob"]))
    except ValueError:
        return err("Invalid date format. Use YYYY-MM-DD")

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(d["email"])):
        return err("Invalid email address")
    if len(str(d["password"])) < 8:
        return err("Password must be at least 8 characters")
    if age < 0 or age > 110:
        return err("Please enter a valid date of birth")
    if age < 18:
        return err("You must be 18 or older to join HEAVEN, so we can't create an account for you.", 403)
    _el, _msg = _individual_eligible(d.get("gender"), d.get("interested_in"))
    if not _el:
        return jsonify({"success": False, "error": _msg, "code": "ineligible"}), 422
    if _has_markup(d["first_name"], d.get("last_name") or ""):
        return err("Names cannot contain < or >")
    user_data = {
        "first_name": d["first_name"],
        "last_name": d.get("last_name") or "",
        "email": d["email"].lower(),
        "phone": d.get("phone") or "",
        "password_hash": password_hash,
        "dob": d["dob"],
        "age": age,
        "gender": _norm_gender(d.get("gender")),               # "man" | "woman" | "other"
        "interested_in": _norm_interest(d.get("interested_in")),  # "man" | "woman" | "everyone"
        "onboarded": False,
        "is_verified": False,   # only set True by an admin photo-verification review
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        result = supabase.table("users").insert(user_data).execute()
    except Exception as e:
        app.logger.exception("Signup insert failed")      # full traceback in the Flask console
        msg = str(e).lower()
        if "duplicate" in msg or "23505" in msg or "unique" in msg:
            return err("An account with this email or phone already exists", 409)
        if "column" in msg or "pgrst204" in msg or "42703" in msg:
            return err("Database schema mismatch (missing column) — check the Flask console", 500)
        return err("Could not create account — check the Flask console for details", 500)

    if not result.data:
        return err("Failed to create account")

    user = result.data[0]

    # Auto-create empty profile row so the user appears in Supabase immediately
    try:
        supabase.table("profiles").upsert({
            "user_id":    user["id"],
            "bio":        "",
            "city":       "",
            "occupation": "",
            "photos":     [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id").execute()
    except Exception:
        app.logger.exception("Profile row creation failed (account was created)")

    token = make_token(user["id"])

    return ok(
        {
            "token": token,
            "user": _safe_user(user)
        },
        "Account created successfully",
        201,
    )

    # Generate and store OTP (in production: send via SMS gateway like Twilio)
    # otp = gen_otp()
    # otp_store[d["phone"]] = otp
    # print(f"[OTP] Phone: {d['phone']}  OTP: {otp}")   # logs for development

    # return ok(
    #     {"user_id": user["id"], "phone": d.get("phone") or "", "otp_preview": otp},   # remove otp_preview in prod
    #     "Account created. OTP sent.",
    #     201,
    # )



    # In demo mode accept any 6-digit OTP
    # if not stored or (otp != stored and len(otp) != 6):
    #     return err("Invalid OTP", 401)

    # Mark user verified
    # supabase.table("users").update({"is_verified": True}).eq("phone", phone).execute()
    # del otp_store[phone]

    # Fetch user
    # user_res = supabase.table("users").select("*").eq("phone", phone).single().execute()
    # user = user_res.data
    # token = make_token(user["id"])

    # return ok({"token": token, "user": _safe_user(user)}, "Phone verified")


@app.route("/api/auth/login", methods=["POST"])
@_rate_limit(10, 60)
def login():
    """
    POST /api/auth/login
    Body: { email, password }
    """
    d = request.json or {}
    if not d.get("email") or not d.get("password"):
        return err("Email and password required")

    result = supabase.table("users").select("*").eq("email", d["email"].lower()).execute()
    if not result.data:
        return err("Invalid credentials", 401)

    user = result.data[0]
    if not check_password(d["password"], user["password_hash"]):
        return err("Invalid credentials", 401)

    token = make_token(user["id"])
    return ok({"token": token, "user": _safe_user(user)}, "Login successful")





def _safe_user(u: dict) -> dict:
    """Strip sensitive fields before sending to client."""
    exclude = {"password_hash"}
    return {k: v for k, v in u.items() if k not in exclude}


# ─────────────────────────────────────────────
# PROFILE / ONBOARDING
# ─────────────────────────────────────────────

@app.route("/api/profile/onboard", methods=["POST"])
@require_auth
def onboard(user_id):
    """
    POST /api/profile/onboard
    Body: { bio, city, occupation, photos[], preferences{}, personality{} }
    """
    d = request.json or {}
    _p0 = d.get("preferences") or {}
    try:
        _amin, _amax, _dist = int(_p0.get("age_min", 22)), int(_p0.get("age_max", 35)), int(_p0.get("max_distance", 50))
    except (TypeError, ValueError):
        return err("Age range and distance must be numbers")
    if not (18 <= _amin <= _amax <= 99) or not (1 <= _dist <= 20000):
        return err("Preferred partner ages must be between 18 and 99, with the minimum no greater than the maximum")

    # Upsert profile
    profile_data = {
        "user_id":    user_id,
        "bio":        d.get("bio", ""),
        "city":       d.get("city", ""),
        "occupation": d.get("occupation", ""),
        "photos":     d.get("photos", []),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("profiles").upsert(profile_data, on_conflict="user_id").execute()

    # Upsert preferences
    prefs = d.get("preferences", {})
    pref_data = {
        "user_id":       user_id,
        "age_min":       prefs.get("age_min", 22),
        "age_max":       prefs.get("age_max", 35),
        "looking_for":   prefs.get("looking_for", "Long-term relationship"),
        "max_distance":  prefs.get("max_distance", 50),
        "education":     prefs.get("education", "Any"),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("preferences").upsert(pref_data, on_conflict="user_id").execute()

    # Upsert personality
    pers = d.get("personality", {})
    pers_data = {
        "user_id":            user_id,
        "energy_type":        pers.get("energy", "mix"),
        "conflict_style":     pers.get("conflict", "talk"),
        "communication_type": pers.get("comm", "thoughtful"),
        "goal_alignment":     pers.get("goal_alignment", 8),
        "wants_children":     pers.get("kids", "open"),
        # Computed Big-5 scores (mocked — plug in real ML model)
        "openness":           pers.get("openness", random.randint(70, 95)),
        "conscientiousness":  pers.get("conscientiousness", random.randint(60, 90)),
        "extraversion":       85 if pers.get("energy") == "social" else 45,
        "agreeableness":      pers.get("agreeableness", random.randint(65, 90)),
        "emotional_stability":pers.get("emotional_stability", random.randint(60, 88)),
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("personality_profiles").upsert(pers_data, on_conflict="user_id").execute()

    # Mark user onboarded
    supabase.table("users").update({"onboarded": True}).eq("id", user_id).execute()

    # Build AI twin description
    twin_desc = _build_twin_description(pers)
    supabase.table("ai_twins").upsert({
        "user_id":     user_id,
        "description": twin_desc,
        "traits":      _build_twin_traits(pers),
        "created_at":  datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id").execute()

    return ok({"twin_description": twin_desc}, "Profile complete", 201)


def _build_twin_description(pers: dict) -> str:
    energy_map = {"solo": "introspective and self-reliant", "social": "vibrant and sociable", "mix": "balanced and adaptable"}
    comm_map   = {"direct": "direct communicator", "thoughtful": "thoughtful listener", "emotive": "expressive and warm"}
    return (
        f"An {energy_map.get(pers.get('energy','mix'))} soul who is a "
        f"{comm_map.get(pers.get('comm','thoughtful'))} — "
        f"values authentic connections and approaches life with curiosity and openness."
    )


def _build_twin_traits(pers: dict) -> list:
    traits = ["Authentic 🌟", "Curious 🔍"]
    if pers.get("energy") == "social": traits.append("Social 🎉")
    elif pers.get("energy") == "solo": traits.append("Introspective 🧗")
    else: traits.append("Balanced ⚖️")
    if pers.get("comm") == "direct": traits.append("Direct Communicator 🎯")
    elif pers.get("comm") == "emotive": traits.append("Expressive 💖")
    else: traits.append("Thoughtful 📝")
    if pers.get("kids") == "yes": traits.append("Family-oriented 👶")
    return traits



@app.route("/api/profile/gender", methods=["PATCH"])
@require_auth
def update_gender(user_id):
    """PATCH /api/profile/gender  Body: { gender, interested_in }"""
    d = request.json or {}
    update = {}
    if d.get("gender"):
        if str(d["gender"]).lower() not in ("man", "woman", "male", "female", "other"):
            return err("Invalid gender")
        update["gender"] = _norm_gender(d["gender"])
    if d.get("interested_in"):
        if str(d["interested_in"]).lower() not in ("man", "woman", "men", "women", "everyone"):
            return err("Invalid interested_in")
        update["interested_in"] = _norm_interest(d["interested_in"])
    if not update:
        return err("Provide gender and/or interested_in")
    supabase.table("users").update(update).eq("id", user_id).execute()
    return ok(message="Gender preferences updated")

@app.route("/api/profile/me", methods=["GET"])
@require_auth
def get_my_profile(user_id):
    user = supabase.table("users").select("*").eq("id", user_id).single().execute().data
    profile = supabase.table("profiles").select("*").eq("user_id", user_id).execute().data
    prefs   = supabase.table("preferences").select("*").eq("user_id", user_id).execute().data
    pers    = supabase.table("personality_profiles").select("*").eq("user_id", user_id).execute().data
    twin    = supabase.table("ai_twins").select("*").eq("user_id", user_id).execute().data

    return ok({
        "user":        _safe_user(user),
        "profile":     profile[0] if profile else {},
        "preferences": prefs[0] if prefs else {},
        "personality": pers[0] if pers else {},
        "ai_twin":     twin[0] if twin else {},
        "is_admin":    _is_admin(user_id),
    })


@app.route("/api/profile/update", methods=["PATCH"])
@require_auth
def update_profile(user_id):
    d = request.json or {}
    allowed = ["bio", "city", "occupation", "photos", "anonymous_mode"]
    if "photos" in d and (not isinstance(d["photos"], list) or len(d["photos"]) > 10):
        return err("photos must be a list of at most 10 items")
    if len(str(d.get("bio", ""))) > 1000 or _has_markup(d.get("bio"), d.get("city"), d.get("occupation")):
        return err("Invalid bio/city/occupation (max 1000 chars, no < or >)")
    if any(k in d for k in allowed):
        supabase.table("profiles").update({k: d[k] for k in allowed if k in d}).eq("user_id", user_id).execute()
    return ok(message="Profile updated")


# ─────────────────────────────────────────────
# MATCHING ENGINE
# ─────────────────────────────────────────────

@app.route("/api/matches", methods=["GET"])
@require_auth
def get_matches(user_id):
    """
    GET /api/matches?filter=all|high|new
    Returns scored matches for the authenticated user.
    """
    filter_type = request.args.get("filter", "all")

    # Fetch current user's preferences and personality
    prefs = supabase.table("preferences").select("*").eq("user_id", user_id).execute().data
    pers  = supabase.table("personality_profiles").select("*").eq("user_id", user_id).execute().data
    user  = supabase.table("users").select("*").eq("id", user_id).single().execute().data

    prefs_data = prefs[0] if prefs else {}
    pers_data  = pers[0]  if pers  else {}
    if not user.get("screening_complete"):
                return ok([], "Complete your profile screening to see matches")
    if not user.get("challenge_complete"):
        return ok([], "Complete the 21-day challenge and movie journal to unlock matches")
    if _paired_with(user_id):
        return ok([], "You're in an exclusive match, so discovery is paused")
    if not _ready(user):
        return ok([], "Verify your photo and voice in the Hub to see matches")

    # Fetch candidate users (exclude self and already-liked)
    liked_res = supabase.table("likes").select("liked_id").eq("liker_id", user_id).execute()
    liked_ids = [l["liked_id"] for l in liked_res.data]

    # Determine current user's gender for reverse-filter
    my_gender    = user.get("gender", "other")        # what I am
    my_interest  = user.get("interested_in", "everyone")  # who I want

    query = (
        supabase.table("users")
        .select("id, first_name, last_name, age, gender, interested_in, onboarded, screening_complete, verification_status, voice_verified, challenge_complete, created_at")
        .neq("id", user_id)
        .eq("onboarded", True)
    )
    candidates = query.execute().data

    # Filter out already liked
    # AFTER (keeps them visible, just marks them as liked)
    liked_ids = set(l["liked_id"] for l in liked_res.data)
    # remove the filter line — don't exclude liked users

    # ── Gender preference filter ──────────────────────────────────────────
    # Rule: show candidate C to me (user U) only if:
    #   1. I want C's gender  (or I want "everyone")
    #   2. C wants my gender  (or C wants "everyone")
    def _gender_match(me_interest, me_gender, them_gender, them_interest):
        # Does my preference match their gender?
        i_want_them = (
            me_interest == "everyone"
            or (me_interest == "men"   and them_gender == "male")
            or (me_interest == "women" and them_gender == "female")
            or (me_interest == them_gender)   # exact match fallback
        )
        # Does their preference match my gender?
        they_want_me = (
            them_interest == "everyone"
            or (them_interest == "men"   and me_gender == "male")
            or (them_interest == "women" and me_gender == "female")
            or (them_interest == me_gender)   # exact match fallback
        )
        return i_want_them and they_want_me

    candidates = [
        c for c in candidates
        if _gender_match(
            my_interest,
            my_gender,
            c.get("gender", "other"),
            c.get("interested_in", "everyone"),
        )
    ]

    # Screening gate: both completed, mutually eligible, inside each other's age range, no dealbreaker
    my_screen, scr_by_id, _vis = _screen_row(user_id), {}, []
    for c in candidates:
        cs = _screen_row(c["id"])
        if _pair_ok(user, my_screen, c, cs)[0]:
            scr_by_id[c["id"]] = cs
            _vis.append(c)
    candidates = _vis
    _ended_ids = _ended_with(user_id)
    _engaged = _engaged_ids()
    _paired = _paired_ids()
    candidates = [c for c in candidates if c["id"] not in _ended_ids and c["id"] not in _engaged and c["id"] not in _paired]   # never show someone you ended things with

    # Score each candidate
    results = []
    _revealed_set = _revealed_with(user_id)   # people whose photos I may see
    for c in candidates:
        c_profile = supabase.table("profiles").select("*").eq("user_id", c["id"]).execute().data
        c_pers    = supabase.table("personality_profiles").select("*").eq("user_id", c["id"]).execute().data

        profile  = c_profile[0] if c_profile else {}
        cp       = c_pers[0]    if c_pers    else {}

        # Compatibility score (simple algorithm — plug in ML model in production)
        score, _factors = _match_score(my_screen, pers_data, scr_by_id.get(c["id"], {}), cp, movie=_movie_pair(user_id, c["id"]))

        created_at = c.get("created_at","")
        is_new = False
        if created_at:
            try:
                ct = datetime.fromisoformat(created_at.replace("Z","+00:00"))
                is_new = (datetime.now(timezone.utc) - ct).days < 2
            except Exception:
                pass

        anon = profile.get("anonymous_mode", False)
        _all_photos = profile.get("photos") or []
        _locked = bool(_all_photos) and _photos_hidden(user_id, c["id"], anon, _revealed_set)
        results.append({
            "id":              c["id"],
            "name":            f"{c['first_name']} {c['last_name']}",
            "first_name":      c["first_name"],
            "age":             c.get("age", 25),
            "city":            profile.get("city", ""),
            "occupation":      profile.get("occupation", ""),
            "bio":             profile.get("bio", ""),
            "photos":          [] if _locked else _all_photos,
            "photos_locked":   _locked,
            "anonymous_mode":  anon,
            "compatibility":   score,
            "compat_reasons":  _reasons(_factors),
            "compat_factors":  _factors,
            "is_new":          is_new,
        })

    # Sort by compatibility
    results.sort(key=lambda x: x["compatibility"], reverse=True)

    # Apply filter
    if filter_type == "high":
        results = [r for r in results if r["compatibility"] >= 90]
    elif filter_type == "new":
        results = [r for r in results if r["is_new"]]

    return ok(results)


def _compute_compatibility(p1: dict, p2: dict) -> int:
    """
    Simple dimension-based compatibility score.
    In production replace with a trained ML model.
    """
    if not p1 or not p2:
        return 75   # deterministic default when personality data is missing

    score = 0
    weights = {
        "openness":            0.20,
        "conscientiousness":   0.15,
        "agreeableness":       0.20,
        "emotional_stability": 0.15,
        "extraversion":        0.10,
    }
    for dim, w in weights.items():
        v1 = p1.get(dim) or 70
        v2 = p2.get(dim) or 70
        diff = abs(v1 - v2)
        # Closer scores → higher compatibility on this dimension
        dim_score = max(0, 100 - diff * 1.5)
        score += dim_score * w

    # Goal alignment bonus
    if p1.get("wants_children") == p2.get("wants_children"):
        score += 10

    # Communication style compatibility
    comm_compat = {"direct-direct": 10, "thoughtful-thoughtful": 10, "emotive-emotive": 8,
                   "direct-thoughtful": 6, "thoughtful-emotive": 7, "direct-emotive": 5}
    key = f"{p1.get('communication_type','')}-{p2.get('communication_type','')}"
    score += comm_compat.get(key, comm_compat.get(f"{p2.get('communication_type','')}-{p1.get('communication_type','')}", 5))

    return min(99, max(60, int(score)))


@app.route("/api/matches/<match_id>/report", methods=["GET"])
@require_auth
def compatibility_report(user_id, match_id):
    """GET /api/matches/<match_id>/report — detailed compatibility report."""
    p1 = supabase.table("personality_profiles").select("*").eq("user_id", user_id).execute().data
    p2 = supabase.table("personality_profiles").select("*").eq("user_id", match_id).execute().data
    if not _valid_id(match_id):
        return err("Invalid id", 400)
    _mu = supabase.table("users").select("first_name, last_name").eq("id", match_id).execute().data
    if not _mu:
        return err("User not found", 404)
    _gg = None if (match_id == user_id or _have_conversation(user_id, match_id)) else _gate(user_id, match_id)
    if _gg:
        return _gg
    m_user = _mu[0]

    pd1 = p1[0] if p1 else {}
    pd2 = p2[0] if p2 else {}

    overall, _factors = _match_score(_screen_row(user_id), pd1, _screen_row(match_id), pd2, movie=_movie_pair(user_id, match_id))

    report = {
        "match_id":   match_id,
        "match_name": f"{m_user['first_name']} {m_user['last_name']}",
        "overall":    overall,
        "factors":    _factors,
        "reasons":    _reasons(_factors),
        "dimensions": {
            "openness":              {"you": pd1.get("openness",80), "them": pd2.get("openness",80)},
            "conscientiousness":     {"you": pd1.get("conscientiousness",75), "them": pd2.get("conscientiousness",75)},
            "agreeableness":         {"you": pd1.get("agreeableness",78), "them": pd2.get("agreeableness",78)},
            "emotional_stability":   {"you": pd1.get("emotional_stability",70), "them": pd2.get("emotional_stability",70)},
            "extraversion":          {"you": pd1.get("extraversion",60), "them": pd2.get("extraversion",60)},
        },
        "shared_values": pd1.get("wants_children") == pd2.get("wants_children"),
        "communication_match": pd1.get("communication_type") == pd2.get("communication_type"),
    }

    # Log report generation
    supabase.table("compatibility_reports").upsert({
        "user_id":    user_id,
        "match_id":   match_id,
        "score":      overall,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id,match_id").execute()

    return ok(report)


# ─────────────────────────────────────────────
# USERS — public profile lookup
# ─────────────────────────────────────────────

@app.route("/api/users/<target_id>", methods=["GET"])
@require_auth
def get_user(user_id, target_id):
    """GET /api/users/<id> — public profile for profile-view modal."""
    user = supabase.table("users").select("id, first_name, last_name, age, gender").eq("id", target_id).execute().data
    if not user:
        return err("User not found", 404)
    if target_id != user_id and not _have_conversation(user_id, target_id):     # strict pre-match privacy: only completed users see each other
        _both = {x["id"]: x for x in supabase.table("users").select("*").in_("id", [user_id, target_id]).execute().data}
        if len(_both) < 2 or not (_ready(_both[user_id]) and _ready(_both[target_id])):
            return err("User not found", 404)
    u = user[0]
    profile = supabase.table("profiles").select("bio, city, occupation, photos, anonymous_mode").eq("user_id", target_id).execute().data
    p = profile[0] if profile else {}
    all_photos = p.get("photos") or []
    locked = bool(all_photos) and _photos_hidden(user_id, target_id, p.get("anonymous_mode", False), _revealed_with(user_id))
    rstate = _reveal_state(user_id, target_id)["state"] if target_id != user_id else "self"
    # Build word tags from bio
    bio = p.get("bio", "")
    tags = [w for w in bio.split() if len(w) > 3][:4] if bio else []
    return ok({
        "id":         u["id"],
        "name":       f"{u['first_name']} {u['last_name']}",
        "first_name": u["first_name"],
        "age":        u.get("age"),
        "city":       p.get("city", ""),
        "occupation": p.get("occupation", ""),
        "bio":        bio,
        "photos":     [] if locked else all_photos,
        "photos_locked": locked,
        "photo_count":   len(all_photos),
        "reveal_state":  rstate,
        "tags":       tags,
    })


@app.route("/api/matches/mutual", methods=["GET"])
@require_auth
def get_mutual_matches(user_id):
    """GET /api/matches/mutual — users who mutually liked each other."""
    _me = supabase.table("users").select("*").eq("id", user_id).execute().data
    if not _me or not _ready(_me[0]):
        return ok([])
    # People I liked
    i_liked = {r["liked_id"] for r in supabase.table("likes").select("liked_id").eq("liker_id", user_id).execute().data}
    # People who liked me
    liked_me = {r["liker_id"] for r in supabase.table("likes").select("liker_id").eq("liked_id", user_id).execute().data}
    mutual_ids = list(i_liked & liked_me)
    if not mutual_ids:
        return ok([])
    results = []
    for uid in mutual_ids:
        u = supabase.table("users").select("id, first_name, last_name, age").eq("id", uid).execute().data
        p = supabase.table("profiles").select("city").eq("user_id", uid).execute().data
        if u and not _connection_ended(user_id, uid):     # hide ended connections
            row = u[0]
            results.append({
                "id":   row["id"],
                "name": f"{row['first_name']} {row['last_name']}",
                "first_name": row["first_name"],
                "age":  row.get("age"),
                "city": p[0].get("city", "") if p else "",
            })
    return ok(results)


# ─────────────────────────────────────────────
# LIKES
# ─────────────────────────────────────────────

@app.route("/api/likes", methods=["POST"])
@require_auth
def like_user(user_id):
    """POST /api/likes  Body: { liked_id }"""
    liked_id = (request.json or {}).get("liked_id")
    if not liked_id:
        return err("liked_id required")
    if not _valid_id(liked_id):
        return err("Invalid liked_id")
    if liked_id == user_id:
        return err("You cannot like yourself")
    _g = _gate(user_id, liked_id)
    if _g:
        return _g
    if _connection_ended(user_id, liked_id):
        return err("This connection has ended", 403)

    _already_liked = bool(supabase.table("likes").select("id").eq("liker_id", user_id).eq("liked_id", liked_id).execute().data)

    # Insert like
    supabase.table("likes").upsert({
        "liker_id":  user_id,
        "liked_id":  liked_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="liker_id,liked_id").execute()

    # Get liker's name for notification
    liker = supabase.table("users").select("first_name, last_name").eq("id", user_id).execute().data
    liker_name = f"{liker[0]['first_name']} {liker[0]['last_name']}" if liker else "Someone"

    # Send like notification to the liked user (only for a new like - not on repeated clicks)
    if not _already_liked:
        supabase.table("notifications").insert({
            "recipient_id": liked_id,
            "actor_id":     user_id,
            "type":         "like",
            "title":        f"{liker_name} liked your profile!",
            "is_read":      False,
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }).execute()

    # Check mutual like
    mutual = supabase.table("likes").select("id").eq("liker_id", liked_id).eq("liked_id", user_id).execute()
    is_mutual = bool(mutual.data)

    if is_mutual:
        _pok, _pwhy = _form_pairing(user_id, liked_id)      # atomic: one exclusive active pairing per user
        if not _pok:
            return err("This person is no longer available", 409) if _pwhy == "unavailable" else err("Matching is temporarily unavailable", 503)
        _cancel_other_opportunities(user_id, liked_id)
        # Create match record
        supabase.table("matches").upsert({
            "user1_id":  min(user_id, liked_id),
            "user2_id":  max(user_id, liked_id),
            "phase":     "anon",
            "matched_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user1_id,user2_id").execute()

        # Create conversation
        match_check = supabase.table("conversations").select("id") \
            .or_(f"and(user1_id.eq.{user_id},user2_id.eq.{liked_id}),and(user1_id.eq.{liked_id},user2_id.eq.{user_id})") \
            .execute()
        if not match_check.data:
            supabase.table("conversations").insert({
                "user1_id":   min(user_id, liked_id),
                "user2_id":   max(user_id, liked_id),
                "phase":      "anon",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

        # Send mutual match notification to both users
        for (recipient, actor) in ([] if _already_liked else [(user_id, liked_id), (liked_id, user_id)]):
            actor_name_res = supabase.table("users").select("first_name, last_name").eq("id", actor).execute().data
            actor_name = f"{actor_name_res[0]['first_name']} {actor_name_res[0]['last_name']}" if actor_name_res else "Someone"
            supabase.table("notifications").insert({
                "recipient_id": recipient,
                "actor_id":     actor,
                "type":         "match",
                "title":        f"It's a Match! You and {actor_name} liked each other 🎉",
                "is_read":      False,
                "created_at":   datetime.now(timezone.utc).isoformat(),
            }).execute()

    _auto_credit_streak(user_id)
    liked_user = supabase.table("users").select("first_name, last_name").eq("id", liked_id).execute().data
    liked_name = f"{liked_user[0]['first_name']} {liked_user[0]['last_name']}" if liked_user else "them"
    return ok({"is_mutual": is_mutual, "mutual": is_mutual, "name": liked_name}, "Match!" if is_mutual else "Like sent")


@app.route("/api/conversations", methods=["POST"])
@app.route("/api/conversations/open", methods=["POST"])
@require_auth
def open_conversation(user_id):
    """
    POST /api/conversations/open
    Body: { other_user_id }
    Creates a conversation between the two users if it doesn't exist.
    This allows chatting even before a mutual like — the like is recorded automatically.
    """
    other_id = (request.json or {}).get("other_user_id")
    if not other_id:
        return err("other_user_id required")
    if not _valid_id(other_id) or other_id == user_id:
        return err("Invalid other_user_id")
    if _connection_ended(user_id, other_id):
        return err("This connection has ended", 403)
    _g = _gate(user_id, other_id)
    if _g:
        return _g

    # Verify the other user exists
    other = supabase.table("users").select("id, first_name, last_name").eq("id", other_id).execute().data
    if not other:
        return err("User not found", 404)

    # Auto-like so mutual match can form
    supabase.table("likes").upsert({
        "liker_id":  user_id,
        "liked_id":  other_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="liker_id,liked_id").execute()

    # Check if conversation already exists (in either direction)
    existing = supabase.table("conversations").select("*")         .or_(f"and(user1_id.eq.{user_id},user2_id.eq.{other_id}),and(user1_id.eq.{other_id},user2_id.eq.{user_id})")         .execute()

    if existing.data:
        convo = existing.data[0]
    else:
        if not supabase.table("likes").select("liker_id").eq("liker_id", other_id).eq("liked_id", user_id).execute().data:
            return err("Like sent. You can start chatting once they like you back", 403)
        _pok, _pwhy = _form_pairing(user_id, other_id)
        if not _pok:
            return err("This person is no longer available", 409) if _pwhy == "unavailable" else err("Matching is temporarily unavailable", 503)
        _cancel_other_opportunities(user_id, other_id)
        # Create the conversation directly
        convo = supabase.table("conversations").insert({
            "user1_id":   min(user_id, other_id),
            "user2_id":   max(user_id, other_id),
            "phase":      "anon",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute().data[0]

        # Also create a match record
        supabase.table("matches").upsert({
            "user1_id":   min(user_id, other_id),
            "user2_id":   max(user_id, other_id),
            "phase":      "anon",
            "matched_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user1_id,user2_id").execute()

    other_user = other[0]
    return ok({
        "id":            convo["id"],
        "other_user_id": other_id,
        "other_name":    f"{other_user['first_name']} {other_user['last_name']}",
        "phase":         convo.get("phase", "anon"),
        "last_message":  "",
        "unread_count":  0,
    }, "Conversation ready")


# ─────────────────────────────────────────────
# CHAT / MESSAGES
# ─────────────────────────────────────────────

@app.route("/api/conversations", methods=["GET"])
@require_auth
def get_conversations(user_id):
    """GET /api/conversations — all conversations for the current user."""
    res = supabase.table("conversations").select("*") \
        .or_(f"user1_id.eq.{user_id},user2_id.eq.{user_id}") \
        .order("updated_at", desc=True) \
        .execute()

    convos = []
    _ended = _ended_with(user_id)
    for c in res.data:
        other_id = c["user2_id"] if c["user1_id"] == user_id else c["user1_id"]
        if other_id in _ended:          # ended matches disappear from the chat list
            continue
        other = supabase.table("users").select("first_name, last_name").eq("id", other_id).single().execute().data
        last_msg = supabase.table("messages").select("content, created_at").eq("conversation_id", c["id"]) \
            .order("created_at", desc=True).limit(1).execute().data
        unread = supabase.table("messages").select("id", count="exact").eq("conversation_id", c["id"]) \
            .eq("is_read", False).neq("sender_id", user_id).execute()

        convos.append({
            "id":           c["id"],
            "other_user_id": other_id,
            "other_name":   f"{other['first_name']} {other['last_name']}",
            "phase":        c.get("phase", "anon"),
            "last_message": last_msg[0]["content"] if last_msg else "",
            "last_time":    last_msg[0]["created_at"] if last_msg else c["created_at"],
            "unread_count": unread.count or 0,
        })

    return ok(convos)


@app.route("/api/conversations/<convo_id>/messages", methods=["GET"])
@require_auth
def get_messages(user_id, convo_id):
    """GET /api/conversations/<id>/messages?limit=50&before=<timestamp>"""
    if not _user_in_convo(user_id, convo_id):
        return err("Unauthorized", 403)
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        return err("limit must be a number")
    before = request.args.get("before")

    q = supabase.table("messages").select("*").eq("conversation_id", convo_id).order("created_at").limit(limit)
    if before:
        q = q.lt("created_at", before)

    msgs = q.execute().data

    # Mark as read
    supabase.table("messages").update({"is_read": True}) \
        .eq("conversation_id", convo_id).neq("sender_id", user_id).execute()

    return ok(msgs)


@app.route("/api/conversations/<convo_id>/messages", methods=["POST"])
@require_auth
@_rate_limit(60, 60)
def send_message(user_id, convo_id):
    """
    POST /api/conversations/<id>/messages
    Body: { content, type }   type: text | voice_note | emoji
    """
    d = request.get_json(silent=True) or {}
    content = d.get("content", "")
    content = content.strip() if isinstance(content, str) else ""
    if not content:
        return err("Message content required")
    if d.get("type", "text") not in ("text", "voice_note", "emoji"):
        return err("Invalid message type")

    # Verify user is part of conversation
    if len(content) > 2000:
        return err("Message too long (max 2000 characters)")
    _rows = supabase.table("conversations").select("*").eq("id", convo_id).execute().data
    if not _rows or user_id not in (_rows[0]["user1_id"], _rows[0]["user2_id"]):
        return err("Unauthorized", 403)
    if _connection_ended(_rows[0]["user1_id"], _rows[0]["user2_id"]):
        return err("This connection has ended", 403)

    msg = supabase.table("messages").insert({
        "conversation_id": convo_id,
        "sender_id":       user_id,
        "content":         content,
        "type":            d.get("type", "text"),
        "is_read":         False,
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]

    # Update conversation last-activity
    supabase.table("conversations").update({
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", convo_id).execute()

    # Notify the other person - one unread "message" notification per sender, not one per message
    try:
        _other = _rows[0]["user2_id"] if _rows[0]["user1_id"] == user_id else _rows[0]["user1_id"]
        if not supabase.table("notifications").select("id").eq("recipient_id", _other).eq("actor_id", user_id) \
                .eq("type", "message").eq("is_read", False).execute().data:
            _notify(_other, user_id, "message", f"{_first_name(user_id)} sent you a message 💬")
    except Exception:
        app.logger.exception("message notification failed")
    _closeness_touch(user_id, _rows[0]["user2_id"] if _rows[0]["user1_id"] == user_id else _rows[0]["user1_id"])

    _auto_credit_streak(user_id)
    return ok(msg, "Message sent", 201)


# ─────────────────────────────────────────────
# REVEAL PHASE MANAGEMENT
# ─────────────────────────────────────────────

PHASE_ORDER = ["anon", "voice", "photo"]

@app.route("/api/conversations/<convo_id>/reveal", methods=["POST"])
@require_auth
def request_reveal(user_id, convo_id):
    """
    POST /api/conversations/<id>/reveal
    Body: { action: "request" | "accept" | "decline" }
    The other person is notified of the request; the requester is notified of the answer.
    """
    d = request.get_json(silent=True) or {}
    action = d.get("action", "request")
    if action not in ("request", "accept", "decline"):
        return err("Invalid action")

    rows = supabase.table("conversations").select("*").eq("id", convo_id).execute().data
    if not rows or user_id not in (rows[0]["user1_id"], rows[0]["user2_id"]):
        return err("Unauthorized", 403)          # same answer for "missing" and "not yours"
    convo = rows[0]
    other_id = convo["user2_id"] if convo["user1_id"] == user_id else convo["user1_id"]
    if _connection_ended(convo["user1_id"], convo["user2_id"]):
        return err("This connection has ended", 403)

    current_phase = convo.get("phase", "anon")
    if current_phase not in PHASE_ORDER:
        return err("This chat is not at a reveal step", 409)
    idx = PHASE_ORDER.index(current_phase)

    _pend = supabase.table("reveal_requests").select("requester_id").eq("conversation_id", convo_id) \
        .eq("current_phase", current_phase).eq("status", "pending").execute().data or []
    mine_pending = any(r["requester_id"] == user_id for r in _pend)
    theirs_pending = any(r["requester_id"] != user_id for r in _pend)

    if action == "request":
        if idx + 1 >= len(PHASE_ORDER):
            return err("This chat is already at the final reveal step", 409)
        if mine_pending:
            return err("Request already sent. Waiting for their response", 409)
        if theirs_pending:
            return err("They already sent you a request. Accept it instead", 409)
        supabase.table("reveal_requests").upsert({
            "conversation_id": convo_id,
            "requester_id":    user_id,
            "current_phase":   current_phase,
            "status":          "pending",
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }, on_conflict="conversation_id,requester_id,current_phase").execute()
        _notify(other_id, user_id, "phase_reveal_request", f"{_first_name(user_id)} wants to take your chat to the next reveal step",
                f"Current step: {current_phase}")
        return ok(message=f"Reveal request sent for {current_phase} phase")

    if not theirs_pending:
        return err(f"No pending request from the other person to {action}", 409)

    if action == "accept":
        next_phase = PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else current_phase
        supabase.table("conversations").update({"phase": next_phase}).eq("id", convo_id).execute()
        supabase.table("matches").update({"phase": next_phase}) \
            .or_(f"and(user1_id.eq.{convo['user1_id']},user2_id.eq.{convo['user2_id']})") \
            .execute()
        supabase.table("reveal_requests").update({"status": "accepted"}) \
            .eq("conversation_id", convo_id).eq("current_phase", current_phase).execute()
        _mark_read(user_id, other_id, "phase_reveal_request")
        _notify(other_id, user_id, "phase_reveal_accepted", f"{_first_name(user_id)} accepted your reveal request",
                f"Your chat moved to: {next_phase}")
        _closeness_touch(user_id, other_id, force=True)
        return ok({"new_phase": next_phase}, f"Phase advanced to {next_phase}")

    supabase.table("reveal_requests").update({"status": "declined"}) \
        .eq("conversation_id", convo_id).eq("current_phase", current_phase).execute()
    _mark_read(user_id, other_id, "phase_reveal_request")
    _notify(other_id, user_id, "phase_reveal_declined", f"{_first_name(user_id)} declined your reveal request")
    return ok(message="Reveal declined")


# ─────────────────────────────────────────────
# VIDEO DATES
# ─────────────────────────────────────────────

@app.route("/api/video/schedule", methods=["POST"])
@require_auth
def schedule_video(user_id):
    d = request.get_json(silent=True) or {}
    gid = d.get("guest_id")
    if not _valid_id(gid) or gid == user_id or not _is_active_match(user_id, gid):
        return err("You can only schedule calls with an active mutual match", 403)
    when = d.get("scheduled_at")
    if when in (None, ""):
        when = None
    else:
        try:
            when_dt = _ts(when)
        except (ValueError, TypeError):
            return err("Invalid scheduled_at (use ISO 8601)")
        if when_dt <= _now():
            return err("Pick a time in the future")
        when = when_dt.isoformat()
    convo_ref = d.get("conversation_id") or None
    if convo_ref is not None:       # must be THIS pair's conversation, never someone else's
        _cv = supabase.table("conversations").select("user1_id, user2_id").eq("id", convo_ref).execute().data \
            if _valid_id(convo_ref) else None
        if not _cv or {_cv[0]["user1_id"], _cv[0]["user2_id"]} != {user_id, gid}:
            return err("Invalid conversation_id")
    for _h, _g in ((user_id, gid), (gid, user_id)):      # one unanswered invitation per pair
        if supabase.table("video_dates").select("id").eq("host_id", _h).eq("guest_id", _g) \
                .in_("status", ["invited", "scheduled"]).execute().data:
            return err("A video invitation between you two is already waiting for a reply", 409)
    vd = supabase.table("video_dates").insert({
        "host_id":         user_id,
        "guest_id":        gid,
        "conversation_id": convo_ref,
        "scheduled_at":    when,
        "status":          "invited",
        "room_id":         "room_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)),
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]
    _notify(gid, user_id, "video_invite", f"{_first_name(user_id)} invited you to a video date 📹",
            ("Proposed time: " + when_dt.strftime("%d %b %Y %H:%M UTC") + ". " if when else "") + "Open the Hub to accept or decline")
    return ok(vd, "Video date scheduled", 201)


@app.route("/api/video/<room_id>/token", methods=["GET"])
@require_auth
def get_video_token(user_id, room_id):
    """Generate a token for video room access (integrate with Twilio Video / Daily.co in production)."""
    return err("Deprecated: use /api/video/room/<room_id>/join", 410)


# ─────────────────────────────────────────────
# FEEDBACK
# ─────────────────────────────────────────────

@app.route("/api/feedback", methods=["POST"])
@require_auth
def submit_feedback(user_id):
    d = request.json or {}
    rating = d.get("rating", 3)
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return err("rating must be a whole number from 1 to 5")
    supabase.table("feedback").insert({
        "user_id":    user_id,
        "match_id":   d.get("match_id"),
        "rating":     rating,
        "comment":    str(d.get("comment", ""))[:2000],
        "type":       d.get("type", "match_quality"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return ok(message="Feedback submitted — thank you!")


# ─────────────────────────────────────────────
# AI COACH SUGGESTIONS
# ─────────────────────────────────────────────

AI_COACH_POOL = [
    "Ask about their favorite childhood memory 🧡",
    "Share a fun fact about yourself 🌟",
    "Suggest a virtual game to play together 🎮",
    "Ask what they're currently reading 📖",
    "Share your travel bucket list ✈️",
    "Ask about their go-to comfort food 🍜",
    "Share the last thing that made you laugh 😄",
    "Ask what skill they wish they had 🎨",
    "Discuss a documentary you both might enjoy 🎬",
    "Ask about their weekend routine ☕",
]

@app.route("/api/coach/suggestions", methods=["GET"])
@require_auth
def coach_suggestions(user_id):
    convo_id = request.args.get("conversation_id")
    # In production: analyze message history and generate context-aware suggestions with an LLM
    suggestions = random.sample(AI_COACH_POOL, 3)
    return ok(suggestions)


# ─────────────────────────────────────────────
# SERVE FRONTEND
# ─────────────────────────────────────────────

@app.route("/")
@app.route("/soulsync-ai.html")
def serve_frontend():
    """Serve the SoulSync AI frontend HTML file from the same directory as app.py."""
    directory = os.path.dirname(os.path.abspath(__file__))
    # Try common filenames
    for fname in ["soulsync-ai-fixed.html", "soulsync-ai.html", "index.html"]:
        if os.path.exists(os.path.join(directory, fname)):
            return send_from_directory(directory, fname)
    return "<h2>soulsync-ai.html not found — make sure it's in the same folder as app.py</h2>", 404


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    try:
        supabase.table("users").select("id").limit(1).execute()
        db_ok = True
    except Exception:
        db_ok = False
    return ok({"status": "ok", "database": "connected" if db_ok else "error", "version": "1.0.0"})



@app.route("/api/notifications", methods=["GET"])
@require_auth
def get_notifications(user_id):
    """GET /api/notifications — all unread notifications for the user."""
    res = supabase.table("notifications") \
        .select("*") \
        .eq("recipient_id", user_id) \
        .order("created_at", desc=True) \
        .limit(30) \
        .execute()
    return ok(res.data)


@app.route("/api/notifications/read", methods=["POST"])
@require_auth
def mark_notifications_read(user_id):
    """POST /api/notifications/read — mark all as read."""
    supabase.table("notifications").update({"is_read": True}).eq("recipient_id", user_id).execute()
    return ok(message="Marked as read")


@app.route("/api/notifications/likes", methods=["GET"])
@require_auth
def get_like_notifications(user_id):
    res = supabase.table("notifications") \
        .select("*, actor:actor_id(id, first_name, liked_at:created_at)") \
        .eq("recipient_id", user_id) \
        .eq("type", "like") \
        .eq("is_read", False) \
        .order("created_at", desc=True) \
        .execute()
    # Flatten to the shape the frontend expects: { id, name, liked_at }
    data = [{"id": n["actor_id"], "name": n["title"].replace(" liked your profile!",""),
             "liked_at": n["created_at"]} for n in res.data]
    return ok(data)


# ─────────────────────────────────────────────
# SAFETY: block, report, admin dashboard, password reset
# ─────────────────────────────────────────────
REPORT_REASONS = ("harassment", "fake_profile", "inappropriate", "scam", "underage", "other")


def _do_block(user_id, other_id):
    """Block = permanent end of the connection (same hiding rules as a breakup) + a record for moderators."""
    if _get_match(user_id, other_id) or _have_conversation(user_id, other_id):
        if not _connection_ended(user_id, other_id):
            end_connection.__wrapped__(user_id, other_id)
    else:
        a, b = _pair(user_id, other_id)
        supabase.table("matches").upsert({"user1_id": a, "user2_id": b, "status": "ended", "ended_at": _iso(), "ended_by": user_id},
                                         on_conflict="user1_id,user2_id").execute()
    try:
        supabase.table("blocks").upsert({"blocker_id": user_id, "blocked_id": other_id, "created_at": _iso()}, on_conflict="blocker_id,blocked_id").execute()
    except Exception:
        app.logger.exception("block record failed")


@app.route("/api/block/<other_id>", methods=["POST"])
@require_auth
def block_user(user_id, other_id):
    if not _valid_id(other_id) or other_id == user_id:
        return err("Invalid id")
    _do_block(user_id, other_id)
    return ok(message="Blocked. You will never see each other again")


@app.route("/api/report", methods=["POST"])
@_rate_limit(10, 60)
@require_auth
def report_user(user_id):
    d = request.get_json(silent=True) or {}
    rid = d.get("reported_id")
    if not _valid_id(rid) or rid == user_id:
        return err("Invalid id")
    reason = d.get("reason") if d.get("reason") in REPORT_REASONS else "other"
    supabase.table("reports").insert({"reporter_id": user_id, "reported_id": rid, "reason": reason,
                                      "details": str(d.get("details") or "")[:1000], "status": "open", "created_at": _iso()}).execute()
    if d.get("block"):
        _do_block(user_id, rid)
    return ok(message="Thank you. Our team will review this report", status=201)


def _count(table, **eq):
    q = supabase.table(table).select("id", count="exact")
    for k, v in eq.items():
        q = q.eq(k, v)
    r = q.execute()
    return getattr(r, "count", None) if getattr(r, "count", None) is not None else len(r.data or [])


@app.route("/api/admin/stats", methods=["GET"])
@require_auth
def admin_stats(user_id):
    if not _is_admin(user_id):
        return err("Admins only", 403)
    def safe(f):
        try:
            return f()
        except Exception:
            return None
    return ok({"users": safe(lambda: _count("users")), "onboarded": safe(lambda: _count("users", onboarded=True)),
               "screening_complete": safe(lambda: _count("users", screening_complete=True)),
               "matches": safe(lambda: _count("matches")), "matches_ended": safe(lambda: _count("matches", status="ended")),
               "messages": safe(lambda: _count("messages")), "video_dates_accepted": safe(lambda: _count("video_dates", status="accepted")),
               "real_dates_confirmed": safe(lambda: _count("real_dates", status="confirmed")),
               "engaged_users": len(_engaged_ids()), "reports_open": safe(lambda: _count("reports", status="open"))})


@app.route("/api/admin/reports", methods=["GET"])
@require_auth
def admin_reports(user_id):
    if not _is_admin(user_id):
        return err("Admins only", 403)
    rows = supabase.table("reports").select("*").eq("status", "open").order("created_at", desc=True).limit(100).execute().data or []
    for r in rows:
        r["reporter_name"], r["reported_name"] = _first_name(r["reporter_id"]), _first_name(r["reported_id"])
    return ok(rows)


@app.route("/api/admin/reports/<rid>/resolve", methods=["POST"])
@require_auth
def admin_resolve_report(user_id, rid):
    if not _is_admin(user_id):
        return err("Admins only", 403)
    if not _valid_id(rid):
        return err("Invalid id")
    supabase.table("reports").update({"status": "resolved", "resolved_by": user_id, "resolved_at": _iso()}).eq("id", rid).execute()
    return ok(message="Resolved")


ADMIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>SoulSync admin</title>
<style>body{font-family:system-ui;max-width:820px;margin:24px auto;padding:0 14px;color:#222}.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.c{border:1px solid #ddd;border-radius:12px;padding:12px}.c b{display:block;font-size:24px;color:#e11d74}.r{border-top:1px solid #eee;padding:10px 0}button{padding:6px 12px;border-radius:20px;border:1px solid #ccc;cursor:pointer}</style>
<h2>SoulSync admin</h2><div class=g id=s></div><h3>Open reports</h3><div id=r>Loading…</div>
<script>
const T=localStorage.getItem('ss_token'),H={Authorization:'Bearer '+T};
async function j(u,m){const r=await fetch(u,{method:m||'GET',headers:H});return r.json()}
(async()=>{const s=await j('/api/admin/stats');if(!s.success){document.body.innerHTML='<p>Admins only. Log in to the app first with an admin account (ADMIN_USER_IDS).</p>';return}
const L={users:'Users',onboarded:'Profiles done',screening_complete:'Screening done',matches:'Matches',matches_ended:'Ended',messages:'Messages',video_dates_accepted:'Video dates',real_dates_confirmed:'Real dates',engaged_users:'Engaged people',reports_open:'Open reports'};
document.getElementById('s').innerHTML=Object.keys(L).map(k=>`<div class=c><b>${s.data[k]==null?'–':s.data[k]}</b>${L[k]}</div>`).join('');load()})();
async function load(){const d=await j('/api/admin/reports'),el=document.getElementById('r');el.textContent='';
if(!d.data||!d.data.length){el.textContent='No open reports.';return}
d.data.forEach(x=>{const div=document.createElement('div');div.className='r';div.textContent=x.reporter_name+' reported '+x.reported_name+' ('+x.reason+') '+(x.details||'')+' — '+String(x.created_at).slice(0,10)+' ';
const b=document.createElement('button');b.textContent='Resolve';b.onclick=async()=>{await j('/api/admin/reports/'+x.id+'/resolve','POST');load()};div.appendChild(b);el.appendChild(div)})}
</script>"""


@app.route("/admin")
def admin_page():
    return ADMIN_PAGE, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


def _send_email(to, subject, body):
    host = os.environ.get("SMTP_HOST", "")
    if not host:
        app.logger.warning("SMTP not configured. Email to %s: %s | %s", to, subject, body)
        return
    import smtplib
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "no-reply@soulsync.local")), to, subject
    m.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        s.send_message(m)


def _pw_fp(user):
    import hashlib
    return hashlib.sha256(str(user.get("password_hash") or "").encode()).hexdigest()[:16]


@app.route("/api/auth/forgot", methods=["POST"])
@_rate_limit(5, 60)
def forgot_password():
    email = str((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    try:
        u = supabase.table("users").select("id, email, password_hash").eq("email", email).execute().data if email else []
        if u:
            tok = jwt.encode({"sub": u[0]["id"], "purpose": "reset", "fp": _pw_fp(u[0]),
                              "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}, JWT_SECRET, algorithm="HS256")
            link = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/") + "/?reset=" + tok
            _send_email(u[0]["email"], "Reset your SoulSync password", f"Open this link within 30 minutes to choose a new password:\n{link}\n\nIf you did not ask for this, ignore this email.")
    except Exception:
        app.logger.exception("password reset email failed")
    return ok(message="If that email is registered, a reset link has been sent")      # same answer either way


@app.route("/api/auth/reset", methods=["POST"])
@_rate_limit(10, 60)
def reset_password():
    d = request.get_json(silent=True) or {}
    try:
        p = jwt.decode(str(d.get("token") or ""), JWT_SECRET, algorithms=["HS256"])
        assert p.get("purpose") == "reset"
    except Exception:
        return err("This reset link is invalid or has expired", 400)
    pw = str(d.get("password") or "")
    if len(pw) < 8:
        return err("Password must be at least 8 characters")
    u = supabase.table("users").select("id, password_hash").eq("id", p["sub"]).execute().data
    if not u or _pw_fp(u[0]) != p.get("fp"):          # single use: the link dies once the password changes
        return err("This reset link is invalid or has expired", 400)
    supabase.table("users").update({"password_hash": hash_password(pw)}).eq("id", p["sub"]).execute()
    return ok(message="Password updated. You can sign in now")


# ─────────────────────────────────────────────
# REAL-LIFE DATES (both must confirm; feedback is private to the person who gave it)
# ─────────────────────────────────────────────
DATE_KINDS = ("coffee", "meal", "walk", "activity", "other")


def _date_view(r, me):
    mine = r.get("fb1") if me == r.get("user1_id") else r.get("fb2")
    return {"id": r["id"], "date_on": r.get("date_on"), "kind": r.get("kind"), "status": r.get("status"),
            "by_me": r.get("proposer_id") == me, "my_feedback": mine}


def _real_dates(a, b, status=None):
    u1, u2 = _pair(a, b)
    q = supabase.table("real_dates").select("*").eq("user1_id", u1).eq("user2_id", u2)
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


@app.route("/api/dates", methods=["POST"])
@require_auth
def date_log(user_id):
    d = request.get_json(silent=True) or {}
    oid = d.get("other_id")
    if not _valid_id(oid) or oid == user_id or not _is_active_match(user_id, oid):
        return err("You can only log a date with an active match", 403)
    try:
        day = datetime.strptime(str(d.get("date_on")), "%Y-%m-%d").date()
    except ValueError:
        return err("Pick the date (YYYY-MM-DD)")
    today = _now().date()
    if day > today or (today - day).days > 60:
        return err("Pick a date from the last 60 days")
    kind = d.get("kind") if d.get("kind") in DATE_KINDS else "other"
    rows = _real_dates(user_id, oid)
    if any(r["status"] == "pending" for r in rows):
        return err("A date is already waiting for confirmation", 409)
    if any(r["status"] in ("pending", "confirmed") and str(r.get("date_on"))[:10] == day.isoformat() for r in rows):
        return err("That date is already logged", 409)
    u1, u2 = _pair(user_id, oid)
    row = supabase.table("real_dates").insert({"user1_id": u1, "user2_id": u2, "proposer_id": user_id, "date_on": day.isoformat(),
                                               "kind": kind, "status": "pending", "created_at": _iso()}).execute().data[0]
    _notify(oid, user_id, "date_request", f"{_first_name(user_id)} says you two went on a date 📍", "Open your closeness page to confirm it")
    return ok(_date_view(row, user_id), "Sent for confirmation", 201)


@app.route("/api/dates/<did>/respond", methods=["POST"])
@require_auth
def date_respond(user_id, did):
    action = (request.get_json(silent=True) or {}).get("action")
    r = supabase.table("real_dates").select("*").eq("id", did).execute().data if _valid_id(did) else None
    if not r or user_id not in (r[0]["user1_id"], r[0]["user2_id"]):
        return err("Not found", 404)
    r = r[0]
    if r["proposer_id"] == user_id or r["status"] != "pending" or action not in ("confirm", "deny"):
        return err("Cannot respond", 403)
    new = "confirmed" if action == "confirm" else "denied"
    supabase.table("real_dates").update({"status": new}).eq("id", did).eq("status", "pending").execute()
    other = r["proposer_id"]
    _mark_read(user_id, other, "date_request")
    if new == "confirmed":
        _closeness_touch(user_id, other, force=True)
        _notify(other, user_id, "date_confirmed", f"📍 {_first_name(user_id)} confirmed your date", "Tell us how it went. Your answer stays private")
    else:
        _notify(other, user_id, "date_denied", f"{_first_name(user_id)} says that date did not happen")
    return ok({"status": new})


@app.route("/api/dates/<did>/feedback", methods=["POST"])
@require_auth
def date_feedback(user_id, did):
    ans = (request.get_json(silent=True) or {}).get("answer")
    r = supabase.table("real_dates").select("*").eq("id", did).execute().data if _valid_id(did) else None
    if not r or user_id not in (r[0]["user1_id"], r[0]["user2_id"]):
        return err("Not found", 404)
    r = r[0]
    if r["status"] != "confirmed" or ans not in ("great", "not_sure", "uncomfortable"):
        return err("Feedback is only for confirmed dates (great, not_sure or uncomfortable)", 409)
    supabase.table("real_dates").update({"fb1" if user_id == r["user1_id"] else "fb2": ans}).eq("id", did).execute()
    return ok({"safety": ans == "uncomfortable"}, "Saved privately")      # never shown or sent to the other person


# ─────────────────────────────────────────────
# CLOSENESS  (0-100% bond between a matched pair; at 100% both are asked about engagement)
# ─────────────────────────────────────────────
# Server-computed from real activity only, so it cannot be faked from the browser:
#   conversation days (two-way, max 4 pts/day) + accepted reveal steps (10 each, max 3) + photo reveal (8)
#   + accepted/completed video dates (8 each, max 3) + accepted dating request (15).
# "More time" stores a negative offset so the score drops to exactly 50 and must be earned again.

def _closeness_row(a, b):
    u1, u2 = _pair(a, b)
    r = supabase.table("closeness").select("*").eq("user1_id", u1).eq("user2_id", u2).execute().data
    return r[0] if r else None


def _closeness_raw(a, b):
    u1, u2 = _pair(a, b)
    cids = []
    for x, y in ((u1, u2), (u2, u1)):
        cids += [c["id"] for c in (supabase.table("conversations").select("id").eq("user1_id", x).eq("user2_id", y).execute().data or [])]
    per_day = {}
    for cid in cids:
        rows = supabase.table("messages").select("sender_id, created_at").eq("conversation_id", cid) \
            .order("created_at", desc=True).limit(1000).execute().data or []
        for r in rows:
            day = str(r.get("created_at") or "")[:10]
            if day:
                per_day.setdefault(day, {})
                per_day[day][r["sender_id"]] = per_day[day].get(r["sender_id"], 0) + 1
    chat = sum(min(4, 1 + sum(w.values()) // 6) for w in per_day.values() if a in w and b in w)
    phases = sum(len(supabase.table("reveal_requests").select("id").eq("conversation_id", cid).eq("status", "accepted").execute().data or []) for cid in cids)
    photo = 8 if b in _revealed_with(a) else 0
    vids = 0
    for h, g in ((a, b), (b, a)):
        vids += len(supabase.table("video_dates").select("id").eq("host_id", h).eq("guest_id", g).in_("status", ["accepted", "completed"]).execute().data or [])
    try:
        ndates = len(_real_dates(a, b, "confirmed"))
    except Exception:
        ndates = 0
    dating = 15 if supabase.table("dating_requests").select("id").eq("user1_id", u1).eq("user2_id", u2).eq("status", "accepted").execute().data else 0
    parts = [{"label": "Conversations", "points": chat}, {"label": "Reveal steps", "points": min(phases, 3) * 10},
             {"label": "Photos revealed", "points": photo}, {"label": "Video dates", "points": min(vids, 3) * 8},
             {"label": "Dating", "points": dating},
             {"label": f"Real dates ({ndates})", "points": min(ndates, 3) * 12}]
    return parts, sum(p["points"] for p in parts)


def _closeness_touch(a, b, force=False):
    """Recompute and store the bond; notifies both people once when it reaches 100%. Never raises."""
    try:
        if not _is_active_match(a, b):
            return None
        row = _closeness_row(a, b)
        if row and row.get("status") == "engaged":
            return dict(row, parts=[])
        if row and not force and row.get("updated_at") and (_now() - _ts(row["updated_at"])).total_seconds() < 60:
            return dict(row, parts=[])
        parts, raw = _closeness_raw(a, b)
        offset = int((row or {}).get("score_offset") or 0)
        pct = max(0, min(100, raw + offset))
        hist = list((row or {}).get("history") or [])
        if not hist or hist[-1].get("p") != pct:
            hist = (hist + [{"t": _iso(), "p": pct}])[-60:]
        status = (row or {}).get("status") or "building"
        u1, u2 = _pair(a, b)
        rec = {"user1_id": u1, "user2_id": u2, "score_offset": offset, "pct": pct, "history": hist, "status": status, "updated_at": _iso()}
        prev = int((row or {}).get("pct") or 0)
        crossed = [t for t in (25, 50, 75) if prev < t <= pct and pct < 100]
        newly = pct >= 100 and status == "building"
        if newly:
            rec.update(status="asking", asked_at=_iso(), answer1=None, answer2=None)
        supabase.table("closeness").upsert(rec, on_conflict="user1_id,user2_id").execute()
        if crossed and not newly:
            for me, other in ((a, b), (b, a)):
                _notify(me, other, "closeness_milestone", f"💞 Your closeness with {_first_name(other)} reached {crossed[-1]}%", "Keep going. At 100% you will both be asked about the next step")
        if newly:
            for me, other in ((a, b), (b, a)):
                _notify(me, other, "closeness_100", f"💞 You and {_first_name(other)} reached 100% closeness",
                        "Is it a match? Are you ready to get engaged? Tap to answer")
        return dict(row or {}, **rec, parts=parts)
    except Exception:
        app.logger.exception("closeness update failed")
        return None


def _engaged_ids() -> set:
    try:
        rows = supabase.table("closeness").select("user1_id, user2_id").eq("status", "engaged").execute().data or []
        return {r["user1_id"] for r in rows} | {r["user2_id"] for r in rows}
    except Exception:
        return set()


def _engage(a, b):
    """Both said yes: lock the pair, close every other connection of both people for good, and tell those contacts."""
    u1, u2 = _pair(a, b)
    supabase.table("closeness").update({"status": "engaged", "updated_at": _iso()}).eq("user1_id", u1).eq("user2_id", u2).execute()
    for x, y in ((a, b), (b, a)):
        others = set()
        for tbl in ("matches", "conversations"):
            for col, oc in (("user1_id", "user2_id"), ("user2_id", "user1_id")):
                others |= {r[oc] for r in (supabase.table(tbl).select("user1_id, user2_id").eq(col, x).execute().data or [])}
        others -= {x, y}
        for o in others:
            if _connection_ended(x, o):
                continue
            p1, p2 = _pair(x, o)
            supabase.table("matches").upsert({"user1_id": p1, "user2_id": p2, "status": "ended", "ended_at": _iso(), "ended_by": x},
                                             on_conflict="user1_id,user2_id").execute()
            supabase.table("dating_requests").update({"status": "cancelled", "updated_at": _iso()}).eq("user1_id", p1).eq("user2_id", p2) \
                .in_("status", ["pending", "accepted"]).execute()
            supabase.table("video_dates").update({"status": "cancelled"}).in_("status", ["scheduled", "accepted", "invited"]) \
                .or_(f"and(host_id.eq.{x},guest_id.eq.{o}),and(host_id.eq.{o},guest_id.eq.{x})").execute()
            _notify(o, x, "contact_engaged", f"{_first_name(x)} is now engaged and is no longer available",
                    "This connection has been closed permanently")
    for me, other in ((a, b), (b, a)):
        _notify(me, other, "engaged", f"💍 You and {_first_name(other)} are engaged! Congratulations", "")


@app.route("/api/closeness/<other_id>", methods=["GET"])
@require_auth
def closeness_get(user_id, other_id):
    if not _valid_id(other_id) or other_id == user_id or not _is_active_match(user_id, other_id):
        return err("No active connection", 404)
    row = _closeness_touch(user_id, other_id, force=True)
    if not row:
        return err("Closeness is not available right now", 503)
    u1, _u2 = _pair(user_id, other_id)
    mine = row.get("answer1") if user_id == u1 else row.get("answer2")
    try:
        dates = [_date_view(r, user_id) for r in _real_dates(user_id, other_id)][-10:]
    except Exception:
        dates = []
    return ok({"dates": dates, "percent": row.get("pct", 0), "status": row.get("status", "building"), "history": row.get("history") or [],
               "parts": row.get("parts") or [], "my_answer": mine, "name": _first_name(other_id)})


@app.route("/api/closeness/<other_id>/answer", methods=["POST"])
@require_auth
def closeness_answer(user_id, other_id):
    ans = (request.get_json(silent=True) or {}).get("answer")
    if ans not in ("yes", "more_time", "break_up"):
        return err("answer must be yes, more_time or break_up")
    if not _valid_id(other_id) or other_id == user_id or not _is_active_match(user_id, other_id):
        return err("No active connection", 404)
    row = _closeness_row(user_id, other_id)
    if not row or row.get("status") != "asking":
        return err("This question is not open yet", 409)
    u1, u2 = _pair(user_id, other_id)
    mine_k, other_k = ("answer1", "answer2") if user_id == u1 else ("answer2", "answer1")
    where = lambda q: q.eq("user1_id", u1).eq("user2_id", u2)
    if ans == "break_up":
        resp = end_connection.__wrapped__(user_id, other_id)      # same rules as a normal breakup: chat closes, never shown to each other again
        where(supabase.table("closeness").update({"status": "building", "answer1": None, "answer2": None})).execute()
        return resp if isinstance(resp, tuple) and resp[1] >= 400 else ok({"result": "ended"}, "Connection ended")
    if ans == "more_time":
        _parts, raw = _closeness_raw(user_id, other_id)
        hist = (list(row.get("history") or []) + [{"t": _iso(), "p": 50}])[-60:]
        where(supabase.table("closeness").update({"status": "building", "answer1": None, "answer2": None, "score_offset": 50 - raw,
                                                  "pct": 50, "history": hist, "updated_at": _iso()})).execute()
        _notify(other_id, user_id, "closeness_more_time", f"{_first_name(user_id)} would like a little more time to get to know you",
                "Your closeness is back to 50%. Keep talking, it will grow again")
        return ok({"result": "more_time", "percent": 50}, "Closeness set to 50%")
    if row.get(mine_k) == "yes":
        return ok({"result": "waiting"})
    where(supabase.table("closeness").update({mine_k: "yes"})).execute()
    if row.get(other_k) == "yes":
        _engage(user_id, other_id)
        return ok({"result": "engaged"}, "You are engaged")
    return ok({"result": "waiting"}, "Waiting for their answer")


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# PHOTO REVEAL SYSTEM
# Flow: A requests -> B is notified -> B accepts -> both can see each other's photos.
# PHOTO_PRIVACY=all  (default): photos are hidden from everyone until a reveal is accepted.
# PHOTO_PRIVACY=anon: photos are hidden only for users who turned on anonymous_mode.
# ─────────────────────────────────────────────

PHOTO_PRIVACY = os.environ.get("PHOTO_PRIVACY", "all").lower()
REVEAL_COOLDOWN = timedelta(hours=24)


def _parse_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _revealed_with(user_id) -> set:
    """Ids of users who have an accepted photo reveal with user_id."""
    out = set()
    for col, other in (("user1_id", "user2_id"), ("user2_id", "user1_id")):
        rows = supabase.table("photo_reveal_permissions").select("user1_id, user2_id, enabled") \
            .eq(col, user_id).execute().data or []
        out.update(r[other] for r in rows if r.get("enabled", True))
    return out


def _photos_hidden(viewer_id, target_id, target_anon, revealed) -> bool:
    """True if viewer must not see target's photos."""
    if viewer_id == target_id or target_id in revealed:
        return False
    if PHOTO_PRIVACY == "anon" and not target_anon:
        return False
    return True


def _ended_with(user_id) -> set:
    """Ids of people whose connection with user_id has ended."""
    out = set()
    for col, other in (("user1_id", "user2_id"), ("user2_id", "user1_id")):
        rows = supabase.table("matches").select("user1_id, user2_id, status").eq(col, user_id).execute().data or []
        out.update(r[other] for r in rows if r.get("status") == "ended")
    return out


def _have_conversation(a, b) -> bool:
    for x, y in ((a, b), (b, a)):
        if supabase.table("conversations").select("id").eq("user1_id", x).eq("user2_id", y).execute().data:
            return True
    return False


def _reveal_state(me, other) -> dict:
    """none | pending_sent | pending_received | declined | revealed (+ request_id / retry_after)."""
    if other in _revealed_with(me):
        return {"state": "revealed"}
    sent = supabase.table("photo_reveal_requests").select("*").eq("sender_id", me).eq("receiver_id", other).execute().data or []
    got = supabase.table("photo_reveal_requests").select("*").eq("sender_id", other).eq("receiver_id", me).execute().data or []
    got_pending = sorted([r for r in got if r.get("status") == "pending"], key=lambda r: _parse_ts(r.get("created_at")), reverse=True)
    if got_pending:
        return {"state": "pending_received", "request_id": got_pending[0]["id"]}
    sent.sort(key=lambda r: _parse_ts(r.get("created_at")), reverse=True)
    if sent and sent[0].get("status") == "pending":
        return {"state": "pending_sent", "request_id": sent[0]["id"]}
    if sent and sent[0].get("status") == "declined":
        retry = _parse_ts(sent[0].get("created_at")) + REVEAL_COOLDOWN
        if datetime.now(timezone.utc) < retry:
            return {"state": "declined", "retry_after": retry.isoformat()}
    return {"state": "none"}


@app.route("/api/reveal/request", methods=["POST"])
@require_auth
def send_reveal_request(user_id):
    """POST /api/reveal/request  { receiver_id } — ask the other person to reveal photos to each other."""
    receiver_id = (request.get_json(silent=True) or {}).get("receiver_id")
    if not _valid_id(receiver_id) or receiver_id == user_id:
        return err("Open a chat with someone first (receiver_id missing or invalid)")
    if _connection_ended(user_id, receiver_id):
        return err("This connection has ended", 403)
    if not _have_conversation(user_id, receiver_id):   # same rule as chatting: you must already share a conversation
        return err("Start a conversation first, then request a photo reveal", 403)

    st = _reveal_state(user_id, receiver_id)
    if st["state"] == "revealed":
        return err("Photos are already revealed", 409)
    if st["state"] == "pending_sent":
        return err("Request already sent. Waiting for their response", 409)
    if st["state"] == "pending_received":
        return err("They already sent you a request. Accept it instead", 409)
    if st["state"] == "declined":
        return err("They declined. You can request again after 24 hours", 429)

    row = supabase.table("photo_reveal_requests").insert({
        "sender_id": user_id, "receiver_id": receiver_id, "status": "pending", "created_at": _iso(),
    }).execute().data
    rid = row[0]["id"] if row else None

    s = supabase.table("users").select("first_name").eq("id", user_id).execute().data
    name = s[0].get("first_name", "Someone") if s else "Someone"
    _notify(receiver_id, user_id, "reveal_request", f"{name} wants to reveal photos",
            "Open your chat to accept or decline")
    return ok({"request_id": rid, "state": "pending_sent"}, "Reveal request sent", 201)


@app.route("/api/reveal/respond", methods=["POST"])
@require_auth
def respond_reveal_request(user_id):
    """POST /api/reveal/respond  { request_id, action: accept|decline } — receiver only."""
    body = request.get_json(silent=True) or {}
    request_id, action = body.get("request_id"), body.get("action")
    if not request_id or action not in ("accept", "decline"):
        return err("request_id and action (accept/decline) required")

    rows = supabase.table("photo_reveal_requests").select("*").eq("id", request_id).execute().data
    if not rows:
        return err("Request not found", 404)
    req = rows[0]
    if req["receiver_id"] != user_id:
        return err("Not authorised", 403)
    if req.get("status") != "pending":
        return err("This request was already answered", 409)
    sender_id = req["sender_id"]
    if _connection_ended(user_id, sender_id):
        return err("This connection has ended", 403)

    now = _iso()
    supabase.table("photo_reveal_requests").update({
        "status": "accepted" if action == "accept" else "declined",
        "accepted_at": now if action == "accept" else None,
    }).eq("id", request_id).execute()

    # the request notification is handled, stop showing it as unread
    try:
        supabase.table("notifications").update({"is_read": True}).eq("recipient_id", user_id) \
            .eq("actor_id", sender_id).eq("type", "reveal_request").execute()
    except Exception:
        pass

    me = supabase.table("users").select("first_name").eq("id", user_id).execute().data
    my_name = me[0].get("first_name", "Someone") if me else "Someone"
    if action == "accept":
        u1, u2 = sorted([sender_id, user_id])
        supabase.table("photo_reveal_permissions").upsert(
            {"user1_id": u1, "user2_id": u2, "enabled": True}, on_conflict="user1_id,user2_id").execute()
        _closeness_touch(user_id, sender_id, force=True)
        _notify(sender_id, user_id, "reveal_accepted", f"{my_name} accepted your photo reveal",
                "You can now see each other's photos")
        return ok({"state": "revealed"}, "Photos revealed!")
    _notify(sender_id, user_id, "reveal_declined", f"{my_name} declined your photo reveal")
    return ok({"state": "declined"}, "Request declined")


@app.route("/api/reveal/status/<other_user_id>", methods=["GET"])
@require_auth
def reveal_status(user_id, other_user_id):
    """GET /api/reveal/status/<id> — where the photo reveal with this person stands."""
    if not _valid_id(other_user_id) or other_user_id == user_id:
        return err("Invalid user id")
    return ok(_reveal_state(user_id, other_user_id))


@app.route("/api/reveal/check/<other_user_id>", methods=["GET"])
@require_auth
def check_reveal_permission(user_id, other_user_id):
    """GET /api/reveal/check/<id> — check mutual reveal permission."""
    if not _valid_id(other_user_id):
        return err("Invalid user id")
    return ok({"reveal_allowed": other_user_id in _revealed_with(user_id)})


# ═════════════════════════════════════════════
# HEAVEN UPGRADE (additive) — see migration_heaven.sql
# ═════════════════════════════════════════════
import json
import urllib.request
import urllib.error

ADMIN_USER_IDS = {x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}
ELIGIBILITY_MODE = os.environ.get("ELIGIBILITY_MODE", "straight")      # "straight" | "open"
DAILY_API_KEY = os.environ.get("DAILY_API_KEY", "")                    # video provider (Daily.co)
DAILY_DOMAIN = os.environ.get("DAILY_DOMAIN", "")                      # your Daily subdomain, e.g. "mycompany" for mycompany.daily.co
VERIFY_BUCKET = os.environ.get("VERIFY_BUCKET", "verification")        # PRIVATE storage bucket
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
IMG_TYPES = {"image/jpeg", "image/png", "image/webp"}
AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4"}

ANIMAL_AVATARS = ["fox", "wolf", "owl", "panda", "cat", "dog", "rabbit", "bear", "deer", "dolphin", "lion", "penguin"]
VOICE_PHRASES = ["blue river morning", "silver lantern garden", "quiet mountain sunrise",
                 "golden harvest moon", "gentle thunder rain", "paper boat journey"]
STREAK_MILESTONES = [3, 7, 14, 21, 30, 50, 60, 100, 150, 200, 365, 500, 730, 1000]

# Documented, configurable compatibility weights (must sum to 100)
COMPAT_WEIGHTS = {"goals": 30, "values": 25, "interests": 20, "personality": 15, "expectations": 10}


def _now():
    return datetime.now(timezone.utc)


def _ts(v):
    t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso():
    return _now().isoformat()


def _is_admin(uid):
    return uid in ADMIN_USER_IDS


def _admin_only(f):
    @wraps(f)
    def w(user_id, *a, **k):
        if not _is_admin(user_id):
            return err("Forbidden", 403)
        return f(user_id, *a, **k)
    return w


def _pair(a, b):
    return min(a, b), max(a, b)


def _get_match(a, b):
    u1, u2 = _pair(a, b)
    r = supabase.table("matches").select("*").eq("user1_id", u1).eq("user2_id", u2).execute().data
    return r[0] if r else None


def _is_active_match(a, b):
    m = _get_match(a, b)
    return bool(m) and m.get("status", "active") != "ended"


def _notify(recipient, actor, ntype, title, body=""):
    try:
        supabase.table("notifications").insert({"recipient_id": recipient, "actor_id": actor, "type": ntype,
                                                "title": title, "body": body, "is_read": False,
                                                "created_at": _iso()}).execute()
    except Exception:
        app.logger.exception("NOTIFICATION INSERT FAILED (%s -> %s, type=%s)", actor, recipient, ntype)


def _first_name(uid) -> str:
    try:
        r = supabase.table("users").select("first_name").eq("id", uid).execute().data
        return (r[0].get("first_name") if r else None) or "Someone"
    except Exception:
        return "Someone"


def _mark_read(recipient, actor, *ntypes):
    """Mark the recipient's unread notifications of these types from `actor` as read (a handled request must stop showing as pending)."""
    try:
        supabase.table("notifications").update({"is_read": True}).eq("recipient_id", recipient) \
            .eq("actor_id", actor).in_("type", list(ntypes)).eq("is_read", False).execute()
    except Exception:
        pass


def _eligible(a: dict, b: dict) -> bool:
    """Mutual audience eligibility from voluntary gender/interested_in (configurable via ELIGIBILITY_MODE)."""
    if ELIGIBILITY_MODE == "open":
        return True
    return _individual_eligible(a.get("gender"), a.get("interested_in"))[0] and _individual_eligible(b.get("gender"), b.get("interested_in"))[0] \
        and _norm_gender(a.get("gender")) != _norm_gender(b.get("gender"))


import base64, difflib

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "gemini-3.1-flash-lite")
PHOTO_CHALLENGES = ["Hold up two fingers", "Give a thumbs up", "Touch your left ear", "Hold up three fingers"]
_NUMW = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
         "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _http_json(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=body, headers={**headers, "User-Agent": "soulsync/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ai_check_selfie(data, ctype, challenge):
    """(True|False|None, reason). None = service unavailable (nothing is stored, user can retry). Fails closed."""
    if not GEMINI_API_KEY:
        return None, "AI verification is not configured on the server"
    prompt = ("You are a liveness checker for a dating app. Look at this selfie. Do NOT identify the person. "
              "Reply with ONLY a JSON object: {\"one_real_person\": bool (exactly one real human, face fully visible, "
              "no mask/sunglasses), \"live_photo\": bool (a genuine camera selfie, NOT a photo of a screen or printed photo, "
              "not a cartoon, not AI-generated or stock), \"challenge_done\": bool (the person is clearly doing this: "
              + json.dumps(challenge) + "), \"reason\": short user-friendly sentence explaining what to fix if anything is false}")
    body = json.dumps({"contents": [{"parts": [
        {"inline_data": {"mime_type": ctype, "data": base64.b64encode(data).decode()}},
        {"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 1024}}).encode()
    try:
        r = _http_json("https://generativelanguage.googleapis.com/v1beta/models/" + VERIFY_MODEL + ":generateContent",
                       {"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"}, body)
        txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
        j = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        app.logger.exception("selfie AI check failed")
        return None, "Verification service is unavailable — please try again shortly"
    passed = bool(j.get("one_real_person") and j.get("live_photo") and j.get("challenge_done"))
    return passed, ("" if passed else str(j.get("reason") or "We couldn't verify this selfie — retake it in good light")[:300])


def _spoken_norm(t):
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    t = " ".join(_NUMW.get(w, w) for w in t.split())
    return re.sub(r"(?<=\d) (?=\d)", "", t)


def _transcribe(data, ctype):
    ext = ctype.split("/")[1]
    b = "----ss" + "".join(random.choices(string.ascii_letters, k=16))
    def part(n, v):
        return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, n, v)).encode()
    body = part("model", "whisper-large-v3-turbo") + part("language", "en") + \
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.%s\"\r\nContent-Type: %s\r\n\r\n" % (b, ext, ctype)).encode() + \
        data + ("\r\n--%s--\r\n" % b).encode()
    r = _http_json("https://api.groq.com/openai/v1/audio/transcriptions",
                   {"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "multipart/form-data; boundary=" + b}, body)
    return r.get("text", "")


def _ai_check_voice(data, ctype, phrase):
    """(True|False|None, reason). Speech-to-text, then the spoken words and the random number must match the phrase."""
    if not GROQ_API_KEY:
        return None, "Voice verification is not configured on the server"
    try:
        said = _spoken_norm(_transcribe(data, ctype))
    except Exception:
        app.logger.exception("voice transcription failed")
        return None, "Verification service is unavailable — please try again shortly"
    want = _spoken_norm(phrase)
    app.logger.warning("VOICE CHECK heard=%r want=%r", said, want)
    digits = re.sub(r"\D", "", want)
    passed = bool(said) and digits in said.replace(" ", "") and difflib.SequenceMatcher(None, want, said).ratio() >= 0.85
    return passed, ("" if passed else "We couldn't hear the exact phrase and number — speak clearly and try again")


def _ready(u):
    """Matches are only usable after profile screening + AI photo verification + AI voice verification + the 21-day challenge and movie analysis."""
    return bool(u and u.get("screening_complete") and u.get("verification_status") == "verified" and u.get("voice_verified") and u.get("challenge_complete"))


def _upload_private(path, data, ctype):
    supabase.storage.from_(VERIFY_BUCKET).upload(path, data, {"content-type": ctype})


def _read_file(field, allowed):
    f = request.files.get(field)
    if not f:
        return None, "No file uploaded"
    if f.mimetype not in allowed:
        return None, "Unsupported file type"
    data = f.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return None, "File too large (max 5 MB)"
    if not data:
        return None, "Empty file"
    return (data, f.mimetype), None


# ── Animal avatars ───────────────────────────
@app.route("/api/avatars", methods=["GET"])
def list_avatars():
    return ok({"avatars": ANIMAL_AVATARS})


@app.route("/api/profile/avatar", methods=["PATCH"])
@require_auth
def set_avatar(user_id):
    a = (request.json or {}).get("avatar")
    if a not in ANIMAL_AVATARS:
        return err("Invalid avatar")
    supabase.table("users").update({"avatar": a}).eq("id", user_id).execute()
    return ok({"avatar": a}, "Avatar saved")


# ── Streaks (server-side, one credit per UTC day, only for real activity) ──
def _did_meaningful_activity_today(uid):
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    m = supabase.table("messages").select("id").eq("sender_id", uid).gte("created_at", start).limit(1).execute().data
    if m:
        return True
    l = supabase.table("likes").select("id").eq("liker_id", uid).gte("created_at", start).limit(1).execute().data
    return bool(l)


@app.route("/api/streak", methods=["GET"])
@require_auth
def get_streak(user_id):
    r = supabase.table("user_streaks").select("*").eq("user_id", user_id).execute().data
    s = r[0] if r else {"current_streak": 0, "longest_streak": 0, "last_active_date": None}
    cur = s["current_streak"]
    # streak is broken if last credit was before yesterday
    if s["last_active_date"] and s["last_active_date"] < (_now().date() - timedelta(days=1)).isoformat():
        cur = 0
    nxt = next((m for m in STREAK_MILESTONES if m > cur), None)
    if nxt is None:
        nxt = (cur // 500 + 1) * 500   # past the last listed milestone: keep going in steps of 500
    return ok({"current_streak": cur, "longest_streak": s["longest_streak"],
               "last_active_date": s["last_active_date"], "next_milestone": nxt,
               "milestones": STREAK_MILESTONES})


def _auto_credit_streak(user_id):
    """Called after a real like or message: credits today's streak once. Never breaks the caller."""
    try:
        today = _now().date()
        r = supabase.table("user_streaks").select("*").eq("user_id", user_id).execute().data
        if r and r[0]["last_active_date"] == today.isoformat():
            return
        prev = r[0] if r else None
        cur = (prev["current_streak"] + 1) if prev and prev["last_active_date"] == (today - timedelta(days=1)).isoformat() else 1
        supabase.table("user_streaks").upsert({"user_id": user_id, "current_streak": cur,
            "longest_streak": max(cur, prev["longest_streak"] if prev else 0),
            "last_active_date": today.isoformat(), "updated_at": _iso()}, on_conflict="user_id").execute()
        if cur in STREAK_MILESTONES:
            _notify(user_id, user_id, "streak", f"🔥 {cur}-day streak milestone!")
    except Exception:
        app.logger.warning("auto streak credit failed", exc_info=True)


@app.route("/api/streak/checkin", methods=["POST"])
@require_auth
def streak_checkin(user_id):
    if not _did_meaningful_activity_today(user_id):
        return err("Send a message or like a profile first to earn today's streak", 409)
    today = _now().date()
    r = supabase.table("user_streaks").select("*").eq("user_id", user_id).execute().data
    if r and r[0]["last_active_date"] == today.isoformat():
        return ok({"current_streak": r[0]["current_streak"], "already_counted": True}, "Already counted today")
    prev = r[0] if r else None
    cur = (prev["current_streak"] + 1) if prev and prev["last_active_date"] == (today - timedelta(days=1)).isoformat() else 1
    row = {"user_id": user_id, "current_streak": cur,
           "longest_streak": max(cur, prev["longest_streak"] if prev else 0),
           "last_active_date": today.isoformat(), "updated_at": _iso()}
    supabase.table("user_streaks").upsert(row, on_conflict="user_id").execute()
    if cur in STREAK_MILESTONES:
        _notify(user_id, user_id, "streak", f"🔥 {cur}-day streak milestone!")
    return ok({"current_streak": cur, "milestone_hit": cur in STREAK_MILESTONES}, "Streak updated")


# ── Verification (photo + voice). Only admins can approve. ──
@app.route("/api/verification/status", methods=["GET"])
@require_auth
def verification_status(user_id):
    rows = supabase.table("verification_requests").select("id, kind, status, submitted_at, reviewed_at, reject_reason") \
        .eq("user_id", user_id).order("submitted_at", desc=True).limit(10).execute().data
    u = supabase.table("users").select("verification_status, voice_verified").eq("id", user_id).execute().data
    return ok({"status": (u[0].get("verification_status") if u else None) or "unverified", "voice_verified": bool(u and u[0].get("voice_verified")), "attempts": rows})


@app.route("/api/verification/photo", methods=["POST"])
@require_auth
@_rate_limit(8, 60)
def submit_photo_verification(user_id):
    """multipart: selfie=<file>, challenge=<text the user performed>. Goes to PENDING for human review."""
    pend = supabase.table("verification_requests").select("id").eq("user_id", user_id).eq("kind", "photo").eq("status", "pending").execute().data
    if pend:
        pass  # AI verifies instantly, so a stale pending row must not block new attempts
    chal = (request.form.get("challenge") or "").strip()
    if chal not in PHOTO_CHALLENGES:
        return err("Invalid challenge")
    if (supabase.table("users").select("verification_status").eq("id", user_id).execute().data or [{}])[0].get("verification_status") == "verified":
        return err("Your photo is already verified", 409)
    _since = (_now() - timedelta(hours=1)).isoformat()
    if len(supabase.table("verification_requests").select("id").eq("user_id", user_id).eq("kind", "photo").gte("submitted_at", _since).execute().data) >= 5:
        return err("Too many attempts — try again in an hour", 429)
    res, e = _read_file("selfie", IMG_TYPES)
    _sp = (False, "") if e else _ai_check_selfie(res[0], res[1], chal)
    if _sp[0] is None:
        return err(_sp[1], 503)
    if e:
        return err(e)
    data, ctype = res
    path = f"{user_id}/photo_{int(_now().timestamp())}.{ctype.split('/')[1]}"
    try:
        _upload_private(path, data, ctype)
    except Exception:
        return err("Upload failed — check the private storage bucket is configured", 500)
    row = supabase.table("verification_requests").insert({
"user_id": user_id, "kind": "photo", "status": "verified" if _sp[0] else "rejected", "storage_path": path, "reject_reason": "" if _sp[0] else _sp[1][:300], "reviewed_at": _iso(),
        "challenge": (request.form.get("challenge") or "")[:200], "submitted_at": _iso()}).execute().data[0]
    supabase.table("users").update({"verification_status": "verified" if _sp[0] else "rejected", "is_verified": bool(_sp[0])}).eq("id", user_id).execute()
    return ok({"id": row["id"], "status": "verified" if _sp[0] else "rejected", "reason": _sp[1]}, "Photo verified" if _sp[0] else "Photo check failed", 201)


@app.route("/api/verification/voice/prompt", methods=["GET"])
@require_auth
def voice_prompt(user_id):
    phrase = " ".join(random.sample(VOICE_PHRASES, 1)) + " " + str(random.randint(100, 999))
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=24))
    supabase.table("voice_prompts").insert({"user_id": user_id, "nonce": nonce, "phrase": phrase,
                                            "expires_at": (_now() + timedelta(minutes=5)).isoformat(),
                                            "used": False, "created_at": _iso()}).execute()
    return ok({"nonce": nonce, "phrase": phrase, "expires_in": 300})


@app.route("/api/verification/voice", methods=["POST"])
@require_auth
@_rate_limit(8, 60)
def submit_voice_verification(user_id):
    """multipart: audio=<file>, nonce=<from prompt>. Single-use nonce prevents replay. Goes to PENDING review."""
    nonce = request.form.get("nonce", "")
    p = supabase.table("voice_prompts").select("*").eq("user_id", user_id).eq("nonce", nonce).execute().data
    if not p or p[0]["used"] or _ts(p[0]["expires_at"]) < _now():
        return err("Prompt expired or already used — request a new one", 409)
    if (supabase.table("users").select("voice_verified").eq("id", user_id).execute().data or [{}])[0].get("voice_verified"):
        return err("Your voice is already verified", 409)
    _since = (_now() - timedelta(hours=1)).isoformat()
    if len(supabase.table("verification_requests").select("id").eq("user_id", user_id).eq("kind", "voice").gte("submitted_at", _since).execute().data) >= 5:
        return err("Too many attempts — try again in an hour", 429)
    res, e = _read_file("audio", AUDIO_TYPES)
    _vp = (False, "") if e else _ai_check_voice(res[0], res[1], p[0]["phrase"])
    if _vp[0] is None:
        return err(_vp[1], 503)
    if e:
        return err(e)
    data, ctype = res
    # burn the nonce first so a replay can't reuse it
    supabase.table("voice_prompts").update({"used": True}).eq("id", p[0]["id"]).execute()
    path = f"{user_id}/voice_{int(_now().timestamp())}.{ctype.split('/')[1]}"
    try:
        _upload_private(path, data, ctype)
    except Exception:
        return err("Upload failed — check the private storage bucket is configured", 500)
    row = supabase.table("verification_requests").insert({
        "user_id": user_id, "kind": "voice", "status": "verified" if _vp[0] else "rejected", "storage_path": path, "reject_reason": "" if _vp[0] else _vp[1][:300], "reviewed_at": _iso(),
                "challenge": p[0]["phrase"], "submitted_at": _iso()}).execute().data[0]
    if _vp[0]:
        supabase.table("users").update({"voice_verified": True}).eq("id", user_id).execute()
    return ok({"id": row["id"], "status": "verified" if _vp[0] else "rejected", "reason": _vp[1]}, "Voice verified" if _vp[0] else "Voice check failed", 201)


@app.route("/api/admin/verifications", methods=["GET"])
@require_auth
@_admin_only
def admin_list_verifications(user_id):
    rows = supabase.table("verification_requests").select("*").eq("status", "pending").order("submitted_at").limit(50).execute().data
    for r in rows:   # short-lived signed URLs, never public
        try:
            r["signed_url"] = supabase.storage.from_(VERIFY_BUCKET).create_signed_url(r["storage_path"], 300)["signedURL"]
        except Exception:
            r["signed_url"] = None
    return ok(rows)


@app.route("/api/admin/verifications/<vid>/review", methods=["POST"])
@require_auth
@_admin_only
def admin_review_verification(user_id, vid):
    d = request.json or {}
    decision = d.get("decision")
    if decision not in ("verified", "rejected"):
        return err("decision must be verified or rejected")
    r = supabase.table("verification_requests").select("*").eq("id", vid).execute().data
    if not r or r[0]["status"] != "pending":
        return err("Not found or already reviewed", 404)
    supabase.table("verification_requests").update({"status": decision, "reviewed_by": user_id, "reviewed_at": _iso(),
                                                    "reject_reason": (d.get("reason") or "")[:300]}).eq("id", vid).execute()
    uid = r[0]["user_id"]
    if decision == "verified" and r[0]["kind"] == "photo":
        supabase.table("users").update({"verification_status": "verified", "is_verified": True}).eq("id", uid).execute()
    elif decision == "rejected" and r[0]["kind"] == "photo":
        supabase.table("users").update({"verification_status": "rejected", "is_verified": False}).eq("id", uid).execute()
    _notify(uid, user_id, "verification", f"Your {r[0]['kind']} verification was {decision}")
    return ok(message=f"Marked {decision}")


# ── Ideal partner / manifestation (extends AI Twin data; never creates fake profiles) ──
def _csv_list(v, n=10):
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip()[:40] for x in (v or []) if str(x).strip()][:n]


@app.route("/api/ideal-partner", methods=["PUT"])
@require_auth
def save_ideal_partner(user_id):
    d = request.json or {}
    row = {"user_id": user_id, "interests": _csv_list(d.get("interests")), "values": _csv_list(d.get("values")),
           "traits": _csv_list(d.get("traits")), "goals": _csv_list(d.get("goals")),
           "expectations": (d.get("expectations") or "")[:300], "updated_at": _iso()}
    row["ai_description"] = (
        "AI-generated description (not a real person): someone who shares your interest in "
        f"{', '.join(row['interests']) or 'meaningful activities'}, values {', '.join(row['values']) or 'honesty'}, "
        f"tends to be {', '.join(row['traits']) or 'kind'}, and is working toward {', '.join(row['goals']) or 'a shared future'}. "
        "This is used only to improve recommendations; it is not a prediction.")
    supabase.table("ideal_partner_prefs").upsert(row, on_conflict="user_id").execute()
    return ok(row, "Saved")


@app.route("/api/ideal-partner", methods=["GET"])
@require_auth
def get_ideal_partner(user_id):
    r = supabase.table("ideal_partner_prefs").select("*").eq("user_id", user_id).execute().data
    return ok(r[0] if r else None)


def _jaccard(a, b):
    a, b = {x.lower() for x in a or []}, {x.lower() for x in b or []}
    return (len(a & b) / len(a | b)) if (a | b) else 0.0


@app.route("/api/compat/<target_id>", methods=["GET"])
@require_auth
def compat_explain(user_id, target_id):
    """Transparent, server-computed score. Only for mutually eligible users."""
    if not _valid_id(target_id):
        return err("Not available", 404)
    _gg = None if (target_id == user_id or _have_conversation(user_id, target_id)) else _gate(user_id, target_id)
    if _gg:
        return err("Not available", 404)
    us = supabase.table("users").select("id, gender, interested_in").in_("id", [user_id, target_id]).execute().data
    by = {u["id"]: u for u in us}
    if user_id not in by or target_id not in by or not _eligible(by[user_id], by[target_id]):
        return err("Not available", 404)
    ip = {r["user_id"]: r for r in supabase.table("ideal_partner_prefs").select("*").in_("user_id", [user_id, target_id]).execute().data}
    me, th = ip.get(user_id, {}), ip.get(target_id, {})
    parts = {"goals": _jaccard(me.get("goals"), th.get("goals")),
             "values": _jaccard(me.get("values"), th.get("values")),
             "interests": _jaccard(me.get("interests"), th.get("interests")),
             "personality": _jaccard(me.get("traits"), th.get("traits")),
             "expectations": 1.0 if me.get("expectations") and th.get("expectations") else 0.0}
    breakdown = {k: round(parts[k] * COMPAT_WEIGHTS[k], 1) for k in parts}
    common_goals = sorted({g.lower() for g in me.get("goals", [])} & {g.lower() for g in th.get("goals", [])})
    return ok({"score": round(sum(breakdown.values())), "weights": COMPAT_WEIGHTS, "breakdown": breakdown,
               "common_goals": common_goals})


# ── Dating flow (mutual consent, no duplicates) ──
@app.route("/api/dating/request", methods=["POST"])
@require_auth
def dating_request(user_id):
    tid = (request.get_json(silent=True) or {}).get("target_id")
    if not _valid_id(tid) or tid == user_id or not _is_active_match(user_id, tid):
        return err("You can only send this to an active mutual match", 403)
    u1, u2 = _pair(user_id, tid)
    ex = supabase.table("dating_requests").select("id, status").eq("user1_id", u1).eq("user2_id", u2).in_("status", ["pending", "accepted"]).execute().data
    if ex:
        return err(f"A request is already {ex[0]['status']}", 409)
    try:
        r = supabase.table("dating_requests").insert({"user1_id": u1, "user2_id": u2, "sender_id": user_id,
                                                      "status": "pending", "created_at": _iso()}).execute().data[0]
    except Exception:       # the one_open_dating_request unique index caught a double-click / race
        return err("A request is already pending", 409)
    _notify(tid, user_id, "dating_request", f"{_first_name(user_id)} wants to start dating you 💕", "Open the Hub to accept or decline")
    return ok(r, "Request sent", 201)


@app.route("/api/dating/<rid>/respond", methods=["POST"])
@require_auth
def dating_respond(user_id, rid):
    action = (request.json or {}).get("action")      # accept | reject | cancel
    r = supabase.table("dating_requests").select("*").eq("id", rid).execute().data
    if not r or user_id not in (r[0]["user1_id"], r[0]["user2_id"]):
        return err("Not found", 404)
    r = r[0]
    if r["status"] != "pending" and not (action == "cancel" and r["status"] == "accepted"):
        return err("Request is not pending", 409)
    if action in ("accept", "reject") and r["sender_id"] == user_id:
        return err("You can't respond to your own request", 403)
    if action == "cancel" and r["status"] == "pending" and r["sender_id"] != user_id:
        return err("Only the sender can cancel", 403)
    new = {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}.get(action)
    if not new:
        return err("action must be accept, reject or cancel")
    done = supabase.table("dating_requests").update({"status": new, "updated_at": _iso()}).eq("id", rid).eq("status", r["status"]).execute().data
    if not done:
        return err("This request was just updated - refresh and try again", 409)
    other = r["user2_id"] if user_id == r["user1_id"] else r["user1_id"]
    _mark_read(r["user1_id"] if r["sender_id"] != r["user1_id"] else r["user2_id"], r["sender_id"], "dating_request")   # the receiver's "wants to date you" is handled
    _closeness_touch(user_id, other, force=True)
    _notify(other, user_id, "dating_" + new, f"{_first_name(user_id)} {dict(accepted='accepted', rejected='declined', cancelled='cancelled')[new]} the dating request")
    return ok({"status": new})


@app.route("/api/dating", methods=["GET"])
@require_auth
def dating_list(user_id):
    rows = supabase.table("dating_requests").select("*").or_(f"user1_id.eq.{user_id},user2_id.eq.{user_id}").order("created_at", desc=True).execute().data
    return ok(rows)


# ── End connection + private breakup feedback ──
@app.route("/api/connections/<other_id>/end", methods=["POST"])
@require_auth
def end_connection(user_id, other_id):
    if not _valid_id(other_id):
        return err("Invalid id")
    m = _get_match(user_id, other_id)
    if not m and _have_conversation(user_id, other_id):       # older chats may have no match row
        _a, _b = _pair(user_id, other_id)
        supabase.table("matches").upsert({"user1_id": _a, "user2_id": _b, "status": "active"}, on_conflict="user1_id,user2_id").execute()
        m = _get_match(user_id, other_id)
    if not m or m.get("status") == "ended":
        return err("No active connection", 404)
    d = request.get_json(silent=True) or {}
    u1, u2 = _pair(user_id, other_id)
    _end_pairing(user_id, other_id, user_id)     # frees both people only after the explicit end/unmatch
    # photos lock again and any open photo-reveal request is closed
    supabase.table("photo_reveal_permissions").update({"enabled": False}).eq("user1_id", u1).eq("user2_id", u2).execute()
    supabase.table("photo_reveal_requests").update({"status": "cancelled"}).eq("status", "pending") \
        .in_("sender_id", [user_id, other_id]).in_("receiver_id", [user_id, other_id]).execute()
    supabase.table("matches").update({"status": "ended", "ended_at": _iso(), "ended_by": user_id}).eq("user1_id", u1).eq("user2_id", u2).execute()
    supabase.table("dating_requests").update({"status": "cancelled", "updated_at": _iso()}).eq("user1_id", u1).eq("user2_id", u2).in_("status", ["pending", "accepted"]).execute()
    supabase.table("video_dates").update({"status": "cancelled"}).in_("status", ["scheduled", "accepted", "invited"]) \
        .or_(f"and(host_id.eq.{user_id},guest_id.eq.{other_id}),and(host_id.eq.{other_id},guest_id.eq.{user_id})").execute()
    if isinstance(d.get("feedback"), dict):
        fb = d["feedback"]
        supabase.table("breakup_feedback").insert({
            "author_id": user_id, "about_user_id": other_id, "reason": (fb.get("reason") or "")[:60],
            "comment": (fb.get("comment") or "")[:1000], "created_at": _iso()}).execute()
    for _a, _b in ((user_id, other_id), (other_id, user_id)):      # nothing left to accept / decline
        _mark_read(_a, _b, "dating_request", "video_invite", "phase_reveal_request", "reveal_request")
    _notify(other_id, user_id, "connection_ended", "A connection has ended")   # no feedback content is ever sent
    return ok(message="Connection ended")


@app.route("/api/breakup-feedback", methods=["GET"])
@require_auth
def my_breakup_feedback(user_id):
    """Authors can only read their own; feedback is never shown to the other person."""
    return ok(supabase.table("breakup_feedback").select("id, reason, comment, created_at").eq("author_id", user_id).execute().data)


# ── Contests ──
@app.route("/api/contests", methods=["GET"])
@require_auth
def contests_list(user_id):
    cs = supabase.table("contests").select("id, title, rules, starts_at, ends_at, status").order("starts_at", desc=True).execute().data
    mine = {e["contest_id"] for e in supabase.table("contest_entries").select("contest_id").eq("user_id", user_id).execute().data}
    for c in cs:
        c["joined"] = c["id"] in mine
        c["open"] = _ts(c["starts_at"]) <= _now() < _ts(c["ends_at"]) and c["status"] == "open"
    return ok(cs)


@app.route("/api/contests/<cid>/join", methods=["POST"])
@require_auth
def contest_join(user_id, cid):
    c = supabase.table("contests").select("*").eq("id", cid).execute().data
    if not c or not (_ts(c[0]["starts_at"]) <= _now() < _ts(c[0]["ends_at"])) or c[0]["status"] != "open":
        return err("Contest is not open", 409)
    try:   # UNIQUE(contest_id, user_id) in the DB blocks duplicates even under race conditions
        supabase.table("contest_entries").insert({"contest_id": cid, "user_id": user_id, "joined_at": _iso()}).execute()
    except Exception:
        return err("Already entered", 409)
    return ok(message="Entered", status=201)


@app.route("/api/contests/<cid>/results", methods=["GET"])
@require_auth
def contest_results(user_id, cid):
    c = supabase.table("contests").select("ends_at, status").eq("id", cid).execute().data
    if not c or _now() < _ts(c[0]["ends_at"]) or c[0]["status"] != "completed":
        return err("Results not available yet", 409)
    rows = supabase.table("contest_entries").select("user_id, rank, score").eq("contest_id", cid).order("rank").execute().data
    return ok([{"rank": r["rank"], "score": r["score"], "is_you": r["user_id"] == user_id} for r in rows])   # no identities


@app.route("/api/admin/contests", methods=["POST"])
@require_auth
@_admin_only
def admin_create_contest(user_id):
    d = request.json or {}
    if not all(d.get(k) for k in ("title", "starts_at", "ends_at")):
        return err("title, starts_at, ends_at required")
    try:
        if _ts(d["ends_at"]) <= _ts(d["starts_at"]):
            return err("ends_at must be after starts_at")
    except ValueError:
        return err("Invalid date format (use ISO 8601)")
    r = supabase.table("contests").insert({"title": d["title"][:120], "rules": (d.get("rules") or "")[:2000],
                                           "starts_at": d["starts_at"], "ends_at": d["ends_at"], "status": "open",
                                           "created_by": user_id}).execute().data[0]
    return ok(r, "Created", 201)


@app.route("/api/admin/contests/<cid>/results", methods=["POST"])
@require_auth
@_admin_only
def admin_set_results(user_id, cid):
    """Body: {results:[{user_id, rank, score}]}. Only admin, only after the contest ends."""
    c = supabase.table("contests").select("ends_at").eq("id", cid).execute().data
    if not c or _now() < _ts(c[0]["ends_at"]):
        return err("Contest has not ended", 409)
    for r in (request.json or {}).get("results", []):
        supabase.table("contest_entries").update({"rank": r.get("rank"), "score": r.get("score")}).eq("contest_id", cid).eq("user_id", r.get("user_id")).execute()
    supabase.table("contests").update({"status": "completed"}).eq("id", cid).execute()
    return ok(message="Results saved")


# ── Reveal after contest (explicit mutual consent; never returns contact details) ──
@app.route("/api/contests/<cid>/reveal", methods=["POST"])
@require_auth
def contest_reveal_request(user_id, cid):
    tid = (request.get_json(silent=True) or {}).get("target_id")
    c = supabase.table("contests").select("ends_at, status").eq("id", cid).execute().data
    if not c or c[0]["status"] != "completed" or tid == user_id or not _valid_id(tid):
        return err("Reveal is not available for this contest", 409)
    ents = {e["user_id"] for e in supabase.table("contest_entries").select("user_id").eq("contest_id", cid).in_("user_id", [user_id, tid]).execute().data}
    if ents != {user_id, tid}:
        return err("Both users must have entered this contest", 403)
    ex = supabase.table("contest_reveals").select("id").eq("contest_id", cid).eq("sender_id", user_id).eq("receiver_id", tid).in_("status", ["pending", "accepted"]).execute().data
    if ex:
        return err("Already requested", 409)
    r = supabase.table("contest_reveals").insert({"contest_id": cid, "sender_id": user_id, "receiver_id": tid,
                                                  "status": "pending", "created_at": _iso()}).execute().data[0]
    _notify(tid, user_id, "contest_reveal_request", f"{_first_name(user_id)} sent you a contest reveal request")
    return ok(r, "Reveal request sent", 201)


@app.route("/api/contests/reveal/<rid>/respond", methods=["POST"])
@require_auth
def contest_reveal_respond(user_id, rid):
    action = (request.json or {}).get("action")
    r = supabase.table("contest_reveals").select("*").eq("id", rid).execute().data
    if not r or r[0]["receiver_id"] != user_id or r[0]["status"] != "pending" or action not in ("accept", "reject"):
        return err("Not found or invalid", 404)
    new = "accepted" if action == "accept" else "rejected"
    supabase.table("contest_reveals").update({"status": new, "responded_at": _iso()}).eq("id", rid).execute()
    _mark_read(user_id, r[0]["sender_id"], "contest_reveal_request")
    _notify(r[0]["sender_id"], user_id, "contest_reveal_" + new, f"{_first_name(user_id)} {'accepted' if new == 'accepted' else 'declined'} your contest reveal request")
    return ok({"status": new})


# ── Video calls: invitations + provider-backed private rooms ──
@app.route("/api/video", methods=["GET"])
@require_auth
def video_list(user_id):
    """Open video dates (invited/accepted) for the caller — lets guests see and answer invitations."""
    rows = supabase.table("video_dates").select("*").or_(f"host_id.eq.{user_id},guest_id.eq.{user_id}") \
        .in_("status", ["invited", "scheduled", "accepted"]).order("created_at", desc=True).limit(20).execute().data
    out = []
    for v in rows:
        if user_id not in (v["host_id"], v["guest_id"]):
            continue
        other = v["guest_id"] if v["host_id"] == user_id else v["host_id"]
        u = supabase.table("users").select("first_name").eq("id", other).execute().data
        out.append({"id": v["id"], "other_id": other, "room_id": v["room_id"], "status": v["status"], "scheduled_at": v.get("scheduled_at"),
                    "other_name": u[0]["first_name"] if u else "Match", "is_host": v["host_id"] == user_id})
    return ok(out)


@app.route("/api/video/<vid>/respond", methods=["POST"])
@require_auth
def video_respond(user_id, vid):
    action = (request.json or {}).get("action")      # accept | reject | cancel
    v = supabase.table("video_dates").select("*").eq("id", vid).execute().data
    if not v or user_id not in (v[0]["host_id"], v[0]["guest_id"]):
        return err("Not found", 404)
    v = v[0]
    if action in ("accept", "reject") and (user_id != v["guest_id"] or v["status"] not in ("scheduled", "invited")):
        return err("Cannot respond", 403)
    if action == "cancel" and user_id != v["host_id"]:
        return err("Only the host can cancel", 403)
    new = {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}.get(action)
    if not new:
        return err("action must be accept, reject or cancel")
    if action == "cancel" and v["status"] not in ("invited", "scheduled", "accepted"):
        return err("This video date is already closed", 409)
    done = supabase.table("video_dates").update({"status": new}).eq("id", vid).eq("status", v["status"]).execute().data
    if not done:
        return err("This video date was just updated - refresh and try again", 409)
    other = v["host_id"] if user_id == v["guest_id"] else v["guest_id"]
    _mark_read(v["guest_id"], v["host_id"], "video_invite")
    _closeness_touch(user_id, other, force=True)
    _notify(other, user_id, "video_" + new, f"{_first_name(user_id)} {dict(accepted='accepted', rejected='declined', cancelled='cancelled')[new]} the video date 📹",
            "Open the Hub and tap Join call" if new == "accepted" else "")
    return ok({"status": new})


def _daily_token(room_name, uid):
    def call(path, payload):
        req = urllib.request.Request("https://api.daily.co/v1/" + path, data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + DAILY_API_KEY, "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=10))
    try:
        call("rooms", {"name": room_name, "privacy": "private", "properties": {"exp": int((_now() + timedelta(hours=3)).timestamp())}})
    except urllib.error.HTTPError as e:
        if e.code != 400:      # 400 = room already exists
            raise
    t = call("meeting-tokens", {"properties": {"room_name": room_name, "user_name": uid[:8],
                                               "exp": int((_now() + timedelta(hours=2)).timestamp())}})
    return t["token"]


@app.route("/api/video/room/<room_id>/join", methods=["GET"])
@require_auth
def video_join(user_id, room_id):
    """Replaces the demo token flow. Only the accepted host/guest of a still-matched pair can join."""
    v = supabase.table("video_dates").select("*").eq("room_id", room_id).execute().data
    if not v or user_id not in (v[0]["host_id"], v[0]["guest_id"]):
        return err("Not found", 404)
    v = v[0]
    if v["status"] != "accepted" or not _is_active_match(v["host_id"], v["guest_id"]):
        return err("Call is not accepted or the connection has ended", 403)
    if not DAILY_API_KEY or not DAILY_DOMAIN:
        # No paid provider configured: fall back to a free Jitsi room (room id is random + unguessable) so video works out of the box.
        return ok({"room_id": room_id, "provider": "jitsi", "url": f"https://meet.jit.si/soulsync-{room_id}"})
    try:
        token = _daily_token(room_id, user_id)
    except Exception:
        return err("Could not create the call room", 502)
    return ok({"room_id": room_id, "token": token, "provider": "daily",
               "url": f"https://{DAILY_DOMAIN}.daily.co/{room_id}?t={token}"})



# ═════════════════════════════════════════════
# PROFILE SCREENING + SCREENING-BASED COMPATIBILITY (additive) — see migration_screening.sql
# ═════════════════════════════════════════════
CONSENT_VERSION = "2026-10"
SCREEN_OPTIONS = {
    "relationship_goal":   ["long_term", "marriage", "casual", "exploring"],
    "monogamy":            ["monogamous", "open_to_discuss", "non_monogamous"],
    "marriage_timeline":   ["within_1y", "1_2y", "3_5y", "no_rush", "not_sure"],
    "children_plans":      ["want", "dont_want", "open", "unsure"],
    "comm_style":          ["direct", "thoughtful", "emotive"],
    "emotional_readiness": ["ready", "healing", "unsure"],
}
SCREEN_MULTI = {
    "lifestyle":    ["non_smoker", "smoker", "non_drinker", "social_drinker", "drinks_often", "active", "homebody", "vegetarian", "early_bird", "night_owl"],
    "values":       ["family", "honesty", "faith", "ambition", "kindness", "adventure", "independence", "loyalty", "growth", "humour"],
    "interests":    ["music", "travel", "fitness", "cooking", "reading", "movies", "gaming", "art", "outdoors", "tech"],
    "dealbreakers": ["smoker", "frequent_drinker", "wants_children", "no_children", "casual_only", "different_city", "non_monogamous"],
}
SCREEN_REQUIRED = ["gender", "interested_in", "relationship_goal", "monogamy", "marriage_timeline", "children_plans", "age_pref_min",
                   "age_pref_max", "distance_pref", "city", "lifestyle", "values", "interests", "comm_style", "emotional_readiness",
                   "consent", "age_confirmed"]
SCREEN_COLUMNS = ["gender", "interested_in", "relationship_goal", "monogamy", "marriage_timeline", "children_plans", "comm_style",
                  "emotional_readiness", "age_pref_min", "age_pref_max", "distance_pref", "city", "lifestyle", "values", "interests",
                  "dealbreakers", "consent", "consent_at", "consent_version", "age_confirmed"]
SCREEN_LABELS = {"gender": "gender", "interested_in": "who you are interested in", "relationship_goal": "relationship goal", "monogamy": "monogamy preference",
                 "marriage_timeline": "marriage timeline", "children_plans": "family plans", "age_pref_min": "minimum partner age",
                 "age_pref_max": "maximum partner age", "distance_pref": "distance preference", "city": "city", "lifestyle": "lifestyle",
                 "values": "values", "interests": "interests", "comm_style": "communication style",
                 "emotional_readiness": "emotional readiness", "consent": "consent to the guidelines, privacy and safety policies",
                 "age_confirmed": "18+ confirmation"}
# Compatibility weights (percent, must sum to 100). Edit here to re-tune; documented in AUDIT.md.
SCREEN_WEIGHTS = {"goal": 20, "children": 14, "values": 14, "personality": 10, "timeline": 8, "lifestyle": 8, "comm": 8,
                  "monogamy": 6, "interests": 6, "location": 6}
FACTOR_LABELS = {"goal": "Relationship goals", "children": "Family plans", "values": "Shared values", "personality": "Personality fit",
                 "timeline": "Marriage timeline", "lifestyle": "Lifestyle", "comm": "Communication style", "monogamy": "Views on exclusivity",
                 "interests": "Shared interests", "location": "Location"}
REASON_TEXT = {"goal": "Similar relationship goals", "children": "Aligned family plans", "values": "Shared values", "personality": "Personality fit",
               "timeline": "Similar timeline", "lifestyle": "Compatible lifestyle", "comm": "Compatible communication",
               "monogamy": "Same view on exclusivity", "interests": "Shared interests", "location": "Same city"}
ELIGIBILITY_MESSAGE = ("HEAVEN is designed for adults looking for relationships between women and men. Based on the gender and preference "
                       "you selected, we can't create matches for you right now. If you chose these by mistake, you can change them and try again.")
_GOAL = {frozenset(["long_term", "marriage"]): 85, frozenset(["exploring", "long_term"]): 60, frozenset(["exploring", "marriage"]): 55,
         frozenset(["exploring", "casual"]): 70, frozenset(["casual", "long_term"]): 25, frozenset(["casual", "marriage"]): 10}
_COMM = {"direct-direct": 100, "thoughtful-thoughtful": 100, "emotive-emotive": 100, "direct-thoughtful": 60, "thoughtful-emotive": 70, "direct-emotive": 50}
_YEARS = {"within_1y": 1, "1_2y": 2, "3_5y": 4}


def _age_on(dob_str, today=None):
    """Exact age in whole years (the old `days // 365` let people turning 18 within ~4 days through)."""
    dob = datetime.strptime(str(dob_str)[:10], "%Y-%m-%d").date()
    t = today or datetime.now(timezone.utc).date()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def _norm_gender(g):
    g = str(g or "").lower()
    return "man" if g in ("man", "male", "men", "m") else "woman" if g in ("woman", "female", "women", "f") else "other"


def _norm_interest(i):
    i = str(i or "").lower()
    return "man" if i in ("man", "male", "men") else "woman" if i in ("woman", "female", "women") else "everyone"


def _individual_eligible(gender, interest):
    if ELIGIBILITY_MODE == "open":
        return True, ""
    if (_norm_gender(gender), _norm_interest(interest)) in (("man", "woman"), ("woman", "man")):
        return True, ""
    return False, ELIGIBILITY_MESSAGE


def _screen_row(uid):
    r = supabase.table("screening_profiles").select("*").eq("user_id", uid).execute().data
    return r[0] if r else {}


def _validate_screening(d):
    clean, errs = {}, {}
    for k, opts in SCREEN_OPTIONS.items():
        if k in d:
            if d[k] in opts: clean[k] = d[k]
            else: errs[k] = "Choose one of the listed options"
    if "gender" in d:
        v = str(d["gender"]).lower()
        if v in ("man", "woman", "other", "male", "female"): clean["gender"] = _norm_gender(v)
        else: errs["gender"] = "Choose one of the listed options"
    if "interested_in" in d:
        v = str(d["interested_in"]).lower()
        if v in ("man", "woman", "everyone", "men", "women", "male", "female"): clean["interested_in"] = _norm_interest(v)
        else: errs["interested_in"] = "Choose one of the listed options"
    for k, lo, hi, label in (("age_pref_min", 18, 99, "Age"), ("age_pref_max", 18, 99, "Age"), ("distance_pref", 5, 1000, "Distance")):
        if k in d:
            try:
                v = int(d[k])
                if not (lo <= v <= hi) or isinstance(d[k], bool): raise ValueError
                clean[k] = v
            except (TypeError, ValueError):
                errs[k] = f"{label} must be a whole number between {lo} and {hi}" + (" (adults only)" if k.startswith("age") else "")
    if "city" in d:
        v = str(d["city"] or "").strip()
        if 1 <= len(v) <= 80 and not re.search(r"[<>]", v): clean["city"] = v
        else: errs["city"] = "Enter your city (max 80 characters, no < or >)"
    for k, allowed in SCREEN_MULTI.items():
        if k in d:
            v = d[k]
            if isinstance(v, list) and len(v) <= 20 and all(x in allowed for x in v): clean[k] = sorted(set(v))
            else: errs[k] = "Choose from the listed options"
    if "consent" in d:
        c = d["consent"] if isinstance(d["consent"], dict) else {}
        clean["consent"] = {x: c.get(x) is True for x in ("guidelines", "privacy", "safety")}
    if "age_confirmed" in d:
        clean["age_confirmed"] = d["age_confirmed"] is True
    return clean, errs


def _screening_missing(row):
    miss = []
    for k in SCREEN_REQUIRED:
        v = row.get(k)
        if k == "consent": ok_ = isinstance(v, dict) and all(v.get(x) is True for x in ("guidelines", "privacy", "safety"))
        elif k == "age_confirmed": ok_ = v is True
        elif k in ("lifestyle", "values", "interests"): ok_ = bool(v)
        else: ok_ = v not in (None, "")
        if not ok_: miss.append(k)
    return miss


def _sync_screening(user_id, m, age):
    now = _iso()
    supabase.table("users").update({"gender": m["gender"], "interested_in": m["interested_in"], "age": age, "screening_complete": True}).eq("id", user_id).execute()
    goal_label = {"long_term": "Long-term relationship", "marriage": "Marriage", "casual": "Casual dating", "exploring": "Friendship first"}[m["relationship_goal"]]
    supabase.table("preferences").upsert({"user_id": user_id, "age_min": m["age_pref_min"], "age_max": m["age_pref_max"], "looking_for": goal_label,
                                          "max_distance": m["distance_pref"], "updated_at": now}, on_conflict="user_id").execute()
    kids = {"want": "yes", "dont_want": "no"}.get(m["children_plans"], "open")
    supabase.table("personality_profiles").upsert({"user_id": user_id, "communication_type": m["comm_style"], "wants_children": kids, "updated_at": now}, on_conflict="user_id").execute()
    supabase.table("profiles").upsert({"user_id": user_id, "city": m["city"], "updated_at": now}, on_conflict="user_id").execute()


@app.route("/api/screening", methods=["GET"])
@require_auth
def screening_get(user_id):
    u = supabase.table("users").select("*").eq("id", user_id).execute().data
    if not u:
        return err("User not found", 404)
    u = u[0]; row = _screen_row(user_id)
    prof = supabase.table("profiles").select("city").eq("user_id", user_id).execute().data
    merged = {"gender": _norm_gender(u.get("gender")), "interested_in": _norm_interest(u.get("interested_in")), "city": (prof[0].get("city") if prof else "") or ""}
    merged.update({k: v for k, v in row.items() if k in SCREEN_COLUMNS and v is not None})
    el, msg = _individual_eligible(merged.get("gender"), merged.get("interested_in"))
    try: age = _age_on(u["dob"])
    except Exception: age = u.get("age")
    return ok({"answers": merged, "status": "complete" if u.get("screening_complete") else "incomplete", "missing": _screening_missing(merged),
               "eligible": el, "eligibility_message": msg, "age": age})


@app.route("/api/screening", methods=["PUT"])
@require_auth
@_rate_limit(60, 60)
def screening_put(user_id):
    """Partial saves keep the profile 'incomplete'. Only submit:true with every required answer, eligibility and 18+ makes it complete."""
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return err("Invalid request body")
    u = supabase.table("users").select("*").eq("id", user_id).execute().data
    if not u:
        return err("User not found", 404)
    u = u[0]
    try: age = _age_on(u["dob"])
    except Exception: return err("Your date of birth is missing or invalid", 400)
    if age < 0 or age > 110:
        return err("Your date of birth is invalid", 400)
    if age < 18:
        return err("You must be 18 or older to use HEAVEN", 403)
    clean, errs = _validate_screening(d)
    if errs:
        return jsonify({"success": False, "error": "Please fix: " + "; ".join(f"{SCREEN_LABELS.get(k, k)} — {v}" for k, v in errs.items()), "fields": errs}), 400
    prof = supabase.table("profiles").select("city").eq("user_id", user_id).execute().data
    merged = {"gender": _norm_gender(u.get("gender")), "interested_in": _norm_interest(u.get("interested_in")), "city": (prof[0].get("city") if prof else "") or ""}
    merged.update({k: v for k, v in _screen_row(user_id).items() if k in SCREEN_COLUMNS and v is not None})
    merged.update(clean)
    if merged.get("age_pref_min") is not None and merged.get("age_pref_max") is not None and merged["age_pref_min"] > merged["age_pref_max"]:
        return jsonify({"success": False, "error": "Maximum partner age must be at least the minimum", "fields": {"age_pref_max": "Must be ≥ minimum"}}), 400
    if isinstance(clean.get("consent"), dict) and all(clean["consent"].values()):
        merged["consent_at"], merged["consent_version"] = _iso(), CONSENT_VERSION
    el, emsg = _individual_eligible(merged.get("gender"), merged.get("interested_in"))
    miss = _screening_missing(merged)
    was_complete = bool(u.get("screening_complete"))
    complete = el and not miss and (d.get("submit") is True or was_complete)
    row = {k: merged[k] for k in SCREEN_COLUMNS if k in merged}
    row.update({"user_id": user_id, "status": "complete" if complete else "incomplete", "updated_at": _iso()})
    supabase.table("screening_profiles").upsert(row, on_conflict="user_id").execute()
    if complete:
        _sync_screening(user_id, merged, age)
    elif was_complete:
        supabase.table("users").update({"screening_complete": False}).eq("id", user_id).execute()
    if not el:
        return jsonify({"success": False, "error": emsg, "code": "ineligible"}), 422
    if d.get("submit") is True and miss:
        return jsonify({"success": False, "error": "Some required answers are missing: " + ", ".join(SCREEN_LABELS[k] for k in miss), "missing": miss}), 400
    return ok({"status": "complete" if complete else "incomplete", "missing": miss}, "Saved")


def _violated_dealbreakers(a, b):
    """Dealbreakers of A that B triggers."""
    b_life = b.get("lifestyle") or []
    hit = {"smoker": "smoker" in b_life, "frequent_drinker": "drinks_often" in b_life, "wants_children": b.get("children_plans") == "want",
           "no_children": b.get("children_plans") == "dont_want", "casual_only": b.get("relationship_goal") == "casual",
           "different_city": (a.get("city") or "").strip().lower() != (b.get("city") or "").strip().lower(),
           "non_monogamous": b.get("monogamy") == "non_monogamous"}
    return [k for k in (a.get("dealbreakers") or []) if hit.get(k)]


def _pair_ok(au, a, bu, b):
    """Server-side visibility / interaction rule: both screened, mutually eligible, inside each other's age range, no dealbreaker."""
    if not _ready(au) or not _ready(bu):
        return False, "screening"
    if not _eligible(au, bu):
        return False, "eligibility"
    try:
        if not (a["age_pref_min"] <= int(bu.get("age") or 0) <= a["age_pref_max"]) or not (b["age_pref_min"] <= int(au.get("age") or 0) <= b["age_pref_max"]):
            return False, "age"
    except (KeyError, TypeError):
        return False, "screening"
    if _violated_dealbreakers(a, b) or _violated_dealbreakers(b, a):
        return False, "dealbreaker"
    return True, ""


def _gate(user_id, other_id):
    """None if the caller may interact with other_id, else an error response (no private reason is revealed)."""
    us = {x["id"]: x for x in supabase.table("users").select("*").in_("id", [user_id, other_id]).execute().data}
    if user_id not in us or other_id not in us:
        return err("User not found", 404)
    if not us[user_id].get("screening_complete"):
        return err("Complete your profile screening first", 403)
    if not us[user_id].get("challenge_complete"):
        return err("Complete the 21-day challenge first", 403)
    _pw_me, _pw_o = _paired_with(user_id), _paired_with(other_id)
    if (_pw_me and _pw_me != other_id) or (_pw_o and _pw_o != user_id):      # exclusivity: nobody with an active pairing can be liked/opened
        return err("You can't interact with this profile", 403)
    ok_, _ = _pair_ok(us[user_id], _screen_row(user_id), us[other_id], _screen_row(other_id))
    return None if ok_ else err("You can't interact with this profile", 403)


def _overlap(a, b):
    a, b = set(a or []), set(b or [])
    return len(a & b) / min(len(a), len(b)) * 100 if a and b else 60.0


def _match_score(a, pa, b, pb, movie=None):
    """Returns (score 5-99, factors[]). Deterministic; falls back to the personality score when screening data is missing."""
    base = _compute_compatibility(pa, pb)
    if not a or not b:
        return base, []
    g1, g2 = a.get("relationship_goal"), b.get("relationship_goal")
    c1, c2 = a.get("children_plans"), b.get("children_plans")
    t1, t2 = _YEARS.get(a.get("marriage_timeline")), _YEARS.get(b.get("marriage_timeline"))
    m1, m2 = a.get("monogamy"), b.get("monogamy")
    dp = min(a.get("distance_pref") or 50, b.get("distance_pref") or 50)
    s = {
        "goal": 100 if g1 == g2 else _GOAL.get(frozenset([g1, g2]), 40),
        "children": 100 if c1 == c2 else 0 if {c1, c2} == {"want", "dont_want"} else 70,
        "values": _overlap(a.get("values"), b.get("values")),
        "personality": base,
        "timeline": 100 if a.get("marriage_timeline") == b.get("marriage_timeline") else (max(0, 100 - 30 * abs(t1 - t2)) if t1 and t2 else 70),
        "lifestyle": _overlap(a.get("lifestyle"), b.get("lifestyle")),
        "comm": _COMM.get(f"{a.get('comm_style')}-{b.get('comm_style')}", _COMM.get(f"{b.get('comm_style')}-{a.get('comm_style')}", 60)),
        "monogamy": 100 if m1 == m2 else 0 if {m1, m2} == {"monogamous", "non_monogamous"} else 70,
        "interests": _overlap(a.get("interests"), b.get("interests")),
        "location": 100 if (a.get("city") or "").strip().lower() == (b.get("city") or "").strip().lower() else (30 if dp < 100 else 55),
    }
    total = sum(s[k] * w for k, w in SCREEN_WEIGHTS.items()) / sum(SCREEN_WEIGHTS.values())
    if movie:   # movie taste = 8% of the final score (local rules-based baseline, see challenge.movie_compat)
        total = total * 0.92 + movie[0] * 0.08
    # Fundamental conflicts cap the score (documented): opposite relationship goals -> max 60, opposite family plans -> max 55
    if s["goal"] <= 25: total = min(total, 60)
    if s["children"] == 0: total = min(total, 55)
    factors = sorted(({"key": k, "label": FACTOR_LABELS[k], "score": round(s[k]), "weight": w} for k, w in SCREEN_WEIGHTS.items()),
                     key=lambda f: -(f["score"] * f["weight"]))
    if movie:
        factors.append({"key": "movies", "label": "Movie taste", "score": movie[0], "weight": 8})
        factors.sort(key=lambda f: -(f["score"] * f["weight"]))
    return min(99, max(5, round(total))), factors


def _reasons(factors):
    return [REASON_TEXT[f["key"]] for f in factors if f["score"] >= 80][:3]


# ── Audit fixes: routes the frontend already calls but the backend lacked ──
@app.route("/api/likes/mine", methods=["GET"])
@require_auth
def my_likes(user_id):
    """IDs of everyone the caller has liked: lets every device show the same Liked state."""
    rows = supabase.table("likes").select("liked_id").eq("liker_id", user_id).execute().data
    return ok([r["liked_id"] for r in rows])


@app.route("/api/likes/<liked_id>", methods=["DELETE"])
@require_auth
def unlike_user(user_id, liked_id):
    """Frontend calls DELETE likes/<id> (un-like). Removes only the caller's own like."""
    if not _valid_id(liked_id):
        return err("Invalid id")
    supabase.table("likes").delete().eq("liker_id", user_id).eq("liked_id", liked_id).execute()
    _mark_read(liked_id, user_id, "like")
    return ok(message="Like removed")


@app.route("/api/conversations/<convo_id>/compatibility", methods=["GET"])
@require_auth
def convo_compatibility(user_id, convo_id):
    rows = supabase.table("conversations").select("user1_id, user2_id").eq("id", convo_id).execute().data
    if not rows or user_id not in (rows[0]["user1_id"], rows[0]["user2_id"]):
        return err("Unauthorized", 403)
    other = rows[0]["user2_id"] if rows[0]["user1_id"] == user_id else rows[0]["user1_id"]
    return compatibility_report.__wrapped__(user_id, other)



@app.route("/favicon.ico")
def favicon():
    return "", 204   # silences the browser's favicon 404


import challenge
challenge.register(app, globals())      # 21-day challenge, movie journal, exclusive pairing (see challenge.py)

import couple_games
couple_games.register(app, globals())   # Couple Zone private game rooms (see couple_games.py)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000, host="0.0.0.0", threaded=False)  # debug OFF unless FLASK_DEBUG=1