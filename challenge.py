"""SoulSync: 21-day consistency challenge, movie preference analysis and exclusive pairing.

Registered from app.py with `challenge.register(app, globals())` so it reuses app.py's auth, helpers and Supabase client.
Run migration_challenge.sql first.

POLICIES (all enforced on the server):
* A "day" is the user's LOCAL date after subtracting `cutoff_hour` (default 04:00), so 01:00 still counts as the previous day.
  The client never sends a date. Timezone changes are locked for 7 days after the last change.
* One live (pending/approved) submission per local day; rejected proofs can be resubmitted (max 5 tries/day). The same file
  (SHA-256) can never be submitted twice by a user.
* MISSED DAY = RESET: eligibility needs 21 consecutive local days that are all APPROVED. A gap restarts the run at day 1.
  Once earned, `users.challenge_complete` is permanent (editing preferences or missing days later does not remove it).
* Proofs are reviewed AUTOMATICALLY by the Gemini vision model on upload (approved / rejected right away). If the AI service is
  unavailable or not configured, the proof stays PENDING for a human reviewer. AI reviews have reviewed_by NULL + reviewed_at set.
  Token cost is kept minimal: 1 call per upload, image downscaled to one 768px tile (~258 tokens), tiny prompt, ~20 real output tokens (cap 400 only guards against thinking).
* Movie analysis is a local rules-based baseline (no external AI).
"""
import base64
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import request

CHALLENGE_DAYS = 21
MAX_TRIES_PER_DAY = 5
MIN_MOVIES, MIN_GENRES = 5, 3
HOBBIES = ["fitness", "cooking", "reading", "art", "music", "gardening", "writing", "photography", "dance", "yoga", "other"]
GENRES = ["action", "comedy", "drama", "romance", "thriller", "horror", "sci-fi", "fantasy", "animation", "documentary",
          "mystery", "adventure", "family", "crime", "musical", "war", "biography"]
_GENRE_ALIAS = {"science fiction": "sci-fi", "scifi": "sci-fi", "sci fi": "sci-fi", "rom-com": "romance", "romcom": "romance", "romantic": "romance",
                "animated": "animation", "anime": "animation", "docu": "documentary", "biopic": "biography", "suspense": "thriller"}
SIGNATURES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}
PROFILE_LISTS = ("genres_like", "genres_dislike", "languages", "fav_films")


def norm_genre(x):
    k = re.sub(r"\s+", " ", str(x or "").strip().lower())
    k = _GENRE_ALIAS.get(k, k)
    return k if k in GENRES else None


def norm_genres(xs, limit=6):
    out = []
    for x in (xs if isinstance(xs, list) else []):
        g = norm_genre(x)
        if g and g not in out:
            out.append(g)
    return out[:limit]


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ── Automatic AI review of the daily proof photo (low-token) ─────────────────
AI_PROOF_MAX_SIDE = 768          # <=768px both sides = a single 768x768 tile = ~258 image tokens (smaller input = cheaper)
_AI_THINKING_OK = True           # flipped off for the process if the model rejects thinkingConfig


def shrink_for_ai(data, ctype):
    """Downscale to one tile and re-encode as JPEG q70 so the model bills ~258 image tokens. Falls back to the original bytes."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.thumbnail((AI_PROOF_MAX_SIDE, AI_PROOF_MAX_SIDE))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=70, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, ctype


def ai_review_proof(g, data, ctype, hobby, detail=""):
    """-> (True|False|None, reason). True=approve, False=reject, None=AI unavailable (caller leaves it pending for a human)."""
    global _AI_THINKING_OK
    key, model, http = g.get("GEMINI_API_KEY"), g.get("VERIFY_MODEL"), g.get("_http_json")
    if not (key and model and http):
        return None, ""
    img, mt = shrink_for_ai(data, ctype)
    what = hobby if hobby != "other" or not detail else f"other: {detail}"
    prompt = (f"Daily habit proof. Hobby: {what}. Approve only if this is a real live camera photo clearly showing this hobby "
              "being done or its result today. Reject screenshots, photos of screens/prints, stock/AI images, blank or unrelated photos. "
              "Do not identify anyone. JSON only: {\"ok\":bool,\"r\":\"max 10 words why rejected, else empty\"}")
    def call(think):
        cfg = {"responseMimeType": "application/json", "maxOutputTokens": 400, "temperature": 0}
        if think:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}          # no hidden reasoning tokens for a yes/no check
        body = json.dumps({"contents": [{"parts": [{"inline_data": {"mime_type": mt, "data": base64.b64encode(img).decode()}},
                                                   {"text": prompt}]}], "generationConfig": cfg}).encode()
        r = http("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                 {"x-goog-api-key": key, "content-type": "application/json"}, body, 30)
        txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
        return json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    try:
        try:
            j = call(_AI_THINKING_OK)
        except Exception as e:
            if _AI_THINKING_OK and getattr(e, "code", None) == 400:    # model doesn't accept thinkingConfig: retry once without, remember it
                _AI_THINKING_OK = False
                j = call(False)
            else:
                raise
    except Exception as e:
        body = ""
        try: body = e.read().decode()[:300]          # HTTPError: Google's error message
        except Exception: pass
        (g.get("app") and g["app"].logger or __import__("logging").getLogger(__name__)).warning("proof AI review unavailable: %r %s", e, body)
        return None, ""
    if j.get("ok") is True:
        return True, ""
    if j.get("ok") is False:
        return False, (str(j.get("r") or "").strip() or "Photo doesn't clearly show today's activity - retake it in good light")[:300]
    return None, ""


# ── Pure logic (unit-tested without a database) ──────────────────────────────
def local_day(now_utc, tz_name, cutoff_hour):
    """The challenge day for this instant: local time minus the cutoff, as a date."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return (now_utc.astimezone(tz) - timedelta(hours=cutoff_hour)).date()


def _runs(dates):
    """Consecutive-date runs from a set of dates -> list of (start, length), ascending."""
    out, ds = [], sorted(dates)
    i = 0
    while i < len(ds):
        j = i
        while j + 1 < len(ds) and (ds[j + 1] - ds[j]).days == 1:
            j += 1
        out.append((ds[i], j - i + 1))
        i = j + 1
    return out


def compute_progress(rows, today):
    """rows: [{local_date: date, status}]. Returns the backend-authoritative streak view. Missed day => the run restarts."""
    approved = {r["local_date"] for r in rows if r["status"] == "approved"}
    live = {r["local_date"] for r in rows if r["status"] in ("pending", "approved")}
    best = max([n for _, n in _runs(approved)] or [0])
    complete = best >= CHALLENGE_DAYS
    # current run = run of live days ending today or yesterday (today may not be submitted yet)
    cur_start, cur_len = None, 0
    for s, n in _runs(live):
        end = s + timedelta(days=n - 1)
        if end >= today - timedelta(days=1):
            cur_start, cur_len = s, n
    start = cur_start or today
    by_date = {}
    for r in rows:                       # prefer approved > pending > rejected for display
        rank = {"approved": 3, "pending": 2, "rejected": 1}[r["status"]]
        if r["local_date"] not in by_date or rank > by_date[r["local_date"]][0]:
            by_date[r["local_date"]] = (rank, r["status"])
    days = []
    for i in range(CHALLENGE_DAYS):
        d = start + timedelta(days=i)
        st = by_date.get(d, (0, None))[1]
        if st is None:
            st = "today" if d == today else ("future" if d > today else "missed")
        days.append({"day": i + 1, "date": d.isoformat(), "status": st})
    cur_approved = sum(1 for d in days if d["status"] == "approved")
    milestones = [7, 14, 21]
    nxt = next((m for m in milestones if m > cur_approved), None)
    return {"complete": complete, "current_streak": cur_len, "approved_in_run": cur_approved, "best_streak": best,
            "submitted_today": today in live, "days": days, "next_milestone": nxt,
            "days_to_next_milestone": (nxt - cur_approved) if nxt else 0}


def movie_insights(logs, profile=None):
    """Genre preferences from repeated selections + ratings. Needs >=2 logs of a genre before it counts as like/dislike."""
    profile = profile or {}
    stats = {}
    for m in logs:
        for g in m.get("genres") or []:
            s = stats.setdefault(g, {"n": 0, "sum": 0})
            s["n"] += 1
            s["sum"] += int(m.get("rating") or 0)
    likes, dislikes, uncertain = [], [], []
    for g, s in stats.items():
        avg = s["sum"] / s["n"]
        item = {"genre": g, "count": s["n"], "avg": round(avg, 2)}
        if s["n"] >= 2 and avg >= 3.75:
            likes.append(item)
        elif s["n"] >= 2 and avg <= 2.25:
            dislikes.append(item)
        else:
            uncertain.append(item)
    like_set, dis_set = {x["genre"] for x in likes}, {x["genre"] for x in dislikes}
    for g in profile.get("genres_like") or []:      # stated favourites count only as a weak signal for genres not contradicted by ratings
        if g not in dis_set and g not in like_set and stats.get(g, {}).get("n", 0) >= 1:
            like_set.add(g)
    for g in profile.get("genres_dislike") or []:
        if g not in like_set:
            dis_set.add(g)
    langs = sorted({(m.get("language") or "").strip().lower() for m in logs if m.get("language")})
    rated_genres = len(stats)
    ready = len(logs) >= MIN_MOVIES and rated_genres >= MIN_GENRES and any(s["n"] >= 2 for s in stats.values())
    return {"logged": len(logs), "genres_seen": rated_genres, "likes": sorted(like_set), "dislikes": sorted(dis_set),
            "uncertain": sorted(x["genre"] for x in uncertain if x["genre"] not in like_set | dis_set),
            "languages": langs, "ready": ready, "needs": {"movies": max(0, MIN_MOVIES - len(logs)), "genres": max(0, MIN_GENRES - rated_genres)},
            "detail": sorted(likes + dislikes + uncertain, key=lambda x: -x["count"])}


def movie_compat(a, b):
    """Transparent 0-100 score. 50 base; +40 * (shared likes / smaller like-set); -20 per like-vs-dislike conflict (max -40);
    +10 if they share a language. Returns (score, shared_genres, shared_languages)."""
    la, lb, da, db_ = set(a["likes"]), set(b["likes"]), set(a["dislikes"]), set(b["dislikes"])
    shared = sorted(la & lb)
    conflicts = len(la & db_) + len(lb & da)
    base = 50 + (40 * len(shared) / max(1, min(len(la), len(lb))) if la and lb else 0) - min(40, 20 * conflicts)
    langs = sorted(set(a["languages"]) & set(b["languages"]))
    base += 10 if langs else 0
    return max(0, min(100, round(base))), shared, langs


# ── Registration ─────────────────────────────────────────────────────────────
def register(app, g):
    S = lambda: g["supabase"]                       # looked up per call (tests swap the client)
    ok, err, auth, iso, notify = g["ok"], g["err"], g["require_auth"], g["_iso"], g["_notify"]
    admin_only, valid_id = g["_admin_only"], g["_valid_id"]
    bucket = lambda: g.get("VERIFY_BUCKET", "verification")
    max_bytes = lambda: g.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)

    def now():
        return datetime.now(timezone.utc)

    def cprofile(uid):
        r = S().table("challenge_profiles").select("*").eq("user_id", uid).execute().data
        return r[0] if r else {}

    def today_for(cp):
        return local_day(now(), cp.get("timezone") or "Asia/Kolkata", int(cp.get("cutoff_hour") or 4))

    def sub_rows(uid):
        rows = S().table("challenge_submissions").select("id, local_date, status, created_at, reject_reason").eq("user_id", uid).execute().data or []
        out = []
        for r in rows:
            d = r["local_date"]
            out.append({**r, "local_date": d if isinstance(d, date) else date.fromisoformat(str(d)[:10])})
        return out

    def movie_rows(uid):
        return S().table("movie_logs").select("*").eq("user_id", uid).execute().data or []

    def my_insights(uid):
        return movie_insights(movie_rows(uid), cprofile(uid))

    def onboarding_missing(cp):
        miss = []
        if cp.get("hobby") not in HOBBIES: miss.append("hobby")
        if not cp.get("timezone"): miss.append("timezone")
        if not (cp.get("routine") or {}).get("sleep"): miss.append("routine")
        if not (cp.get("routine") or {}).get("participation"): miss.append("participation")
        if not (cp.get("genres_like") or cp.get("languages")): miss.append("movie_preferences")
        return miss

    def recompute(uid):
        """Authoritative eligibility. challenge_complete is set only here and is permanent once earned."""
        u = (S().table("users").select("challenge_complete").eq("id", uid).execute().data or [{}])[0]
        if u.get("challenge_complete"):
            return True
        cp = cprofile(uid)
        prog = compute_progress(sub_rows(uid), today_for(cp))
        if prog["complete"] and not onboarding_missing(cp) and my_insights(uid)["ready"]:
            S().table("users").update({"challenge_complete": True}).eq("id", uid).execute()
            S().table("challenge_profiles").update({"completed_at": iso()}).eq("user_id", uid).execute()
            notify(uid, uid, "challenge_complete", "21-day challenge complete 🎉", "You're now eligible for matchmaking")
            return True
        return False

    # ---- onboarding extras / timezone ----
    @app.route("/api/challenge/profile", methods=["GET"])
    @auth
    def ch_profile_get(user_id):
        cp = cprofile(user_id)
        return ok({**{k: cp.get(k) for k in ("hobby", "hobby_detail", "timezone", "cutoff_hour", "routine", *PROFILE_LISTS)},
                   "missing": onboarding_missing(cp), "hobbies": HOBBIES, "genres": GENRES})

    @app.route("/api/challenge/profile", methods=["PUT"])
    @auth
    def ch_profile_put(user_id):
        d = request.get_json(silent=True) or {}
        cp = cprofile(user_id)
        new, errs = {}, {}
        if "hobby" in d:
            if d["hobby"] in HOBBIES: new["hobby"] = d["hobby"]
            else: errs["hobby"] = "Choose one of the listed hobbies"
        if "hobby_detail" in d:
            v = str(d["hobby_detail"] or "").strip()
            if len(v) <= 80 and not re.search(r"[<>]", v): new["hobby_detail"] = v
            else: errs["hobby_detail"] = "Max 80 characters, no < or >"
        if "timezone" in d and d["timezone"] != cp.get("timezone"):
            try: ZoneInfo(str(d["timezone"]))
            except Exception: errs["timezone"] = "Unknown timezone"
            else:
                if cp.get("tz_changed_at") and datetime.fromisoformat(str(cp["tz_changed_at"]).replace("Z", "+00:00")) > now() - timedelta(days=7):
                    errs["timezone"] = "Timezone can be changed once every 7 days"
                else:
                    new["timezone"] = d["timezone"]
                    if cp.get("timezone"): new["tz_changed_at"] = iso()
        if "routine" in d:
            r = d["routine"] if isinstance(d["routine"], dict) else {}
            clean = {k: str(r.get(k) or "").strip()[:80] for k in ("sleep", "work", "habits", "participation", "communication", "boundaries", "partner_qualities", "goals") if r.get(k)}
            if any(re.search(r"[<>]", v) for v in clean.values()): errs["routine"] = "No < or > allowed"
            else: new["routine"] = clean
        if "genres_like" in d: new["genres_like"] = norm_genres(d["genres_like"])
        if "genres_dislike" in d: new["genres_dislike"] = norm_genres(d["genres_dislike"])
        if "languages" in d:
            new["languages"] = [str(x).strip().lower()[:30] for x in (d["languages"] if isinstance(d["languages"], list) else []) if str(x).strip() and not re.search(r"[<>]", str(x))][:6]
        if "fav_films" in d:
            new["fav_films"] = [str(x).strip()[:80] for x in (d["fav_films"] if isinstance(d["fav_films"], list) else []) if str(x).strip() and not re.search(r"[<>]", str(x))][:5]
        if errs:
            return err("; ".join(f"{k}: {v}" for k, v in errs.items()))
        if new.get("hobby") and cp.get("hobby") and new["hobby"] != cp["hobby"] and any(s["status"] == "approved" for s in sub_rows(user_id)):
            return err("Your hobby is locked once a proof has been approved", 409)
        new.update({"user_id": user_id, "updated_at": iso()})
        S().table("challenge_profiles").upsert(new, on_conflict="user_id").execute()   # editing never touches submissions or streaks
        merged = {**cp, **new}
        S().table("challenge_profiles").update({"onboarding_complete": not onboarding_missing(merged)}).eq("user_id", user_id).execute()
        recompute(user_id)
        return ok({"missing": onboarding_missing(merged)}, "Saved")

    # ---- challenge status / submission ----
    @app.route("/api/challenge/status", methods=["GET"])
    @auth
    def ch_status(user_id):
        cp = cprofile(user_id)
        today = today_for(cp)
        rows = sub_rows(user_id)
        prog = compute_progress(rows, today)
        u = (S().table("users").select("challenge_complete").eq("id", user_id).execute().data or [{}])[0]
        ins = my_insights(user_id)
        todays = [r for r in rows if r["local_date"] == today]
        local_hour = (now().astimezone(ZoneInfo(cp.get("timezone") or "Asia/Kolkata"))).hour
        if prog["current_streak"] > 0 and not prog["submitted_today"] and local_hour >= 18 and not u.get("challenge_complete"):
            since = (now() - timedelta(hours=20)).isoformat()
            dup = S().table("notifications").select("id").eq("recipient_id", user_id).eq("type", "challenge_risk").gte("created_at", since).execute().data
            if not dup:
                notify(user_id, user_id, "challenge_risk", "Your streak is at risk", "Submit today's proof before the day ends to keep your streak")
        return ok({**prog, "eligible": bool(u.get("challenge_complete")), "onboarding_missing": onboarding_missing(cp), "movies": ins,
                   "hobby": cp.get("hobby"), "timezone": cp.get("timezone"), "today": today.isoformat(),
                   "today_status": (todays[-1]["status"] if todays else None), "today_reject_reason": next((r["reject_reason"] for r in reversed(todays) if r["status"] == "rejected"), None),
                   "policy": f"A missed day restarts your streak at day 1. You need {CHALLENGE_DAYS} consecutive reviewed-and-approved days. Completing it makes you eligible; it never guarantees a match.",
                   "proof_note": "Each proof is checked automatically by AI when you upload it; if the AI check is unavailable a reviewer decides."})

    @app.route("/api/challenge/submit", methods=["POST"])
    @auth
    def ch_submit(user_id):
        cp = cprofile(user_id)
        if onboarding_missing(cp):
            return err("Finish the onboarding questions first", 403)
        f = request.files.get("proof")
        if not f:
            return err("No file uploaded")
        data = f.read(max_bytes() + 1)
        if not data: return err("Empty file")
        if len(data) > max_bytes(): return err("File too large (max 5 MB)")
        ctype = sniff_image(data)
        if not ctype or f.mimetype not in ("image/jpeg", "image/png", "image/webp") or ctype != f.mimetype:
            return err("Upload a JPEG, PNG or WebP image")
        today = today_for(cp)
        rows = sub_rows(user_id)
        if any(r["local_date"] == today and r["status"] in ("pending", "approved") for r in rows):
            return err("You've already submitted today's proof", 409)
        if sum(1 for r in rows if r["local_date"] == today and r["status"] == "rejected") >= MAX_TRIES_PER_DAY:
            return err("Too many rejected attempts today - try again tomorrow", 429)
        last = [r for r in rows if r["status"] in ("pending", "approved")]
        if last and max(datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")) for r in last) > now() - timedelta(hours=12):
            return err("Come back later - proofs must be about a day apart", 409)   # blocks timezone-hopping for two "days" in a row
        sha = hashlib.sha256(data).hexdigest()
        if S().table("challenge_submissions").select("id").eq("user_id", user_id).eq("file_sha256", sha).execute().data:
            return err("This exact file was already submitted - take a fresh photo", 409)
        path = f"challenge/{user_id}/{today.isoformat()}_{sha[:10]}.{ctype.split('/')[1]}"
        try:
            S().storage.from_(bucket()).upload(path, data, {"content-type": ctype})
        except Exception:
            return err("Upload failed - check the private storage bucket is configured", 500)
        # automatic review: all cheap checks above already passed, so the model is called at most once per accepted upload
        verdict, reason = ai_review_proof(g, data, ctype, cp["hobby"], cp.get("hobby_detail") or "")
        status = {True: "approved", False: "rejected", None: "pending"}[verdict]
        rec = {"user_id": user_id, "local_date": today.isoformat(), "hobby": cp["hobby"], "file_path": path,
               "file_sha256": sha, "status": status, "created_at": iso()}
        if verdict is not None:
            rec.update({"reviewed_at": iso(), "reject_reason": reason})      # reviewed_by stays NULL = reviewed by AI
        try:
            row = S().table("challenge_submissions").insert(rec).execute().data[0]
        except Exception:      # unique indexes caught a double-click / race
            return err("You've already submitted today's proof", 409)
        if verdict is True:
            notify(user_id, user_id, "challenge_approved", "Your daily proof was approved", "")
            recompute(user_id)
            msg = "Approved - today is counted"
        elif verdict is False:
            notify(user_id, user_id, "challenge_rejected", "Your daily proof was rejected", reason)
            msg = "Not approved: " + reason + ". Take a fresh photo and try again."
        else:
            msg = "Submitted - the automatic check is unavailable, so a reviewer will look at it"
        return ok({"id": row["id"], "status": status, "reason": reason or None, "local_date": today.isoformat()}, msg, 201)

    @app.route("/api/challenge/recheck", methods=["POST"])
    @auth
    def ch_recheck(user_id):
        """Re-run the automatic review on today's proof if it was left pending because the AI was unavailable."""
        cp = cprofile(user_id); today = today_for(cp)
        rows = [r for r in S().table("challenge_submissions").select("*").eq("user_id", user_id).eq("status", "pending").execute().data or []
                if str(r["local_date"])[:10] == today.isoformat() and not r.get("reviewed_at")]
        if not rows:
            return err("Nothing waiting for an automatic check", 404)
        r = rows[-1]
        try:
            data = S().storage.from_(bucket()).download(r["file_path"])
        except Exception:
            return err("Proof file unavailable", 404)
        verdict, reason = ai_review_proof(g, data, sniff_image(data) or "image/jpeg", r["hobby"], cp.get("hobby_detail") or "")
        if verdict is None:
            return err("The automatic check is still unavailable - a reviewer will decide", 503)
        st = "approved" if verdict else "rejected"
        S().table("challenge_submissions").update({"status": st, "reviewed_at": iso(), "reject_reason": reason}).eq("id", r["id"]).eq("status", "pending").execute()
        notify(user_id, user_id, "challenge_" + st, "Your daily proof was " + st, reason)
        recompute(user_id)
        return ok({"status": st, "reason": reason or None}, "Approved - today is counted" if verdict else "Not approved: " + reason)

    @app.route("/api/challenge/proof/<sid>", methods=["GET"])
    @auth
    def ch_my_proof(user_id, sid):
        r = S().table("challenge_submissions").select("file_path, user_id").eq("id", sid).execute().data if valid_id(sid) else []
        if not r or r[0]["user_id"] != user_id:
            return err("Not found", 404)
        try:
            return ok({"url": S().storage.from_(bucket()).create_signed_url(r[0]["file_path"], 120)["signedURL"]})
        except Exception:
            return err("Proof unavailable", 404)

    @app.route("/api/admin/challenge/pending", methods=["GET"])
    @auth
    @admin_only
    def ch_admin_pending(user_id):
        rows = S().table("challenge_submissions").select("*").eq("status", "pending").order("created_at").limit(50).execute().data
        for r in rows:
            try: r["signed_url"] = S().storage.from_(bucket()).create_signed_url(r["file_path"], 300)["signedURL"]
            except Exception: r["signed_url"] = None
        return ok(rows)

    @app.route("/api/admin/challenge/<sid>/review", methods=["POST"])
    @auth
    @admin_only
    def ch_admin_review(user_id, sid):
        d = request.get_json(silent=True) or {}
        if d.get("decision") not in ("approved", "rejected"):
            return err("decision must be approved or rejected")
        r = S().table("challenge_submissions").select("*").eq("id", sid).execute().data if valid_id(sid) else []
        if not r or r[0]["status"] != "pending":
            return err("Not found or already reviewed", 404)
        S().table("challenge_submissions").update({"status": d["decision"], "reviewed_by": user_id, "reviewed_at": iso(),
                                                   "reject_reason": str(d.get("reason") or "")[:300]}).eq("id", sid).eq("status", "pending").execute()
        uid = r[0]["user_id"]
        notify(uid, user_id, "challenge_" + d["decision"], "Your daily proof was " + d["decision"], str(d.get("reason") or "")[:300])
        recompute(uid)
        return ok(message=d["decision"])

    # ---- movie journal ----
    @app.route("/api/movies", methods=["GET"])
    @auth
    def movies_list(user_id):
        rows = sorted(movie_rows(user_id), key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return ok({"movies": [{k: r.get(k) for k in ("id", "title", "language", "genres", "rating", "review", "watched_on")} for r in rows],
                   "insights": my_insights(user_id)})

    @app.route("/api/movies", methods=["POST"])
    @auth
    def movies_add(user_id):
        d = request.get_json(silent=True) or {}
        title = str(d.get("title") or "").strip()
        genres = norm_genres(d.get("genres"))
        try:
            rating = int(d.get("rating"))
            if isinstance(d.get("rating"), bool) or not 1 <= rating <= 5: raise ValueError
        except (TypeError, ValueError):
            return err("Rating must be 1-5")
        review = str(d.get("review") or "").strip()[:500]
        lang = str(d.get("language") or "").strip()[:30]
        if not (1 <= len(title) <= 120) or re.search(r"[<>]", title + review + lang):
            return err("Enter a movie title (max 120 characters, no < or >)")
        if not genres:
            return err("Pick at least one genre from the list")
        key = re.sub(r"\s+", " ", title.lower())
        if S().table("movie_logs").select("id").eq("user_id", user_id).eq("title_key", key).execute().data:
            return err("You already logged this movie", 409)
        if len(movie_rows(user_id)) >= 200:
            return err("Movie log limit reached", 429)
        try:
            row = S().table("movie_logs").insert({"user_id": user_id, "title": title, "title_key": key, "language": lang, "genres": genres, "rating": rating,
                                                  "review": review, "created_at": iso()}).execute().data[0]
        except Exception:
            return err("You already logged this movie", 409)
        recompute(user_id)
        return ok({"id": row["id"], "insights": my_insights(user_id)}, "Logged", 201)

    @app.route("/api/movies/<mid>", methods=["DELETE"])
    @auth
    def movies_delete(user_id, mid):
        if not valid_id(mid): return err("Invalid id")
        S().table("movie_logs").delete().eq("id", mid).eq("user_id", user_id).execute()
        return ok(message="Removed")

    @app.route("/api/movies/insights", methods=["GET"])
    @auth
    def movies_insights(user_id):
        return ok(my_insights(user_id))

    # ---- exclusive pairing ----
    @app.route("/api/pairing", methods=["GET"])
    @auth
    def pairing_get(user_id):
        pid = paired_with(user_id)
        if not pid:
            return ok({"active": False})
        u = (S().table("users").select("first_name").eq("id", pid).execute().data or [{}])[0]
        return ok({"active": True, "partner_id": pid, "partner_first_name": u.get("first_name")})

    def paired_with(uid):
        """Partner id if uid has an active exclusive pairing, else None."""
        for col, oc in (("user1_id", "user2_id"), ("user2_id", "user1_id")):
            r = S().table("active_pairings").select("*").eq(col, uid).eq("status", "active").execute().data
            if r: return r[0][oc]
        return None

    def form_pairing(a, b):
        """(True, '') on success; (False, reason) otherwise. Atomic in Postgres (advisory locks + partial unique index)."""
        try:
            S().rpc("create_exclusive_pairing", {"a": a, "b": b}).execute()
            return True, ""
        except Exception as e:
            msg = str(e)
            if "already_paired" in msg or "23505" in msg or "duplicate key" in msg:
                return False, "unavailable"
            g["app"].logger.exception("create_exclusive_pairing failed - has migration_challenge.sql been applied?")
            return False, "unavailable_setup"

    def end_pairing(a, b, who):
        try:
            S().rpc("end_exclusive_pairing", {"a": a, "b": b, "who": who}).execute()
        except Exception:
            g["app"].logger.exception("end_exclusive_pairing failed")

    def cancel_other_opportunities(a, b):
        """Both people leave discovery: drop their other pending likes/dating requests and quietly tell the affected people."""
        pair = {a, b}
        affected = set()
        for me in (a, b):
            for r in S().table("likes").select("liker_id, liked_id").eq("liker_id", me).execute().data or []:
                if r["liked_id"] not in pair: S().table("likes").delete().eq("liker_id", me).eq("liked_id", r["liked_id"]).execute()
            for r in S().table("likes").select("liker_id, liked_id").eq("liked_id", me).execute().data or []:
                if r["liker_id"] not in pair:
                    S().table("likes").delete().eq("liker_id", r["liker_id"]).eq("liked_id", me).execute()
                    affected.add(r["liker_id"])
            for r in S().table("dating_requests").select("id, user1_id, user2_id, sender_id").eq("status", "pending").execute().data or []:
                if me in (r["user1_id"], r["user2_id"]) and not {r["user1_id"], r["user2_id"]} == pair:
                    S().table("dating_requests").update({"status": "cancelled", "updated_at": iso()}).eq("id", r["id"]).execute()
                    affected.add(r["user1_id"] if r["user2_id"] == me else r["user2_id"])
        for x in affected - pair:
            notify(x, x, "profile_unavailable", "A profile you liked is no longer available")   # no identity or reason is disclosed

    def paired_ids():
        return {r["user_id"] for r in (S().table("pairing_members").select("user_id").eq("active", True).execute().data or [])}

    def movie_pair(a, b):
        """(score, shared_genres, shared_languages) when both users finished the movie analysis, else None."""
        ia, ib = my_insights(a), my_insights(b)
        return movie_compat(ia, ib) if ia["ready"] and ib["ready"] else None

    g["REASON_TEXT"]["movies"] = "Similar movie taste"
    g.update({"_paired_ids": paired_ids, "_movie_pair": movie_pair, "_paired_with": paired_with, "_form_pairing": form_pairing, "_end_pairing": end_pairing,
              "_cancel_other_opportunities": cancel_other_opportunities, "_challenge_recompute": recompute,
              "_my_movie_insights": my_insights, "_movie_compat": movie_compat})