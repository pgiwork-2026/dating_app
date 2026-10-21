"""Two real browsers (sender + receiver) through the photo reveal flow. Run: pytest -q -s -p no:logging tests/test_e2e_reveal.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import pytest
pw = pytest.importorskip("playwright.sync_api")
from test_e2e_browser import Env, browser, login, toast_has, chk, R

PHOTO = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

def test_photo_reveal_end_to_end():
    E = Env(); db = E.db
    try:
        for nm, g in (("Riya", "woman"), ("Aarav", "man")):
            uid = E.user(nm, g); u = E.U(nm); u["verification_status"] = "verified"; u["voice_verified"] = True; u["challenge_complete"] = True
            prof = next((r for r in db.t.setdefault("profiles", []) if r.get("user_id") == uid), None)
            if prof is None: prof = {"user_id": uid}; db.t["profiles"].append(prof)
            prof.update({"bio": "hello there friend", "city": "Chennai", "photos": [PHOTO], "anonymous_mode": False})   # signup already created the row
        riya, aarav = E.ids["Riya"], E.ids["Aarav"]
        with pw.sync_playwright() as p:
            br = p.chromium.launch()
            A = browser(br, E, "riya"); login(A, "Riya")
            B = browser(br, E, "aarav"); login(B, "Aarav")

            # --- sender opens a chat; the bug was S.activeChatUserId staying null ---
            A.evaluate(f"openChatWithUser('{aarav}', 'Aarav')")
            A.wait_for_function("S.activeChat && !String(S.activeChat).startsWith('local_')", timeout=5000)
            chk("sender: active chat user id is set", A.evaluate("S.activeChatUserId") == aarav)
            A.wait_for_function("document.getElementById('revealRequestBtn').style.display === 'flex'", timeout=5000)
            chk("sender: reveal button visible before any request", True)
            A.click("#revealRequestBtn"); A.wait_for_selector("#photoRevealModal.open"); A.click("text=Send Request")
            chk("sender: success toast", toast_has(A, "request sent"))
            reqs = db.t.get("photo_reveal_requests", [])
            chk("DB has one pending request sender->receiver", len(reqs) == 1 and reqs[0]["sender_id"] == riya and reqs[0]["receiver_id"] == aarav and reqs[0]["status"] == "pending")
            chk("receiver got a notification row", any(n["recipient_id"] == aarav and n["type"] == "reveal_request" for n in db.t.get("notifications", [])))
            A.wait_for_function("document.getElementById('revealBanner').innerText.includes('Waiting')", timeout=5000)
            chk("sender: banner says waiting, request button hidden", A.evaluate("document.getElementById('revealRequestBtn').style.display") == "none")

            # --- receiver sees the request + photos are locked ---
            B.wait_for_function("notifications.some(n => n.revealName)", timeout=15000)
            chk("receiver: in-app notification appears", True)
            B.evaluate("toggleNotifPanel()"); B.click(".notif-item >> nth=0")
            B.wait_for_function(f"S.activeChatUserId === '{riya}' && S.reveal.state === 'pending_received'", timeout=8000)
            B.wait_for_selector("#revealBanner button.rb-yes", timeout=5000)
            chk("receiver: tapping notification opens chat with Accept/Decline", "wants to reveal" in B.inner_text("#revealBanner"))
            B.evaluate(f"openPViewById('{riya}')"); B.wait_for_selector("#pviewOverlay.open"); B.wait_for_function("document.getElementById('pvLockNote').style.display === 'block'", timeout=5000)
            chk("receiver: profile photos locked before accept", B.evaluate("document.querySelectorAll('#pvHead img').length") == 0)
            B.evaluate("document.getElementById('pviewOverlay').classList.remove('open')")

            # --- accept ---
            B.click("#revealBanner button.rb-yes")
            chk("receiver: accept toast", toast_has(B, "revealed"))
            B.wait_for_function("S.reveal.state === 'revealed'", timeout=5000)
            chk("DB permission row created", any(r.get("enabled") for r in db.t.get("photo_reveal_permissions", [])))
            B.evaluate(f"openPViewById('{riya}')"); B.wait_for_function("document.querySelectorAll('#pvHead img').length > 0", timeout=5000)
            chk("receiver: can now view sender's photos", True)

            # --- sender flips to revealed on its own (polling) and can view receiver's photos ---
            A.wait_for_function("S.reveal.state === 'revealed'", timeout=15000)
            chk("sender: banner flips to revealed without refresh", "Photos revealed" in A.inner_text("#revealBanner"))
            A.evaluate(f"openPViewById('{aarav}')"); A.wait_for_function("document.querySelectorAll('#pvHead img').length > 0", timeout=5000)
            chk("sender: can now view receiver's photos", True)
            # --- second entry path: open the chat from the chat list (used to leave the user id null) ---
            A.evaluate("document.getElementById('pviewOverlay').classList.remove('open')")
            A.evaluate("showPage('chat'); S.activeChat = null; S.activeChatUserId = null; S.activeChatName = null; renderChatList()")
            A.wait_for_selector(".chat-item", timeout=5000); A.click(".chat-item >> nth=0")
            A.wait_for_function(f"S.activeChatUserId === '{aarav}'", timeout=5000)
            chk("chat list: opening a chat sets the right user id + name", A.inner_text("#chName").startswith("Aarav"))
            A.wait_for_function("S.reveal.state === 'revealed'", timeout=8000)
            chk("chat list: reveal state loads for that chat", "Photos revealed" in A.inner_text("#revealBanner"))
            # --- ending the match ---
            A.click('.icon-btn[title="End match"]'); A.wait_for_selector("#endMatchModal.open")
            A.click("text=I don't feel comfortable"); A.click("#endMatchBtn")
            A.wait_for_function("S.activeChat === null", timeout=5000)
            chk("end match: sender's chat closes", A.inner_text("#chName") == "Select a chat")
            mt = [m_ for m_ in db.t["matches"] if m_.get("status") == "ended"]
            chk("end match: match marked ended in DB", len(mt) == 1)
            chk("end match: private reason stored", any(f["reason"] == "Not comfortable" and f["author_id"] == riya for f in db.t.get("breakup_feedback", [])))
            chk("end match: photo access revoked", not any(r.get("enabled") for r in db.t["photo_reveal_permissions"]))
            A.wait_for_function("!document.querySelector('.chat-item')", timeout=5000)
            chk("end match: chat gone from sender's list", True)
            B.wait_for_function(f"S.activeChat === null && notifications.some(n => n.icon === '💔')", timeout=20000)
            chk("end match: receiver's open chat closes and they are notified", B.inner_text("#chName") == "Select a chat")
            B.evaluate(f"openPViewById('{riya}')"); B.wait_for_function("document.getElementById('pvLockNote').style.display === 'block'", timeout=5000)
            chk("end match: photos locked again for the receiver", B.evaluate("document.querySelectorAll('#pvHead img').length") == 0)
            chk("no page errors", not E.errs)
            if E.errs: print(E.errs)
            br.close()
    finally:
        E.stop()
    bad = [n for n, ok_ in R if not ok_]
    assert not bad, bad
