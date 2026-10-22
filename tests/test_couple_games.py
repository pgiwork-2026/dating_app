"""Couple Zone game rooms against the in-memory fake Supabase. Run: pytest -q tests/test_couple_games.py"""
import time
from test_api import env  # noqa: F401  (fixture)

def mk(env, game="tictactoe"):
    m, db, c, h = env
    r = c.post("/api/couple-games/with/u2/invite", json={"game": game}, headers=h("u1"))
    assert r.status_code == 201, r.get_json()
    gid = r.get_json()["data"]["id"]
    return m, db, c, h, gid

def accept(c, h, gid, who="u2"):
    r = c.post(f"/api/couple-games/{gid}/respond", json={"action": "accept"}, headers=h(who))
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]["game"]

def mv(c, h, gid, who, **d):
    return c.post(f"/api/couple-games/{gid}/move", json=d, headers=h(who))

def st(c, h, gid, who="u1"):
    return c.get(f"/api/couple-games/{gid}", headers=h(who)).get_json()["data"]["game"]

def test_invite_privacy_and_lifecycle(env):
    m, db, c, h = env
    assert c.post("/api/couple-games/with/u2/invite", json={"game": "tictactoe"}).status_code == 401
    assert c.post("/api/couple-games/with/u2/invite", json={"game": "chess"}, headers=h("u1")).status_code == 400
    assert c.post("/api/couple-games/with/u3/invite", json={"game": "memory"}, headers=h("u1")).status_code == 403   # not matched
    assert c.post("/api/couple-games/with/u1/invite", json={"game": "memory"}, headers=h("u1")).status_code == 403   # self
    m, db, c, h, gid = mk(env)
    assert c.post("/api/couple-games/with/u2/invite", json={"game": "memory"}, headers=h("u1")).status_code == 409   # one open room
    assert c.get(f"/api/couple-games/{gid}", headers=h("u3")).status_code == 404                                    # outsider can't peek
    assert mv(c, h, gid, "u3", cell=0).status_code == 404
    assert c.post(f"/api/couple-games/{gid}/respond", json={"action": "accept"}, headers=h("u1")).status_code == 403  # own invite
    assert c.get("/api/couple-games/with/u1", headers=h("u2")).get_json()["data"]["game"]["status"] == "invited"
    g = accept(c, h, gid)
    assert g["status"] == "active" and g["state"]["turn"] == "u1"
    assert c.post(f"/api/couple-games/{gid}/respond", json={"action": "accept"}, headers=h("u2")).status_code == 409  # already started

def test_decline_and_cancel(env):
    m, db, c, h, gid = mk(env)
    assert c.post(f"/api/couple-games/{gid}/respond", json={"action": "decline"}, headers=h("u2")).status_code == 200
    assert c.get("/api/couple-games/with/u2", headers=h("u1")).get_json()["data"]["game"] is None
    r = c.post("/api/couple-games/with/u2/invite", json={"game": "puzzle"}, headers=h("u1")); gid = r.get_json()["data"]["id"]
    assert c.post(f"/api/couple-games/{gid}/respond", json={"action": "cancel"}, headers=h("u2")).status_code == 403
    assert c.post(f"/api/couple-games/{gid}/respond", json={"action": "cancel"}, headers=h("u1")).status_code == 200

def test_connection_ended_blocks_games(env):
    m, db, c, h, gid = mk(env)
    accept(c, h, gid)
    db.t["matches"][0]["status"] = "ended"
    assert mv(c, h, gid, "u1", cell=0).status_code == 403
    assert c.get(f"/api/couple-games/{gid}", headers=h("u2")).status_code == 403

def test_tictactoe_full_game_win_draw_rematch(env):
    m, db, c, h, gid = mk(env)
    accept(c, h, gid)
    assert mv(c, h, gid, "u2", cell=0).status_code == 409           # not your turn
    assert mv(c, h, gid, "u1", cell=9).status_code == 400
    assert mv(c, h, gid, "u1", cell=0).status_code == 200
    assert mv(c, h, gid, "u2", cell=0).status_code == 400           # taken
    for who, cell in (("u2", 3), ("u1", 1), ("u2", 4), ("u1", 2)):
        assert mv(c, h, gid, who, cell=cell).status_code == 200
    g = st(c, h, gid)
    assert g["status"] == "finished" and g["state"]["result"] == {"winner": "u1", "line": [0, 1, 2]}
    assert g["state"]["series"]["wins"] == {"u1": 1}
    assert mv(c, h, gid, "u2", cell=5).status_code == 409           # game over
    # rematch needs BOTH partners; the other partner then starts
    r1 = c.post(f"/api/couple-games/{gid}/rematch", json={}, headers=h("u1")).get_json()["data"]["game"]
    assert r1["id"] == gid and r1["state"]["rematch"] == ["u1"]
    r2 = c.post(f"/api/couple-games/{gid}/rematch", json={}, headers=h("u2")).get_json()["data"]["game"]
    assert r2["id"] != gid and r2["status"] == "active" and r2["state"]["turn"] == "u2" and r2["state"]["series"]["wins"] == {"u1": 1}
    assert st(c, h, gid)["state"]["next_id"] == r2["id"]            # first voter's poll can jump to the new room
    gid2 = r2["id"]
    for who, cell in (("u2", 0), ("u1", 1), ("u2", 2), ("u1", 4), ("u2", 3), ("u1", 5), ("u2", 7), ("u1", 6), ("u2", 8)):
        assert mv(c, h, gid2, who, cell=cell).status_code == 200
    assert st(c, h, gid2)["state"]["result"]["winner"] == "draw"

def test_poll_unchanged_and_exit(env):
    m, db, c, h, gid = mk(env)
    g = accept(c, h, gid)
    r = c.get(f"/api/couple-games/{gid}?v={g['version']}", headers=h("u1")).get_json()["data"]
    assert r == {"unchanged": True, "version": g["version"]}
    mv(c, h, gid, "u1", cell=4)
    assert "game" in c.get(f"/api/couple-games/{gid}?v={g['version']}", headers=h("u2")).get_json()["data"]
    assert c.post(f"/api/couple-games/{gid}/exit", json={}, headers=h("u2")).status_code == 200
    g = st(c, h, gid)
    assert g["status"] == "finished" and g["state"]["left_by"] == "u2"
    assert c.post(f"/api/couple-games/{gid}/rematch", json={}, headers=h("u1")).status_code == 409   # partner left
    assert c.get("/api/couple-games/with/u2", headers=h("u1")).get_json()["data"]["game"] is None   # room closed, can invite again
    assert c.post("/api/couple-games/with/u2/invite", json={"game": "memory"}, headers=h("u1")).status_code == 201

def test_stale_invite_expires(env):
    m, db, c, h, gid = mk(env)
    db.t["couple_games"][0]["updated_at"] = "2020-01-01T00:00:00+00:00"
    assert c.get("/api/couple-games/with/u1", headers=h("u2")).get_json()["data"]["game"] is None
    assert db.t["couple_games"][0]["status"] == "cancelled"

def test_truth_or_dare_flow(env):
    m, db, c, h, gid = mk(env, "truthdare")
    accept(c, h, gid)
    assert mv(c, h, gid, "u2", type="choose", kind="truth").status_code == 409
    assert mv(c, h, gid, "u1", type="choose", kind="spicy").status_code == 400
    g = mv(c, h, gid, "u1", type="choose", kind="truth").get_json()["data"]["game"]["state"]
    assert g["phase"] == "answer" and g["prompt"]
    assert mv(c, h, gid, "u1", type="answer", text="").status_code == 400            # truth needs an answer
    assert mv(c, h, gid, "u1", type="answer", text="x" * 301).status_code == 400
    assert mv(c, h, gid, "u1", type="answer", text="You made me laugh").status_code == 200
    assert st(c, h, gid, "u2")["state"]["answer"] == "You made me laugh"
    assert mv(c, h, gid, "u1", type="react", heart=True).status_code == 409          # only the partner reacts
    assert mv(c, h, gid, "u2", type="react", heart=True).status_code == 200
    s = st(c, h, gid)["state"]
    assert s["turn"] == "u2" and s["round"] == 2 and s["hearts"]["u1"] == 1 and s["phase"] == "choose"
    mv(c, h, gid, "u2", type="choose", kind="dare")
    for _ in range(2):                                                                # skips are limited
        assert mv(c, h, gid, "u2", type="answer", skip=True).status_code == 200
        mv(c, h, gid, "u1", type="react", heart=False)
        mv(c, h, gid, "u1", type="choose", kind="dare"); mv(c, h, gid, "u1", type="answer", text="done"); mv(c, h, gid, "u2", type="react")
        mv(c, h, gid, "u2", type="choose", kind="dare")
    assert mv(c, h, gid, "u2", type="answer", skip=True).status_code == 400
    # run the game to its end
    for _ in range(40):
        s = st(c, h, gid)
        if s["status"] == "finished": break
        t, o = s["state"]["turn"], ("u1" if s["state"]["turn"] == "u2" else "u2")
        ph = s["state"]["phase"]
        if ph == "choose": mv(c, h, gid, t, type="choose", kind="dare")
        elif ph == "answer": mv(c, h, gid, t, type="answer", text="ok")
        else: mv(c, h, gid, o, type="react", heart=True)
    s = st(c, h, gid)
    assert s["status"] == "finished" and s["state"]["round"] == 10

def test_know_me_hides_answer_until_guess(env):
    m, db, c, h, gid = mk(env, "knowme")
    g = accept(c, h, gid)["state"]
    assert g["subject"] == "u1" and len(g["options"]) == 4 and g["phase"] == "answer"
    assert mv(c, h, gid, "u2", type="answer", choice=0).status_code == 409           # only the subject answers
    assert mv(c, h, gid, "u1", type="answer", choice=7).status_code == 400
    assert mv(c, h, gid, "u1", type="answer", choice=2).status_code == 200
    assert st(c, h, gid, "u2")["state"]["subject_answer"] is None                    # guesser can't see it
    assert st(c, h, gid, "u1")["state"]["subject_answer"] == 2
    assert mv(c, h, gid, "u1", type="guess", choice=2).status_code == 409
    g = mv(c, h, gid, "u2", type="guess", choice=2).get_json()["data"]["game"]["state"]
    assert g["phase"] == "reveal" and g["scores"]["u2"] == 1 and g["subject_answer"] == 2
    g = mv(c, h, gid, "u1", type="next").get_json()["data"]["game"]["state"]
    assert g["subject"] == "u2" and g["round"] == 1 and g["phase"] == "answer"
    for _ in range(40):                                                              # finish: every guess correct for u1
        s = st(c, h, gid)
        if s["status"] == "finished": break
        s = s["state"]; sub = s["subject"]; gu = "u1" if sub == "u2" else "u2"
        if s["phase"] == "answer": mv(c, h, gid, sub, type="answer", choice=1)
        elif s["phase"] == "guess": mv(c, h, gid, gu, type="guess", choice=1 if gu == "u1" else 0)
        else: mv(c, h, gid, "u1", type="next")
    s = st(c, h, gid)
    assert s["status"] == "finished" and s["state"]["result"]["winner"] in ("u1", "u2", "draw")

def test_memory_match_hidden_cards_turns_and_win(env):
    m, db, c, h, gid = mk(env, "memory")
    g = accept(c, h, gid)["state"]
    assert g["cards"] == [None] * 16                                                  # nothing leaks
    cards = db.t["couple_games"][0]["state"]["cards"]
    pairs = {}
    for i, e in enumerate(cards): pairs.setdefault(e, []).append(i)
    pl = list(pairs.values())
    assert mv(c, h, gid, "u2", idx=0).status_code == 409
    a, b = pl[0]
    assert mv(c, h, gid, "u1", idx=a).status_code == 200
    assert mv(c, h, gid, "u1", idx=a).status_code == 400                              # same card twice
    seen = st(c, h, gid, "u2")["state"]["cards"]; assert seen[a] == cards[a] and seen.count(None) == 15
    x = pl[1][0]
    mv(c, h, gid, "u1", idx=x)                                                        # mismatch
    s = st(c, h, gid)["state"]
    assert s["pending"] and s["turn"] == "u1"
    assert mv(c, h, gid, "u1", idx=pl[2][0]).status_code == 409                       # must wait for the flip-back
    time.sleep(1.1)
    s = st(c, h, gid)["state"]
    assert not s["pending"] and s["flipped"] == [] and s["turn"] == "u2"              # settled for both clients
    # u2 clears the board pair by pair and keeps the turn after each match
    for p in pl:
        assert mv(c, h, gid, "u2", idx=p[0]).status_code == 200
        r = mv(c, h, gid, "u2", idx=p[1]); assert r.status_code == 200
    s = st(c, h, gid)
    assert s["status"] == "finished" and s["state"]["result"]["winner"] == "u2" and s["state"]["scores"]["u2"] == 8

def test_puzzle_alternating_cooperative(env):
    m, db, c, h, gid = mk(env, "puzzle")
    g = accept(c, h, gid)["state"]
    tiles = g["tiles"]; assert sorted(tiles) == list(range(9)) and tiles != list(range(1, 9)) + [0]
    assert mv(c, h, gid, "u2", tile=0).status_code == 409
    blank = tiles.index(0)
    far = next(i for i in range(9) if i != blank and abs(i // 3 - blank // 3) + abs(i % 3 - blank % 3) > 1)
    assert mv(c, h, gid, "u1", tile=far).status_code == 400                           # illegal slide
    near = next(i for i in range(9) if abs(i // 3 - blank // 3) + abs(i % 3 - blank % 3) == 1)
    r = mv(c, h, gid, "u1", tile=near).get_json()["data"]["game"]["state"]
    assert r["moves"] == 1 and r["turn"] == "u2"
    # force a one-move-from-solved board and let the partner finish it
    row = db.t["couple_games"][0]; row["state"]["tiles"] = [1, 2, 3, 4, 5, 6, 7, 0, 8]; row["state"]["turn"] = "u2"
    assert mv(c, h, gid, "u2", tile=8).status_code == 200
    s = st(c, h, gid)
    assert s["status"] == "finished" and s["state"]["result"]["coop"] is True and s["state"]["series"]["best_moves"] == 2
