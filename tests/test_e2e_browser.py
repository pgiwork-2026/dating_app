"""Real-browser end-to-end tests (Playwright/Chromium) against Flask + an in-memory fake Supabase.
Run: pip install playwright pytest && playwright install chromium && pytest -q -s -p no:logging tests/test_e2e_browser.py"""
import os, sys, threading, importlib
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(__file__))
import pytest
pw = pytest.importorskip("playwright.sync_api")
import test_api  # sets env, provides FakeDB / scr_row
import supabase as _sb

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
R = []
def chk(name, cond): R.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)

class Env:
    def __init__(self):
        self.db = test_api.FakeDB(); _sb.create_client = lambda *a, **k: self.db
        import app as m; importlib.reload(m); m.supabase = self.db; self.m = m
        self.c = m.app.test_client(); self.ids, self.toks, self.errs = {}, {}, []
        from werkzeug.serving import make_server
        self.srv = make_server("127.0.0.1", 5000, m.app, threaded=True); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def user(self, nm, g, screened=True, dob="1998-01-01"):
        r = self.c.post("/api/auth/signup", json=dict(first_name=nm, last_name="T", email=f"{nm.lower()}@t.co", phone="1", password="password1", dob=dob,
                                                       gender=g, interested_in="woman" if g == "man" else "man")).get_json()
        assert r["success"], r
        uid = r["data"]["user"]["id"]; self.ids[nm], self.toks[nm] = uid, r["data"]["token"]
        u = next(x for x in self.db.t["users"] if x["id"] == uid); u["onboarded"] = True
        if screened:
            u["screening_complete"] = True; self.db.t.setdefault("screening_profiles", []).append(test_api.scr_row(uid, gender=u["gender"], interested_in=u["interested_in"]))
        return uid
    def U(self, nm): return next(u for u in self.db.t["users"] if u["id"] == self.ids[nm])
    def stop(self): self.srv.shutdown(); self.srv.server_close()

def browser(p, E, name, **ctx):
    ctx_ = p.new_context(**ctx); pg = ctx_.new_page()
    pg.on("pageerror", lambda e: E.errs.append(f"{name}: {e}")); pg.on("dialog", lambda d: d.accept(d.default_value or "Test"))
    pg.goto("http://127.0.0.1:5000/"); return pg
def login(pg, nm):
    pg.evaluate("showPage('login')"); pg.fill("#lemail", f"{nm.lower()}@t.co"); pg.fill("#lpass", "password1"); pg.click("text=Sign In →"); pg.wait_for_function("S.user && S.user.id", timeout=5000)
def toast_has(pg, t):
    try: pg.wait_for_function("t => document.getElementById('toastStack').innerText.includes(t)", arg=t, timeout=3000); return True
    except Exception: return False
def hub(pg):
    pg.click("nav >> text=Hub"); pg.wait_for_selector(".hub-card >> text=Streak"); pg.wait_for_timeout(500)

def test_hub_flows():
    E = Env(); db = E.db
    try:
        E.user("Riya", "woman"); E.user("Aarav", "man")
        for a, b in (("Riya", "Aarav"), ("Aarav", "Riya")):
            assert E.c.post("/api/likes", json={"liked_id": E.ids[b]}, headers={"Authorization": "Bearer " + E.toks[a]}).status_code == 200
        E.m.ADMIN_USER_IDS.add(E.ids["Aarav"]); stored = []; E.m._upload_private = lambda p, d, c_: stored.append((p, len(d)))
        for l in db.t["likes"]: l["created_at"] = "2020-01-01T00:00:00+00:00"
        with pw.sync_playwright() as p:
            br = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            A = browser(br, E, "riya", permissions=["microphone"]); login(A, "Riya"); chk("login via UI (screened user lands on app)", A.evaluate("S.user.first") == "Riya" and A.evaluate("!document.getElementById('page-screening').classList.contains('active')"))
            hub(A); txt = A.inner_text("#hubContent")
            for s in ["Streak", "Profile screening", "Animal avatar", "Verification", "Ideal partner", "Contests", "Your matches", "Dating requests", "Video dates"]: chk(f"hub section: {s}", s in txt)
            chk("non-admin sees no admin cards", "Admin" not in txt)
            A.click("#hubContent >> .hub-av[title=fox]"); A.wait_for_timeout(900); chk("avatar saved in DB", E.U("Riya").get("avatar") == "fox"); chk("avatar shown in header pill", A.inner_text("#pillAvatar").strip() == "🦊")
            A.click("#hubContent >> text=Count today's activity"); chk("streak blocked without real activity", toast_has(A, "Send a message or like"))
            db.t["messages"].append({"id": "mx", "conversation_id": "c", "sender_id": E.ids["Riya"], "content": "hi", "created_at": datetime.now(timezone.utc).isoformat()})
            A.click("#hubContent >> text=Count today's activity"); A.wait_for_timeout(900); chk("streak counted once activity exists", "1-day streak" in A.inner_text("#hubContent"))
            A.click("#hubContent >> text=Count today's activity"); A.wait_for_timeout(700); chk("streak not double-counted", db.t["user_streaks"][-1]["current_streak"] == 1)
            A.fill("#ipInterests", "music, travel"); A.fill("#ipGoals", "travel, family"); A.click("#hubContent >> button:has-text('Save')"); A.wait_for_timeout(900)
            chk("ideal partner saved + AI label", "AI-generated" in A.inner_text("#hubContent") and bool(db.t.get("ideal_partner_prefs")))
            open("/tmp/selfie.png", "wb").write(PNG); A.set_input_files("#vPhoto", "/tmp/selfie.png"); A.click("#hubContent >> text=Submit selfie"); A.wait_for_timeout(1200)
            chk("photo verification -> pending (never auto-verified)", E.U("Riya").get("verification_status") == "pending" and not E.U("Riya").get("is_verified"))
            A.click("#hubContent >> button:has-text('Get phrase')"); A.wait_for_function("document.getElementById('vPhrase').innerText.includes('Say:')", timeout=4000); chk("voice phrase shown", True)
            A.click("#hubContent >> button:has-text('Record')"); A.wait_for_timeout(1500); chk("recording started", "Recording" in A.inner_text("#vRecState"))
            A.click("#hubContent >> button:has-text('Stop')"); A.wait_for_timeout(2000)
            chk("voice submitted -> pending", any(v["kind"] == "voice" and v["status"] == "pending" for v in db.t["verification_requests"])); chk("selfie+voice stored privately", len(stored) == 2)
            A.click("#hubContent >> text=💕 Ask to date"); A.wait_for_timeout(900); chk("dating request created", db.t["dating_requests"][0]["status"] == "pending")
            A.click("#hubContent >> text=📹 Invite to video"); A.wait_for_timeout(900); chk("video invitation created", db.t["video_dates"][0]["status"] == "invited")
            B = browser(br, E, "aarav"); login(B, "Aarav"); B.wait_for_timeout(1800)
            nt = B.evaluate("notifications.map(n=>n.text).join('|')")
            chk("aarav sees like/match notification (polling fixed)", "liked your profile" in nt or "Match" in nt); chk("aarav sees video invite notification", "video date invitation" in nt)
            hub(B); chk("admin cards visible to admin", "Admin — pending verifications" in B.inner_text("#hubContent"))
            B.click(".hub-card:has-text('Dating requests') >> text=Accept"); B.wait_for_timeout(900); chk("dating accepted by receiver", db.t["dating_requests"][0]["status"] == "accepted")
            B.click(".hub-card:has-text('Video dates') >> text=Accept"); B.wait_for_timeout(900); chk("video accepted by guest", db.t["video_dates"][0]["status"] == "accepted")
            B.click("#hubContent >> text=Join call"); chk("join without provider shows clear error", toast_has(B, "not configured"))
            B.click(".hub-card:has-text('pending verifications') >> text=Approve >> nth=0"); B.wait_for_timeout(900)
            chk("admin approves photo -> verified", E.U("Riya").get("verification_status") == "verified" and E.U("Riya").get("is_verified") is True)
            now = datetime.now(); f = lambda d: d.strftime("%Y-%m-%dT%H:%M")
            B.fill("#ctTitle", "Goals Quiz"); B.fill("#ctRules", "Be honest"); B.fill("#ctStart", f(now - timedelta(hours=1))); B.fill("#ctEnd", f(now + timedelta(hours=1)))
            B.click("#hubContent >> button:has-text('Create')"); B.wait_for_timeout(900); chk("admin created contest", len(db.t.get("contests", [])) == 1)
            hub(A); chk("riya sees verified status", "verified" in A.inner_text("#hubContent").lower())
            A.click("#hubContent >> button:has-text('Join')"); A.wait_for_timeout(900); chk("contest joined", len(db.t.get("contest_entries", [])) == 1); chk("no duplicate join button", "✔ Entered" in A.inner_text("#hubContent"))
            A.click("nav >> text=Profile"); A.wait_for_timeout(500); A.click("#page-profile >> text=Edit >> nth=0"); A.wait_for_timeout(1000); chk("profile Edit button works", any(x.get("bio") for x in db.t["profiles"]))
            hub(A); A.set_viewport_size({"width": 375, "height": 700}); A.wait_for_timeout(300); chk("hub fits mobile width", A.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))
            A.click("#hubContent >> text=End connection"); A.wait_for_timeout(1200)
            chk("end connection -> match ended", db.t["matches"][0]["status"] == "ended"); chk("private breakup feedback stored", any(x["author_id"] == E.ids["Riya"] for x in db.t["breakup_feedback"]))
            A.click("nav >> text=Hub"); A.wait_for_timeout(900); chk("ended match hidden from list", "No mutual matches yet" in A.inner_text("#hubContent"))
            A.evaluate("logout()"); chk("logout clears token", A.evaluate("localStorage.getItem('ss_token')") is None)
            br.close()
    finally:
        E.stop()
    errs = [e for e in E.errs if "favicon" not in e]; chk("hub: no uncaught JS errors", not errs); print("JS errors:", errs)
    assert all(ok for _, ok in R), [n for n, ok in R if not ok]

def test_screening_wizard_end_to_end():
    E = Env(); db = E.db; R.clear()
    try:
        E.user("Meera", "woman"); E.user("Karan", "man")                       # two screened users to discover
        E.user("Legacy", "man", screened=False)                                 # existing account that never screened
        with pw.sync_playwright() as p:
            br = p.chromium.launch()
            L = browser(br, E, "legacy"); login(L, "Legacy"); L.wait_for_timeout(800)
            chk("unscreened existing user is sent to screening", L.evaluate("document.getElementById('page-screening').classList.contains('active')"))
            L.evaluate("showPage('matches')"); L.wait_for_timeout(300); chk("cannot open Discover before screening (UI gate)", L.evaluate("document.getElementById('page-screening').classList.contains('active')"))
            chk("direct API discovery empty before screening", L.evaluate("apiFetch('matches').then(r => r.success && r.data.length === 0)"))
            chk("direct API like blocked before screening", L.evaluate(f"apiFetch('likes','POST',{{liked_id:'{E.ids['Meera']}'}}).then(r => !r.success)"))
            L.fill("[data-k=gender]", "man") if False else None
            # step 1: validation + ineligible message
            L.click("#scrNext"); chk("step1 requires 18+ confirmation", "18 or older" in L.inner_text("#scrMsg"))
            L.check("[data-c=age_confirmed]"); L.select_option("[data-k=gender]", "man"); L.select_option("[data-k=interested_in]", "man"); L.click("#scrNext"); L.wait_for_timeout(700)
            chk("ineligible choice shows respectful message", "women and men" in L.inner_text("#scrMsg") and "Step 1 of 6" in L.inner_text("#scrLabel"))
            chk("ineligible answers saved as incomplete", E.U("Legacy").get("screening_complete") is not True)
            L.select_option("[data-k=interested_in]", "woman"); L.click("#scrNext"); L.wait_for_timeout(700); chk("moves to step 2 after fixing", "Step 2 of 6" in L.inner_text("#scrLabel"))
            L.click("#scrNext"); chk("step2 requires all answers", "every question" in L.inner_text("#scrMsg"))
            for k, v in (("relationship_goal", "marriage"), ("monogamy", "monogamous"), ("marriage_timeline", "1_2y"), ("children_plans", "want")): L.select_option(f"[data-k={k}]", v)
            L.click("#scrNext"); L.wait_for_timeout(700)
            L.fill("[data-k=age_pref_min]", "17"); L.fill("[data-k=age_pref_max]", "35"); L.fill("[data-k=city]", "Chennai"); L.click("#scrNext"); chk("under-18 partner age rejected (client)", "18" in L.inner_text("#scrMsg") and "Step 3 of 6" in L.inner_text("#scrLabel"))
            L.fill("[data-k=age_pref_min]", "40"); L.click("#scrNext"); chk("min > max rejected", "at least the minimum" in L.inner_text("#scrMsg"))
            chk("server also rejects under-18 range", L.evaluate("apiFetch('screening','PUT',{age_pref_min:17}).then(r => !r.success)"))
            L.fill("[data-k=age_pref_min]", "24"); L.check("[data-m=dealbreakers][value=smoker]"); L.click("#scrNext"); L.wait_for_timeout(700)
            L.click("#scrNext"); chk("step4 requires choices", "at least one" in L.inner_text("#scrMsg"))
            for k, v in (("lifestyle", "non_smoker"), ("values", "family"), ("values", "honesty"), ("interests", "music")): L.check(f"[data-m={k}][value={v}]")
            L.click("#scrNext"); L.wait_for_timeout(700)
            L.select_option("[data-k=comm_style]", "direct"); L.select_option("[data-k=emotional_readiness]", "ready"); L.click("#scrNext"); L.wait_for_timeout(700)
            chk("reached consent step", "Step 6 of 6" in L.inner_text("#scrLabel")); L.click("#scrNext"); chk("consent is mandatory", "accept" in L.inner_text("#scrMsg"))
            chk("still incomplete before submit", E.U("Legacy").get("screening_complete") is not True)
            for k in ("guidelines", "privacy", "safety"): L.check(f"[data-c={k}]")
            L.click("#scrNext"); L.wait_for_timeout(1500)
            chk("screening saved and marked complete", E.U("Legacy").get("screening_complete") is True)
            chk("lands on Discover after completion", L.evaluate("document.getElementById('page-matches').classList.contains('active')"))
            chk("answers persisted (goal/children/dealbreaker/age range)", (lambda s: s["relationship_goal"] == "marriage" and s["children_plans"] == "want" and s["dealbreakers"] == ["smoker"] and (s["age_pref_min"], s["age_pref_max"]) == (24, 35))(next(x for x in db.t["screening_profiles"] if x["user_id"] == E.ids["Legacy"])))
            L.wait_for_timeout(500); chk("discovery now returns eligible matches with reasons", L.evaluate("apiFetch('matches').then(r => r.success && r.data.length >= 1 && r.data.every(m => Array.isArray(m.compat_reasons)))"))
            L.set_viewport_size({"width": 375, "height": 700}); L.evaluate("showPage('screening')"); L.wait_for_timeout(600); chk("screening fits mobile width", L.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))
            # signup: under-18 blocked in browser and by server
            S_ = browser(br, E, "signup"); S_.evaluate("showPage('signup')")
            t = datetime.now(); young = f"{t.year - 17}-{t.month:02d}-{t.day:02d}"
            S_.fill("#sfirst", "Kid"); S_.fill("#semail", "kid@t.co"); S_.fill("#spass", "password1"); S_.fill("#sdob", young); S_.click("text=Create Account & Continue →"); S_.wait_for_timeout(600)
            chk("under-18 signup blocked in browser", "18" in S_.inner_text("#toastStack") and not any(u["email"] == "kid@t.co" for u in db.t["users"]))
            chk("under-18 signup blocked by server too", S_.evaluate(f"apiFetch('auth/signup','POST',{{first_name:'K',last_name:'I',email:'kid2@t.co',phone:'1',password:'password1',dob:'{young}',gender:'man',interested_in:'woman'}}).then(r => !r.success && r.error.includes('18'))"))
            br.close()
    finally:
        E.stop()
    errs = [e for e in E.errs if "favicon" not in e]; chk("screening: no uncaught JS errors", not errs); print("JS errors:", errs)
    assert all(ok for _, ok in R), [n for n, ok in R if not ok]
