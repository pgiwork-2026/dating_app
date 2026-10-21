# SoulSync AI — Audit report
Tested: 15 automated tests (`pip install pytest && pytest -q`) against an in-memory fake Supabase. NOT tested: real Supabase (RLS, storage, constraints), real browser UI, Daily.co video, password recovery.

## Fixed
| Issue | Fix |
|---|---|
| Any logged-in user could read any conversation's messages | membership check in GET messages |
| Requester could accept their own reveal request and advance the phase | accept/decline need a pending request from the other person |
| PostgREST filter injection via `other_user_id`/`liked_id` in `.or_()` | ids validated before use |
| Chat still open after a breakup | send/like/open blocked once match is `ended` |
| CORS `*` + credentials | explicit origins (`CORS_ORIGINS`) |
| `debug=True` on 0.0.0.0 (remote code execution via debugger) | off unless `FLASK_DEBUG=1` |
| Insecure default JWT secret | app refuses to start without `JWT_SECRET` |
| Signup: no email/password/age checks | email format, 8+ chars, 18+, no `<>` in names |
| No brute-force protection | 10 attempts/min/IP on login & signup |
| Stack traces / HTML error pages / 500 on bad JSON, bad ids | JSON error handlers, safe lookups, limits |
| Frontend called `DELETE likes/<id>` and `conversations/<id>/compatibility` (no backend route) | routes added |
| Frontend API base pointed to localhost:5000 when deployed | same-origin `/api/` (file:// still uses localhost) |
| Compatibility score was random when data missing | deterministic default |
| gender/interested_in/bio/photos unvalidated; HTML in profile fields | validated |
| (HEAVEN) timestamp string compares for contests/voice nonce | parsed datetimes |

## Still open (need your decision or setup)
1. **`/api/conversations/open` lets anyone chat with anyone and creates a "match" without a mutual like.** This weakens every "mutual match" check (video, dating). Recommend requiring a mutual like; it changes product behavior, so I didn't.
2. **Password recovery doesn't exist** (needs an email provider). Logout is client-side only; JWTs stay valid up to 7 days (needs a token blocklist if you want server-side logout).
3. ~~Photos visible before reveal~~ **Fixed:** `/api/users/<id>` and discovery now hide photos until the other person accepts a photo reveal (`PHOTO_PRIVACY=all|anon`, default `all`).
4. Names/bio are rendered with `innerHTML` in several frontend spots; server now rejects `<>`, but escape on render too (`escHtml` exists).
5. Missions with ids starting `d` in the frontend are demo data, not real DB rows.
6. Frontend has no screens yet for HEAVEN endpoints (verification, streak, avatars, contests, dating, video).
7. Real schema/RLS unverified: export it and I'll check every query. Enable RLS on all tables (service key bypasses it, so Flask checks are the real guard).
8. Rotate your Supabase service key and JWT secret (they were in the uploaded zip).
9. Rate limiter is in-memory (per process); use Redis if you run multiple workers.

---
# Profile screening & compatibility (added)
Run `migration_screening.sql` in Supabase first (adds `users.screening_complete` + `screening_profiles`; no data touched).

**Flow:** signup (18+ and audience checks on server) → onboarding → 6-step screening → discovery unlocked. Existing accounts are routed to the screening on next login.
**Server rules (cannot be bypassed from the browser):** exact age from stored DOB (<18 → 403); gender/preference eligibility (`ELIGIBILITY_MODE=straight|open`, message shown, answers saved as *incomplete*); partner ages 18–99 and min ≤ max; every required answer + consent + 18+ confirmation before `complete`; discovery, likes and opening chats require both users complete, mutually eligible, inside each other's age range and with no dealbreaker hit (error text never reveals which).
**Scoring (deterministic, `SCREEN_WEIGHTS` in app.py, sum 100):** relationship goal 20, family plans 14, values 14, personality 10, marriage timeline 8, lifestyle 8, communication 8, monogamy 6, interests 6, location 6. Opposite goals cap the score at 60; opposite family plans cap it at 55. Top matching factors are returned as `compat_reasons` (shown on Discover cards) and `compat_factors` / report `factors`.
**Known limits:** location is same-city vs different-city (no geocoding), so the distance slider only influences the different-city score; photo/voice verification stays optional and human-reviewed; existing chats/matches formed before screening keep working.

---
# 21-day challenge, movie analysis & exclusive pairing (added)
Run `migration_challenge.sql` in Supabase first (additive; adds `users.challenge_complete`, `challenge_profiles`, `challenge_submissions`, `movie_logs`, `active_pairings`, `pairing_members` and the `create_exclusive_pairing` / `end_exclusive_pairing` functions). Code lives in `challenge.py` (registered at the end of `app.py`).
**Eligibility** = onboarding answers + 21 consecutive APPROVED local days + movie analysis (>=5 movies, >=3 genres, one genre rated twice). Set only by the server and permanent once earned. **Missed day = streak restarts at day 1.** Day rolls over at 04:00 local time; timezone changes are locked for 7 days; one live proof per day; the same file can't be resubmitted; proofs stay *pending/unverified* until an admin approves them (`/api/admin/challenge/pending`, `/api/admin/challenge/<id>/review`).
**Enforcement:** `_ready()` now includes `challenge_complete`, so discovery, likes, opening chats, profile view, compat and report endpoints all refuse incomplete users. `_gate()` also refuses anyone with an active pairing to someone else.
**Exclusivity:** a mutual like calls `create_exclusive_pairing` (advisory locks + partial unique index). Other pending likes/requests of both people are cancelled and affected users get a neutral notice. `/api/connections/<id>/end` frees both users.
**Known limits:** proof review is manual (no automated activity check); movie scoring is a local rules baseline (8% of the match score); matches created before this migration have no pairing row (not backfilled); `closeness.status='engaged'` still excludes a pair permanently (pre-existing); video calls still need your Daily.co keys; `tests/test_e2e_*` browser tests need Playwright.
