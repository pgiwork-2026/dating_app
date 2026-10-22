"""Real two-browser test of the Couple Zone games (Playwright/Chromium, Flask + in-memory fake Supabase).
Run: pytest -q -s -p no:logging tests/test_e2e_couple_games.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import pytest
pw = pytest.importorskip("playwright.sync_api")
from test_e2e_browser import Env, browser, login, chk, R

def vis(pg, sel, t=6000):
    try: pg.wait_for_selector(sel, state="visible", timeout=t); return True
    except Exception: return False

def gp(pg): return pg.inner_text("#gpBody")

def start(A, B, game, name):
    A.evaluate(f"cgPick('{game}')")
    chk(f"{name}: inviter sees waiting room", vis(A, "#gamePanel.open") and "Waiting for" in gp(A))
    chk(f"{name}: partner receives invite banner live", vis(B, "#cgBanner .cg-banner.invite", 8000) or vis(B, "#gamePanel.open .cg-btn", 8000))
    B.evaluate("cgRespond('accept')")
    chk(f"{name}: both in active room", vis(A, "#gpBody .cg-head", 6000) and vis(B, "#gpBody .cg-head", 6000))

def leave(A, B):
    A.evaluate("cgExit(true)"); B.wait_for_function("!CG.room || CG.room.state.left_by", timeout=6000)
    B.evaluate("cgExit(true)"); B.wait_for_timeout(300)

def test_couple_games_two_browsers():
    E = Env(); db = E.db
    try:
        E.user("Riya", "woman"); E.user("Aarav", "man")
        for nm in ("Riya", "Aarav"): E.U(nm).update(verification_status="verified", voice_verified=True, is_verified=True)   # likes need verified users
        for a, b in (("Riya", "Aarav"), ("Aarav", "Riya")):
            assert E.c.post("/api/likes", json={"liked_id": E.ids[b]}, headers={"Authorization": "Bearer " + E.toks[a]}).status_code == 200
        with pw.sync_playwright() as p:
            br = p.chromium.launch()
            A = browser(br, E, "riya"); login(A, "Riya"); B = browser(br, E, "aarav"); login(B, "Aarav")
            for pg, other in ((A, "Aarav"), (B, "Riya")):
                pg.evaluate(f"openChatWithUser('{E.ids[other]}','{other}')"); pg.wait_for_function("S.activeChat && !String(S.activeChat).startsWith('local_')", timeout=8000)
                pg.evaluate("switchChatTab('zone')"); pg.wait_for_timeout(400)
            txt = A.inner_text("#coupleZoneWrap .games-grid")
            chk("exactly the 5 new games shown", all(t in txt for t in ["Tic-Tac-Toe", "Truth or Dare", "How Well Do You Know Me?", "Memory Match", "Couple Puzzle Challenge"]) and A.locator(".game-card").count() == 5)
            chk("old games removed", not any(t in txt for t in ["This or That", "Spin the Wheel", "Emoji Guess", "Draw Together", "Guess My Song"]))

            # ── Tic-Tac-Toe ──
            start(A, B, "tictactoe", "ttt")
            first, second = (A, B) if E.ids["Riya"] == db.t["couple_games"][0]["state"]["players"][0] else (B, A)
            chk("ttt: only the player whose turn it is can tap", first.locator(".cg-cell:not([disabled])").count() == 9 and second.locator(".cg-cell:not([disabled])").count() == 0)
            for pgx, cell in ((first, 0), (second, 3), (first, 1), (second, 4), (first, 2)):
                pgx.click(f".cg-cell >> nth={cell}"); pgx.wait_for_timeout(250)
            chk("ttt: win synced to both screens", vis(first, ".cg-result") and vis(second, ".cg-result") and "You won" in gp(first) and "wins this one" in gp(second))
            chk("ttt: score updated on both", "1" in first.inner_text(".cg-score") and "1" in second.inner_text(".cg-score"))
            first.click(".cg-result >> text=Rematch"); chk("ttt: rematch shows waiting for the partner", vis(first, ".cg-result >> text=Waiting for"))
            chk("ttt: partner is offered the rematch", vis(second, ".cg-result >> text=wants a rematch"))
            second.click(".cg-result >> .cg-btn >> text=Accept"); chk("ttt: rematch starts a fresh board for both", first.wait_for_function("CG.room && CG.room.status === 'active' && !CG.room.state.over", timeout=8000) is not None and vis(second, ".cg-board", 6000) and first.locator(".cg-cell.filled").count() == 0 and second.locator(".cg-cell.filled").count() == 0)
            chk("ttt: rematch swapped who starts", second.locator(".cg-cell:not([disabled])").count() == 9)
            second.click(".cg-link >> text=Leave game"); chk("ttt: exit tells the partner", vis(first, ".cg-result >> text=left the game", 6000) and first.locator(".cg-result >> text=Rematch").count() == 0)
            first.click(".cg-result >> text=Back to Couple Zone"); first.wait_for_timeout(400); chk("ttt: room closed, zone free again", not first.evaluate("cgOpen()") and first.evaluate("CG.room") is None)
            second.wait_for_timeout(2200)

            # ── Truth or Dare ──
            start(A, B, "truthdare", "td")
            row = lambda: db.t["couple_games"][-1]
            hot, other = (A, B) if row()["state"]["turn"] == E.ids["Riya"] else (B, A)
            hot.click(".cg-btn >> text=Truth"); chk("td: prompt synced to both", vis(other, ".cg-q") and gp(hot).split("\n")[0] and row()["state"]["prompt"] in gp(other))
            hot.fill("#cgTdText", "You make ordinary days feel special"); hot.click("text=Send answer"); chk("td: answer reaches partner", vis(other, ".cg-ans >> text=ordinary days"))
            other.click(".cg-btn >> text=Love it"); chk("td: heart counted + turn swapped", other.wait_for_function("CG.room.state.hearts[CG.room.partner_id] === 1 && CG.room.state.turn === CG.room.you", timeout=6000) is not None and vis(other, "text=YOUR TURN"))
            other.click(".cg-btn >> text=Dare"); other.click(".cg-btn >> text=Skip"); chk("td: skip works and is limited", vis(hot, ".cg-ans >> text=Skipped") and row()["state"]["skips"][other.evaluate("CG.room.you")] == 1)
            leave(A, B)

            # ── How Well Do You Know Me ──
            A.wait_for_timeout(1800); start(A, B, "knowme", "knowme")
            subj = A if row()["state"]["subject"] == E.ids["Riya"] else B; guesser = B if subj is A else A
            subj.click(".cg-opt >> nth=1"); chk("knowme: guesser can't see the real answer yet", vis(guesser, ".cg-opt:not([disabled])") and guesser.locator(".cg-opt.you,.cg-opt.hit").count() == 0)
            guesser.click(".cg-opt >> nth=1"); chk("knowme: matching guess is revealed to both + scores a point", vis(subj, ".cg-verdict.ok") and vis(guesser, ".cg-verdict.ok") and row()["state"]["scores"][guesser.evaluate("CG.room.you")] == 1)
            subj.click("text=Next question"); chk("knowme: roles swap next round", vis(guesser, "text=Pick YOUR real answer"))
            leave(A, B)

            # ── Memory Match ──
            A.wait_for_timeout(1800); start(A, B, "memory", "memory")
            cards = row()["state"]["cards"]; pairs = {}
            [pairs.setdefault(e, []).append(i) for i, e in enumerate(cards)]; pl = list(pairs.values())
            mover, watcher = (A, B) if row()["state"]["turn"] == E.ids["Riya"] else (B, A)
            chk("memory: hidden cards never shown before flipping", watcher.locator(".cg-mcard.open").count() == 0)
            mover.click(f".cg-mcard >> nth={pl[0][0]}"); mover.click(f".cg-mcard >> nth={pl[1][0]}")
            chk("memory: mismatch visible to partner, then flips back and turn passes", vis(watcher, ".cg-mcard.open", 3000) and (watcher.wait_for_function("!CG.room.state.pending && CG.room.state.turn === CG.room.you", timeout=6000) is not None))
            for pr in pl:
                watcher.click(f".cg-mcard >> nth={pr[0]}"); watcher.wait_for_timeout(200); watcher.click(f".cg-mcard >> nth={pr[1]}"); watcher.wait_for_timeout(250)
            chk("memory: board cleared, winner shown to both", vis(watcher, ".cg-result >> text=You won") and vis(mover, ".cg-result >> text=wins this one"))
            leave(A, B)

            # ── Couple Puzzle Challenge ──
            A.wait_for_timeout(1800); start(A, B, "puzzle", "puzzle")
            row()["state"]["tiles"] = [1, 2, 3, 4, 5, 6, 7, 0, 8]
            row()["version"] += 1; A.wait_for_timeout(2000)
            mover, watcher = (A, B) if row()["state"]["turn"] == E.ids["Riya"] else (B, A)
            chk("puzzle: only movable tiles are enabled on your turn", mover.locator(".cg-tile.movable").count() == 3 and watcher.locator(".cg-tile.movable").count() == 0)
            mover.click(".cg-tile.movable >> text=8"); chk("puzzle: solved together shown to both", vis(mover, ".cg-result >> text=Solved together") and vis(watcher, ".cg-result >> text=Solved together"))
            leave(A, B)
            chk("no JS errors in either browser", not E.errs)
            if E.errs: print(E.errs)
            br.close()
    finally:
        E.stop()
    bad = [n for n, ok in R if not ok]
    assert not bad, bad

