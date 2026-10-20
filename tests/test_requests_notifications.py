"""Two-account request + notification flows (in-memory fake Supabase, no network).
Fixture users: u1(man) and u2(woman) are an active match with conversation c1; u3(man) is NOT matched with u1; admin1(woman).
Run: pytest -q tests/test_requests_notifications.py
"""
import ast, glob, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(__file__))
import pytest
from test_api import env  # noqa: F401  (pytest fixture)

ROOT = os.path.dirname(os.path.dirname(__file__))
P = lambda c, url, h, body=None: c.post(url, json=body or {}, headers=h)
future = lambda days=2: (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def notes(db, uid, typ=None):
    return [n for n in db.t.get("notifications", []) if n["recipient_id"] == uid and (typ is None or n["type"] == typ)]


# ───────────────────────── likes / matches ─────────────────────────
def test_like_is_idempotent_for_notifications(env):
    m, db, c, h = env
    for _ in range(3):
        assert P(c, "/api/likes", h("u1"), {"liked_id": "u2"}).status_code == 200
    assert len(notes(db, "u2", "like")) == 1 and notes(db, "u1") == []        # one notification, only to the recipient
    r = P(c, "/api/likes", h("u2"), {"liked_id": "u1"})
    assert r.get_json()["data"]["is_mutual"] is True
    for _ in range(2):
        P(c, "/api/likes", h("u2"), {"liked_id": "u1"})
    assert len(notes(db, "u1", "match")) == 1 and len(notes(db, "u2", "match")) == 1   # no repeated "It's a Match" pop-ups
    assert len(db.t["likes"]) == 2


def test_unlike_withdraws_the_like_notification(env):
    m, db, c, h = env
    P(c, "/api/likes", h("u1"), {"liked_id": "u2"})
    assert len(c.get("/api/notifications/likes", headers=h("u2")).get_json()["data"]) == 1
    assert c.delete("/api/likes/u2", headers=h("u1")).status_code == 200
    assert c.get("/api/notifications/likes", headers=h("u2")).get_json()["data"] == []


def test_notifications_are_private_to_the_recipient(env):
    m, db, c, h = env
    P(c, "/api/likes", h("u1"), {"liked_id": "u2"})
    assert c.get("/api/notifications", headers=h("u3")).get_json()["data"] == []
    P(c, "/api/notifications/read", h("u3"))
    assert notes(db, "u2")[0]["is_read"] is False                               # u3 cannot mark u2's notifications read
    assert len(c.get("/api/notifications", headers=h("u2")).get_json()["data"]) == 1


# ───────────────────────── chat messages ─────────────────────────
def test_new_message_notifies_the_other_person_once(env):
    m, db, c, h = env
    for t in ("a", "b", "c"):
        assert P(c, "/api/conversations/c1/messages", h("u1"), {"content": t}).status_code == 201
    assert len(notes(db, "u2", "message")) == 1 and notes(db, "u1") == []
    assert "u1" in notes(db, "u2", "message")[0]["title"]
    P(c, "/api/notifications/read", h("u2"))
    P(c, "/api/conversations/c1/messages", h("u1"), {"content": "d"})
    assert len(notes(db, "u2", "message")) == 2                                 # new unread notification after the old one was read


# ───────────────────────── legacy phase reveal (chat "Reveal Phase" button) ─────────────────────────
def test_phase_reveal_request_reaches_only_the_other_person(env):
    m, db, c, h = env
    u = "/api/conversations/c1/reveal"
    assert P(c, u, h("u1"), {"action": "request"}).status_code == 200
    n = notes(db, "u2", "phase_reveal_request")
    assert len(n) == 1 and n[0]["actor_id"] == "u1" and "u1" in n[0]["title"]
    assert notes(db, "u1") == [] and notes(db, "u3") == []
    assert P(c, u, h("u1"), {"action": "request"}).status_code == 409           # duplicate
    assert len(notes(db, "u2", "phase_reveal_request")) == 1
    assert P(c, u, h("u2"), {"action": "request"}).status_code == 409           # must accept theirs instead
    assert P(c, u, h("u3"), {"action": "accept"}).status_code == 403            # outsider
    assert P(c, "/api/conversations/nope/reveal", h("u1"), {"action": "request"}).status_code == 403   # was an unhandled 500
    assert P(c, u, h("u1"), {"action": "bogus"}).status_code == 400
    assert P(c, u, h("u2"), {"action": "accept"}).status_code == 200
    assert db.t["conversations"][0]["phase"] == "voice"
    assert len(notes(db, "u1", "phase_reveal_accepted")) == 1
    assert all(x["is_read"] for x in notes(db, "u2", "phase_reveal_request"))   # handled request stops showing as unread


def test_phase_reveal_decline_notifies_requester_and_can_retry(env):
    m, db, c, h = env
    u = "/api/conversations/c1/reveal"
    P(c, u, h("u1"), {"action": "request"})
    assert P(c, u, h("u2"), {"action": "decline"}).status_code == 200
    assert len(notes(db, "u1", "phase_reveal_declined")) == 1 and db.t["conversations"][0]["phase"] == "anon"
    assert P(c, u, h("u1"), {"action": "request"}).status_code == 200


def test_phase_reveal_blocked_after_breakup(env):
    m, db, c, h = env
    assert P(c, "/api/connections/u2/end", h("u1")).status_code == 200
    assert P(c, "/api/conversations/c1/reveal", h("u1"), {"action": "request"}).status_code == 403


# ───────────────────────── photo reveal (already correct; locked in as a regression) ─────────────────────────
def test_photo_reveal_two_accounts(env):
    m, db, c, h = env
    assert P(c, "/api/reveal/request", h("u1"), {"receiver_id": "u3"}).status_code == 403          # no conversation with u3
    r = P(c, "/api/reveal/request", h("u1"), {"receiver_id": "u2"}); assert r.status_code == 201
    rid = r.get_json()["data"]["request_id"]
    assert P(c, "/api/reveal/request", h("u1"), {"receiver_id": "u2"}).status_code == 409
    assert P(c, "/api/reveal/respond", h("u1"), {"request_id": rid, "action": "accept"}).status_code == 403   # sender can't self-accept
    assert P(c, "/api/reveal/respond", h("u3"), {"request_id": rid, "action": "accept"}).status_code == 403   # outsider
    assert P(c, "/api/reveal/respond", h("u2"), {"request_id": rid, "action": "accept"}).status_code == 200
    assert len(notes(db, "u1", "reveal_accepted")) == 1 and all(x["is_read"] for x in notes(db, "u2", "reveal_request"))
    assert P(c, "/api/reveal/respond", h("u2"), {"request_id": rid, "action": "accept"}).status_code == 409   # no double-answer


# ───────────────────────── dating requests ─────────────────────────
def test_dating_request_two_accounts(env):
    m, db, c, h = env
    r = P(c, "/api/dating/request", h("u1"), {"target_id": "u2"}); assert r.status_code == 201
    rid = r.get_json()["data"]["id"]
    n = notes(db, "u2", "dating_request")
    assert len(n) == 1 and n[0]["actor_id"] == "u1" and "u1" in n[0]["title"]      # recipient sees WHO asked
    assert P(c, "/api/dating/request", h("u1"), {"target_id": "u2"}).status_code == 409
    assert P(c, "/api/dating/request", h("u2"), {"target_id": "u1"}).status_code == 409   # reverse direction is a duplicate too
    assert len(notes(db, "u2", "dating_request")) == 1 and notes(db, "u1") == []
    assert P(c, "/api/dating/request", h("u1"), {"target_id": "u3"}).status_code == 403   # wrong person (not a match)
    assert P(c, f"/api/dating/{rid}/respond", h("u3"), {"action": "accept"}).status_code == 404   # outsider
    assert P(c, f"/api/dating/{rid}/respond", h("u1"), {"action": "accept"}).status_code == 403   # sender
    assert c.get("/api/dating", headers=h("u3")).get_json()["data"] == []           # private list
    assert len(c.get("/api/dating", headers=h("u2")).get_json()["data"]) == 1
    assert P(c, f"/api/dating/{rid}/respond", h("u2"), {"action": "accept"}).status_code == 200
    assert db.t["dating_requests"][0]["status"] == "accepted"
    assert len(notes(db, "u1", "dating_accepted")) == 1 and "u2" in notes(db, "u1", "dating_accepted")[0]["title"]
    assert all(x["is_read"] for x in notes(db, "u2", "dating_request"))            # UI/notification state updated
    assert P(c, f"/api/dating/{rid}/respond", h("u2"), {"action": "accept"}).status_code == 409   # no double-processing


def test_dating_reject_then_sender_can_ask_again(env):
    m, db, c, h = env
    rid = P(c, "/api/dating/request", h("u1"), {"target_id": "u2"}).get_json()["data"]["id"]
    assert P(c, f"/api/dating/{rid}/respond", h("u2"), {"action": "reject"}).status_code == 200
    assert len(notes(db, "u1", "dating_rejected")) == 1
    assert P(c, "/api/dating/request", h("u1"), {"target_id": "u2"}).status_code == 201


def test_dating_request_rejects_bad_target(env):
    m, db, c, h = env
    for bad in (None, "", "x),and(a.eq.1", 123, "u1"):
        assert P(c, "/api/dating/request", h("u1"), {"target_id": bad}).status_code in (400, 403)
    assert not db.t.get("dating_requests")


# ───────────────────────── video-call requests ─────────────────────────
def test_video_invite_two_accounts(env):
    m, db, c, h = env
    r = P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"}); assert r.status_code == 201
    vid = r.get_json()["data"]["id"]
    n = notes(db, "u2", "video_invite")
    assert len(n) == 1 and n[0]["actor_id"] == "u1" and "u1" in n[0]["title"]
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"}).status_code == 409   # no invite spam
    assert P(c, "/api/video/schedule", h("u2"), {"guest_id": "u1"}).status_code == 409
    assert len(notes(db, "u2", "video_invite")) == 1 and len(db.t["video_dates"]) == 1
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u3"}).status_code == 403   # not matched
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u1"}).status_code == 403
    assert P(c, f"/api/video/{vid}/respond", h("u1"), {"action": "accept"}).status_code == 403   # host can't accept own invite
    assert P(c, f"/api/video/{vid}/respond", h("u3"), {"action": "accept"}).status_code == 404
    assert c.get("/api/video", headers=h("u3")).get_json()["data"] == []
    assert len(c.get("/api/video", headers=h("u2")).get_json()["data"]) == 1
    assert P(c, f"/api/video/{vid}/respond", h("u2"), {"action": "accept"}).status_code == 200
    assert db.t["video_dates"][0]["status"] == "accepted"
    assert len(notes(db, "u1", "video_accepted")) == 1 and "u2" in notes(db, "u1", "video_accepted")[0]["title"]
    assert all(x["is_read"] for x in notes(db, "u2", "video_invite"))
    assert P(c, f"/api/video/{vid}/respond", h("u2"), {"action": "accept"}).status_code == 403   # already answered


def test_video_invite_input_validation(env):
    m, db, c, h = env
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2", "scheduled_at": "not a date"}).status_code == 400
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2", "scheduled_at": future(-1)}).status_code == 400   # in the past
    db.t["conversations"].append({"id": "c-other", "user1_id": "u3", "user2_id": "admin1", "phase": "anon"})
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2", "conversation_id": "c-other"}).status_code == 400   # someone else's chat
    assert not db.t.get("video_dates")
    r = P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2", "conversation_id": "c1", "scheduled_at": future()})
    assert r.status_code == 201 and db.t["video_dates"][0]["conversation_id"] == "c1"
    assert "UTC" in notes(db, "u2", "video_invite")[0]["body"] or notes(db, "u2", "video_invite")[0]["body"]   # proposed time shown


def test_video_cancel_rules(env):
    m, db, c, h = env
    vid = P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"}).get_json()["data"]["id"]
    assert P(c, f"/api/video/{vid}/respond", h("u2"), {"action": "cancel"}).status_code == 403    # only host cancels
    assert P(c, f"/api/video/{vid}/respond", h("u2"), {"action": "reject"}).status_code == 200
    assert P(c, f"/api/video/{vid}/respond", h("u1"), {"action": "cancel"}).status_code == 409    # can't cancel a finished request
    assert db.t["video_dates"][0]["status"] == "rejected" and len(notes(db, "u2", "video_cancelled")) == 0
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"}).status_code == 201            # can ask again after a decline


# ───────────────────────── ending a connection ─────────────────────────
def test_ending_connection_closes_open_requests_and_clears_actionable_notifications(env):
    m, db, c, h = env
    P(c, "/api/dating/request", h("u1"), {"target_id": "u2"})
    P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"})
    P(c, "/api/conversations/c1/reveal", h("u1"), {"action": "request"})
    assert P(c, "/api/connections/u2/end", h("u1"), {"feedback": {"reason": "x"}}).status_code == 200
    assert db.t["dating_requests"][0]["status"] == "cancelled" and db.t["video_dates"][0]["status"] == "cancelled"
    for t in ("dating_request", "video_invite", "phase_reveal_request"):
        assert all(x["is_read"] for x in notes(db, "u2", t)), t
    assert len(notes(db, "u2", "connection_ended")) == 1
    assert "feedback" not in str(notes(db, "u2", "connection_ended")).lower().replace("connection_ended", "")
    assert P(c, "/api/dating/request", h("u1"), {"target_id": "u2"}).status_code == 403
    assert P(c, "/api/video/schedule", h("u1"), {"guest_id": "u2"}).status_code == 403


# ───────────────────────── contest reveal ─────────────────────────
def test_contest_reveal_notifications_do_not_collide_with_photo_reveal(env):
    m, db, c, h = env
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.t["contests"] = [{"id": "k1", "title": "t", "starts_at": past, "ends_at": past, "status": "completed"}]
    db.t["contest_entries"] = [{"id": "e1", "contest_id": "k1", "user_id": "u1"}, {"id": "e2", "contest_id": "k1", "user_id": "u2"}]
    r = P(c, "/api/contests/k1/reveal", h("u1"), {"target_id": "u2"}); assert r.status_code == 201
    assert P(c, "/api/contests/k1/reveal", h("u1"), {"target_id": "u2"}).status_code == 409
    assert P(c, "/api/contests/k1/reveal", h("u3"), {"target_id": "u2"}).status_code == 403          # didn't enter
    assert notes(db, "u2", "reveal_request") == [] and len(notes(db, "u2", "contest_reveal_request")) == 1
    rid = r.get_json()["data"]["id"]
    assert P(c, f"/api/contests/reveal/{rid}/respond", h("u1"), {"action": "accept"}).status_code == 404   # sender can't answer
    assert P(c, f"/api/contests/reveal/{rid}/respond", h("u2"), {"action": "accept"}).status_code == 200
    assert len(notes(db, "u1", "contest_reveal_accepted")) == 1 and notes(db, "u1", "reveal_accepted") == []
    assert all(x["is_read"] for x in notes(db, "u2", "contest_reveal_request"))


# ───────────────────────── feedback ─────────────────────────

def test_feedback_validation(env):
    m, db, c, h = env
    assert P(c, "/api/feedback", h("u1"), {"match_id": "u2", "rating": 5, "comment": "great"}).status_code == 200
    assert P(c, "/api/feedback", h("u1"), {"rating": "abc"}).status_code == 400
    assert P(c, "/api/feedback", h("u1"), {"rating": 99}).status_code == 400
    assert len(db.t["feedback"]) == 1


# ───────────────────────── schema contract (repo SQL vs. columns the code touches) ─────────────────────────
def _schema_columns():
    cols, depth0 = {}, lambda t: _split0(t)
    for f in sorted(glob.glob(os.path.join(ROOT, "*.sql"))):
        sql = re.sub(r"--[^\n]*", "", open(f, encoding="utf-8").read())
        for mt in re.finditer(r"create table if not exists (\w+)\s*\(", sql, re.I):
            i, d = mt.end(), 1
            while d:
                d += (sql[i] == "(") - (sql[i] == ")"); i += 1
            for part in _split0(sql[mt.end():i - 1]):
                w = part.strip().split()
                if w and w[0].lower() not in ("primary", "unique", "check", "foreign", "constraint"):
                    cols.setdefault(mt.group(1), set()).add(w[0].strip('"'))
        for mt in re.finditer(r"alter table (\w+) add column if not exists (\w+)", sql, re.I):
            cols.setdefault(mt.group(1), set()).add(mt.group(2))
    return cols


def _split0(t):
    out, depth, cur = [], 0, ""
    for ch in t:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0: out.append(cur); cur = ""
        else: cur += ch
    return out + [cur]


def _code_columns():
    """(table, op, column) for every supabase.table("t").select/insert/upsert/update(...) in app.py."""
    used, tree = set(), ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("select", "insert", "upsert", "update")):
            continue
        t = n.func.value
        if not (isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) and t.func.attr == "table" and t.args and isinstance(t.args[0], ast.Constant)):
            continue
        table, op = t.args[0].value, n.func.attr
        if op == "select" and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            for col in _split0(n.args[0].value):
                col = col.strip()
                if col and col != "*" and "(" not in col and ":" not in col:
                    used.add((table, op, col))
        elif op != "select" and n.args and isinstance(n.args[0], ast.Dict):
            used.update((table, op, k.value) for k in n.args[0].keys if isinstance(k, ast.Constant))
    return used


def test_every_column_the_code_uses_exists_in_the_repo_sql():
    schema, missing = _schema_columns(), []
    for table, op, col in sorted(_code_columns()):
        if col not in schema.get(table, set()):
            missing.append(f"{table}.{col} ({op})")
    assert not missing, "columns used by app.py but defined in no .sql file: " + ", ".join(missing)


# ───────────────────────── frontend source checks (no browser available in CI sandbox) ─────────────────────────
HTML = open(os.path.join(ROOT, "soulsync-ai.html"), encoding="utf-8").read()


def _js_fn(name):
    i = HTML.index(f"function {name}(")
    j = HTML.index("{", i); d, k = 0, j
    while True:
        d += (HTML[k] == "{") - (HTML[k] == "}")
        if d == 0: return HTML[i:k + 1]
        k += 1


def test_chat_video_button_passes_the_other_users_id():
    body = _js_fn("initiateCallFromChat")
    assert "startCall(null" not in body and "S.activeChatUserId" in body


def test_reveal_phase_button_reports_the_real_server_result():
    body = _js_fn("doReveal")
    assert "d.success" in body and "toast(d.error" in body


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_inline_scripts_have_valid_syntax(tmp_path):
    for i, js in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S)):
        f = tmp_path / f"s{i}.js"; f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:600]


# ───────────────────────── closeness / engagement ─────────────────────────
def _asking(c, h, db):
    """Push u1<->u2 to 100% (offset trick) and return after both were notified."""
    assert c.get("/api/closeness/u2", headers=h("u1")).status_code == 200
    row = [r for r in db.t["closeness"]][0]
    row["score_offset"] = 500
    d = c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]
    assert d["percent"] == 100 and d["status"] == "asking"


def test_closeness_starts_low_and_is_private_to_the_pair(env):
    m, db, c, h = env
    d = c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]
    assert 0 <= d["percent"] < 100 and d["status"] == "building" and d["name"]
    assert c.get("/api/closeness/u2", headers=h("u3")).status_code == 404          # not their connection


def test_hundred_percent_notifies_both_and_answers_need_that_state(env):
    m, db, c, h = env
    assert P(c, "/api/closeness/u2/answer", h("u1"), {"answer": "yes"}).status_code == 409   # too early
    _asking(c, h, db)
    assert len(notes(db, "u1", "closeness_100")) == 1 and len(notes(db, "u2", "closeness_100")) == 1
    c.get("/api/closeness/u2", headers=h("u1"))
    assert len(notes(db, "u1", "closeness_100")) == 1                                   # no repeat notification
    assert P(c, "/api/closeness/u2/answer", h("u1"), {"answer": "maybe"}).status_code == 400


def test_more_time_drops_to_50_and_notifies_other(env):
    m, db, c, h = env
    _asking(c, h, db)
    assert P(c, "/api/closeness/u1/answer", h("u2"), {"answer": "more_time"}).get_json()["data"]["percent"] == 50
    d = c.get("/api/closeness/u1", headers=h("u2")).get_json()["data"]
    assert d["percent"] == 50 and d["status"] == "building"
    assert len(notes(db, "u1", "closeness_more_time")) == 1


def test_break_up_ends_the_connection_for_good(env):
    m, db, c, h = env
    _asking(c, h, db)
    assert P(c, "/api/closeness/u2/answer", h("u1"), {"answer": "break_up"}).status_code == 200
    assert c.get("/api/closeness/u2", headers=h("u1")).status_code == 404               # connection ended
    assert len(notes(db, "u2", "connection_ended")) == 1


def test_both_yes_means_engaged_and_other_contacts_are_closed_and_told(env):
    m, db, c, h = env
    db.t["matches"].append({"user1_id": "u1", "user2_id": "u3", "status": "active"})   # u1 also had a contact with u3
    _asking(c, h, db)
    assert P(c, "/api/closeness/u2/answer", h("u1"), {"answer": "yes"}).get_json()["data"]["result"] == "waiting"
    assert notes(db, "u2", "engaged") == []                                             # a lone yes is never revealed
    assert P(c, "/api/closeness/u1/answer", h("u2"), {"answer": "yes"}).get_json()["data"]["result"] == "engaged"
    assert len(notes(db, "u1", "engaged")) == 1 and len(notes(db, "u2", "engaged")) == 1
    assert len(notes(db, "u3", "contact_engaged")) == 1
    assert P(c, "/api/closeness/u1/answer", h("u3"), {"answer": "yes"}).status_code == 404   # u3 can no longer reach u1
    assert c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]["status"] == "engaged"


# ───────────────────────── real-life dates ─────────────────────────
today = lambda: datetime.now(timezone.utc).date().isoformat()


def test_date_needs_both_to_confirm_and_adds_closeness(env):
    m, db, c, h = env
    base = c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]["percent"]
    r = P(c, "/api/dates", h("u1"), {"other_id": "u2", "date_on": today(), "kind": "coffee"})
    assert r.status_code == 201 and len(notes(db, "u2", "date_request")) == 1
    did = r.get_json()["data"]["id"]
    assert P(c, f"/api/dates/{did}/respond", h("u1"), {"action": "confirm"}).status_code == 403     # cannot confirm your own claim
    assert c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]["percent"] == base        # pending adds nothing
    assert P(c, f"/api/dates/{did}/respond", h("u2"), {"action": "confirm"}).status_code == 200
    d = c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]
    assert d["percent"] == base + 12 and d["dates"][0]["status"] == "confirmed"
    assert len(notes(db, "u1", "date_confirmed")) == 1


def test_date_rules(env):
    m, db, c, h = env
    assert P(c, "/api/dates", h("u1"), {"other_id": "u3", "date_on": today()}).status_code == 403   # not matched
    assert P(c, "/api/dates", h("u1"), {"other_id": "u2", "date_on": "2999-01-01"}).status_code == 400
    assert P(c, "/api/dates", h("u1"), {"other_id": "u2", "date_on": today()}).status_code == 201
    assert P(c, "/api/dates", h("u2"), {"other_id": "u1", "date_on": today()}).status_code == 409   # one waiting at a time


def test_date_feedback_is_private(env):
    m, db, c, h = env
    did = P(c, "/api/dates", h("u1"), {"other_id": "u2", "date_on": today()}).get_json()["data"]["id"]
    assert P(c, f"/api/dates/{did}/feedback", h("u1"), {"answer": "great"}).status_code == 409      # not confirmed yet
    P(c, f"/api/dates/{did}/respond", h("u2"), {"action": "confirm"})
    assert P(c, f"/api/dates/{did}/feedback", h("u2"), {"answer": "uncomfortable"}).get_json()["data"]["safety"] is True
    mine = c.get("/api/closeness/u1", headers=h("u2")).get_json()["data"]["dates"][0]["my_feedback"]
    theirs = c.get("/api/closeness/u2", headers=h("u1")).get_json()["data"]["dates"][0]["my_feedback"]
    assert mine == "uncomfortable" and theirs is None and notes(db, "u1", "date_feedback") == []


# ───────────────────────── safety: block / report / admin / password reset ─────────────────────────
def test_block_hides_both_ways_and_report_reaches_admin(env):
    m, db, c, h = env
    assert P(c, "/api/block/u2", h("u1")).status_code == 200
    assert c.get("/api/closeness/u2", headers=h("u1")).status_code == 404
    assert P(c, "/api/conversations/open", h("u2"), {"other_user_id": "u1"}).status_code == 403
    assert P(c, "/api/report", h("u1"), {"reported_id": "u2", "reason": "scam", "details": "asked for money"}).status_code == 201
    assert len(db.t["reports"]) == 1 and db.t["blocks"][0]["blocked_id"] == "u2"
    assert c.get("/api/admin/reports", headers=h("u1")).status_code == 403            # normal users cannot read reports


def test_reset_password_link_is_single_use_and_not_a_login_token(env, monkeypatch):
    m, db, c, h = env
    sent = []
    monkeypatch.setattr(m, "_send_email", lambda to, sub, body: sent.append(body))
    u1 = [u for u in db.t["users"] if u["id"] == "u1"][0]
    u1["email"], u1["password_hash"] = "u1@example.com", m.hash_password("old-password-1")
    email = u1["email"]
    assert P(c, "/api/auth/forgot", {}, {"email": "nobody@example.com"}).status_code == 200 and sent == []   # same answer, no email
    assert P(c, "/api/auth/forgot", {}, {"email": email}).status_code == 200 and len(sent) == 1
    tok = re.search(r"reset=(\S+)", sent[0]).group(1)
    assert c.get("/api/notifications", headers={"Authorization": "Bearer " + tok}).status_code == 401      # reset token cannot log in
    assert P(c, "/api/auth/reset", {}, {"token": tok, "password": "short"}).status_code == 400
    assert P(c, "/api/auth/reset", {}, {"token": tok, "password": "a-new-password-1"}).status_code == 200
    assert P(c, "/api/auth/reset", {}, {"token": tok, "password": "another-pass-22"}).status_code == 400   # link already used
    assert P(c, "/api/auth/login", {}, {"email": email, "password": "a-new-password-1"}).status_code == 200


def test_closeness_milestone_notification(env):
    m, db, c, h = env
    c.get("/api/closeness/u2", headers=h("u1"))
    [r for r in db.t["closeness"]][0]["score_offset"] = 60
    c.get("/api/closeness/u2", headers=h("u1"))
    assert len(notes(db, "u1", "closeness_milestone")) == 1 and len(notes(db, "u2", "closeness_milestone")) == 1