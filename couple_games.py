"""SoulSync Couple Zone: five two-player games played in private rooms between two connected partners.

Registered from app.py with `couple_games.register(app, globals())` (same pattern as challenge.py), so it reuses app.py's
JWT auth, helpers and Supabase client. Run migration_couple_games.sql first.

* A room (row in `couple_games`) belongs to exactly one pair and is only readable / writable by those two users, and only
  while they still have a chat and the connection has not ended. One open room (invited / active) per pair.
* The server is authoritative: every move is validated here (turn order, legal move, phase). Hidden information
  (Memory Match cards, How Well Do You Know Me answers) never leaves the server until the game says it is revealed.
* Sync = polling (the app's existing realtime model): clients poll GET /api/couple-games/<id>?v=<version> every ~1.5s and
  get `unchanged` until the version moves. Writes use optimistic locking on `version`, so two simultaneous moves can't both win.
"""
import random
import time

from flask import request

GAMES = ("tictactoe", "truthdare", "knowme", "memory", "puzzle")
INVITE_TTL = 10 * 60          # an unanswered invite expires
IDLE_TTL = 2 * 60 * 60        # an abandoned active room closes itself
MEMORY_HIDE_AFTER = 1.0       # seconds a mismatched pair stays visible before it is turned back
TD_ROUNDS, KNOW_ROUNDS = 10, 8
TD_MAX_SKIPS = 2

LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
MEMORY_EMOJI = ["💖", "🌹", "💌", "🥂", "🌙", "🎶", "🍫", "✨"]

TRUTHS = [
    "What was your first impression of me?",
    "What's a small thing I do that makes you smile?",
    "What's your favourite memory from our conversations so far?",
    "What does a perfect lazy Sunday look like for you?",
    "What's something you're proud of that you rarely talk about?",
    "What's a song that always makes you think of someone special?",
    "What's one thing you'd love for us to do together someday?",
    "When do you feel most loved?",
    "What's the best compliment you've ever received?",
    "What's a childhood memory that still makes you laugh?",
    "What quality do you value most in a partner?",
    "What's a dream you haven't told many people about?",
]
DARES = [
    "Describe me using only three emojis.",
    "Write a two-line poem about us, right now.",
    "Give me the most creative compliment you can think of.",
    "Tell me a joke. If I don't laugh you have to try again!",
    "Describe our perfect first date in exactly one sentence.",
    "Type out your best 'good morning' message for me.",
    "Describe your day using only movie titles.",
    "Invent a cute nickname for me and explain it.",
    "Share the last song you listened to and why you love it.",
    "Tell me three things you're grateful for today.",
    "Make up a two-sentence love story about us.",
    "Send me your most-used emoji and tell me why.",
]

# (question, [4 options]) - the SUBJECT answers about themselves, the partner guesses.
KNOW_ME = [
    ("What's my ideal weekend?", ["Adventure outdoors 🏞️", "Cozy at home 🛋️", "Out with friends 🎉", "Trying something new 🎨"]),
    ("What's my go-to comfort food?", ["Pizza 🍕", "Biryani / rice 🍚", "Sweets & chocolate 🍫", "Ice cream 🍦"]),
    ("How do I recharge after a long day?", ["Music 🎧", "Sleep 😴", "Talking to someone 💬", "Movies / shows 🎬"]),
    ("What's my dream holiday?", ["Beach 🏖️", "Mountains 🏔️", "Big city 🌆", "Countryside 🌾"]),
    ("Am I an early bird or a night owl?", ["Early bird 🌅", "Night owl 🦉", "A bit of both 🌗", "Whatever my schedule says 😅"]),
    ("What's my love language?", ["Words of affirmation 💌", "Quality time ⏳", "Gifts 🎁", "Acts of service 🤝"]),
    ("What would I pick for a movie night?", ["Romance 💕", "Comedy 😂", "Thriller 🕵️", "Sci-fi / fantasy 🚀"]),
    ("What's my biggest pet peeve?", ["Being late ⏰", "Messy spaces 🧹", "Loud chewing 🙉", "Unanswered messages 📱"]),
    ("How do I handle stress?", ["Talk it out 🗣️", "Go quiet & think 🤔", "Stay busy 🏃", "Laugh it off 😄"]),
    ("What's my dream date?", ["Candlelit dinner 🕯️", "Long walk & chai ☕", "Concert or show 🎤", "Road trip 🚗"]),
    ("What do I notice first in a person?", ["Smile 😊", "Sense of humour 😂", "Kindness 💗", "Confidence ✨"]),
    ("Which season is my favourite?", ["Summer ☀️", "Monsoon / rain 🌧️", "Winter ❄️", "Spring 🌸"]),
]


def register(app, g):
    S = lambda: g["supabase"]                       # looked up per call (tests swap the client)
    ok, err, auth, iso, notify = g["ok"], g["err"], g["require_auth"], g["_iso"], g["_notify"]
    valid_id, pair, first_name = g["_valid_id"], g["_pair"], g["_first_name"]

    # ───────────────────────── access ─────────────────────────
    def can_play(a, b):
        return a != b and valid_id(b) and g["_have_conversation"](a, b) and not g["_connection_ended"](a, b)

    def other_of(row, uid):
        return row["user2_id"] if uid == row["user1_id"] else row["user1_id"]

    def get_row(gid, uid):
        """Row if the caller belongs to the room and the connection is still alive, else None."""
        if not valid_id(gid):
            return None
        r = S().table("couple_games").select("*").eq("id", gid).execute().data
        if not r or uid not in (r[0]["user1_id"], r[0]["user2_id"]):
            return None
        return r[0]

    def save(row, status=None, state=None, **extra):
        """Optimistic-lock write. Returns the new row, or None when somebody else wrote first."""
        patch = {"state": state if state is not None else row["state"], "version": row["version"] + 1, "updated_at": iso()}
        if status:
            patch["status"] = status
        patch.update(extra)
        done = S().table("couple_games").update(patch).eq("id", row["id"]).eq("version", row["version"]).execute().data
        return done[0] if done else None

    def age(row):
        try:
            from datetime import datetime
            t = datetime.fromisoformat(str(row.get("updated_at") or row.get("created_at")).replace("Z", "+00:00"))
            return time.time() - t.timestamp()
        except Exception:
            return 0

    def expire(row):
        """Lazy cleanup so a room can never stay open forever."""
        if row["status"] == "invited" and age(row) > INVITE_TTL:
            return save(row, "cancelled") or row
        if row["status"] == "active" and age(row) > IDLE_TTL:
            st = dict(row["state"]); st["over"] = True; st["expired"] = True
            return save(row, "finished", st) or row
        return row

    # ───────────────────────── game engines ─────────────────────────
    def new_series(prev=None):
        s = dict(prev or {})
        s.setdefault("wins", {}); s.setdefault("played", 0); s.setdefault("hearts", 0)
        return s

    def base_state(players, series):
        return {"players": players, "turn": players[0], "over": False, "result": None, "series": new_series(series), "rematch": []}

    def finish(st, winner=None, **result):
        st["over"] = True
        st["result"] = dict(result, winner=winner)
        st["series"]["played"] += 1
        if winner and winner != "draw":
            st["series"]["wins"][winner] = st["series"]["wins"].get(winner, 0) + 1

    def swap(st, uid):
        a, b = st["players"]
        return b if uid == a else a

    def init_game(game, players, series=None):
        st = base_state(players, series)
        a, b = players
        if game == "tictactoe":
            st.update(board=[""] * 9, marks={a: "X", b: "O"})
        elif game == "truthdare":
            st.update(phase="choose", kind=None, prompt=None, answer=None, round=1, used=[], skips={a: 0, b: 0}, hearts={a: 0, b: 0}, done={a: 0, b: 0})
        elif game == "knowme":
            order = random.sample(range(len(KNOW_ME)), KNOW_ME_ROUNDS())
            st.update(order=order, round=0, phase="answer", subject=a, subject_answer=None, guess=None, scores={a: 0, b: 0}, history=[])
        elif game == "memory":
            cards = MEMORY_EMOJI * 2
            random.shuffle(cards)
            st.update(cards=cards, flipped=[], matched={}, scores={a: 0, b: 0}, pending=False, pending_at=0)
        elif game == "puzzle":
            tiles = list(range(1, 9)) + [0]
            prev = -1
            for _ in range(120):                    # random legal moves from the solved board => always solvable
                blank = tiles.index(0)
                opts = [n for n in neighbours(blank) if n != prev]
                tgt = random.choice(opts)
                tiles[blank], tiles[tgt] = tiles[tgt], tiles[blank]
                prev = blank
            if tiles == SOLVED:                     # astronomically unlikely, but never start already solved
                tiles = [1, 2, 3, 4, 5, 6, 0, 7, 8]
            st.update(tiles=tiles, moves=0, started_at=time.time())
        return st

    def KNOW_ME_ROUNDS():
        return min(KNOW_ROUNDS, len(KNOW_ME))

    SOLVED = list(range(1, 9)) + [0]

    def neighbours(i):
        r, c = divmod(i, 3)
        return [rr * 3 + cc for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)) if 0 <= rr < 3 and 0 <= cc < 3]

    class Bad(Exception):
        pass

    def need(cond, msg):
        if not cond:
            raise Bad(msg)

    def as_int(v, lo, hi, what="value"):
        need(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi, f"Invalid {what}")
        return v

    def memory_settle(st, force=False):
        """Turn a mismatched pair back over once it has been visible long enough (idempotent; either partner may trigger it)."""
        if st.get("pending") and (force or time.time() - st.get("pending_at", 0) >= MEMORY_HIDE_AFTER):
            st["flipped"], st["pending"] = [], False
            st["turn"] = swap(st, st["turn"])
            return True
        return False

    def apply_move(game, st, uid, d):
        """Mutates `st` (a deep-ish copy). Raises Bad(msg) for illegal moves."""
        need(not st["over"], "This game is over")
        if game == "memory":
            memory_settle(st)
            if d.get("type") == "resolve":
                return
        if game == "tictactoe":
            need(st["turn"] == uid, "It's your partner's turn")
            i = as_int(d.get("cell"), 0, 8, "cell")
            need(not st["board"][i], "That square is taken")
            st["board"][i] = st["marks"][uid]
            for ln in LINES:
                if st["board"][ln[0]] and all(st["board"][k] == st["board"][ln[0]] for k in ln):
                    return finish(st, uid, line=list(ln))
            if all(st["board"]):
                return finish(st, "draw")
            st["turn"] = swap(st, uid)

        elif game == "truthdare":
            t, other = d.get("type"), swap(st, uid)
            if t == "choose":
                need(st["turn"] == uid and st["phase"] == "choose", "Not your turn to choose")
                kind = d.get("kind")
                need(kind in ("truth", "dare"), "Pick truth or dare")
                bank = TRUTHS if kind == "truth" else DARES
                key = [kind[0] + str(i) for i in range(len(bank))]
                free = [k for k in key if k not in st["used"]] or key
                pick = random.choice(free)
                st["used"].append(pick)
                st.update(kind=kind, prompt=bank[key.index(pick)], phase="answer", answer=None)
            elif t == "answer":
                need(st["turn"] == uid and st["phase"] == "answer", "Not your turn to answer")
                if d.get("skip"):
                    need(st["skips"][uid] < TD_MAX_SKIPS, "No skips left")
                    st["skips"][uid] += 1
                    st.update(answer=None, skipped=True)
                else:
                    text = d.get("text")
                    text = text.strip() if isinstance(text, str) else ""
                    need(len(text) <= 300, "Keep it under 300 characters")
                    need(st["kind"] == "dare" or text, "Write your answer first")
                    st["done"][uid] += 1
                    st.update(answer=text or "Done! ✅", skipped=False)
                st["phase"] = "react"
            elif t == "react":
                need(st["turn"] == other and st["phase"] == "react", "Waiting for your partner")
                if d.get("heart"):
                    st["hearts"][st["turn"]] += 1
                    st["series"]["hearts"] += 1
                if st["round"] >= TD_ROUNDS:
                    return finish(st, None, summary=True)
                st.update(turn=uid, round=st["round"] + 1, phase="choose", kind=None, prompt=None, answer=None, skipped=False)
            else:
                raise Bad("Unknown action")

        elif game == "knowme":
            t, subject = d.get("type"), st["subject"]
            guesser = swap(st, subject)
            q = KNOW_ME[st["order"][st["round"]]]
            if t == "answer":
                need(uid == subject and st["phase"] == "answer", "Waiting for your partner")
                st["subject_answer"] = as_int(d.get("choice"), 0, len(q[1]) - 1, "choice")
                st["phase"] = "guess"
            elif t == "guess":
                need(uid == guesser and st["phase"] == "guess", "Waiting for your partner")
                st["guess"] = as_int(d.get("choice"), 0, len(q[1]) - 1, "choice")
                hit = st["guess"] == st["subject_answer"]
                if hit:
                    st["scores"][guesser] += 1
                st["history"].append({"q": st["order"][st["round"]], "subject": subject, "answer": st["subject_answer"], "guess": st["guess"], "hit": hit})
                st["phase"] = "reveal"
            elif t == "next":
                need(st["phase"] == "reveal", "Not yet")
                if st["round"] + 1 >= len(st["order"]):
                    a, b = st["players"]
                    sa, sb = st["scores"][a], st["scores"][b]
                    return finish(st, "draw" if sa == sb else (a if sa > sb else b))
                st.update(round=st["round"] + 1, phase="answer", subject=guesser, subject_answer=None, guess=None)
                st["turn"] = guesser
            else:
                raise Bad("Unknown action")

        elif game == "memory":
            need(st["turn"] == uid, "It's your partner's turn")
            need(not st["pending"], "Hold on, cards are turning back")
            i = as_int(d.get("idx"), 0, 15, "card")
            need(i not in st["flipped"] and str(i) not in st["matched"], "Card already open")
            st["flipped"].append(i)
            if len(st["flipped"]) == 2:
                a, b = st["flipped"]
                if st["cards"][a] == st["cards"][b]:
                    st["matched"][str(a)] = st["matched"][str(b)] = uid
                    st["scores"][uid] += 1
                    st["flipped"] = []
                    if len(st["matched"]) == 16:
                        x, y = st["players"]
                        sx, sy = st["scores"][x], st["scores"][y]
                        return finish(st, "draw" if sx == sy else (x if sx > sy else y))
                else:
                    st["pending"], st["pending_at"] = True, time.time()

        elif game == "puzzle":
            need(st["turn"] == uid, "It's your partner's turn")
            i = as_int(d.get("tile"), 0, 8, "tile")
            blank = st["tiles"].index(0)
            need(i != blank and i in neighbours(blank), "That tile can't move")
            st["tiles"][blank], st["tiles"][i] = st["tiles"][i], 0
            st["moves"] += 1
            if st["tiles"] == SOLVED:
                secs = int(time.time() - st["started_at"])
                best = st["series"].get("best_moves")
                st["series"]["best_moves"] = st["moves"] if best is None else min(best, st["moves"])
                return finish(st, None, coop=True, moves=st["moves"], seconds=secs)
            st["turn"] = swap(st, uid)

    def view_state(game, st, uid):
        """What this player is allowed to see."""
        v = {k: val for k, val in st.items() if k not in ("cards", "order", "used")}
        if game == "memory":
            shown = set(st["flipped"]) | {int(k) for k in st["matched"]}
            v["cards"] = [st["cards"][i] if i in shown else None for i in range(16)]
        if game == "knowme":
            q = KNOW_ME[st["order"][st["round"]]]
            v["question"], v["options"], v["total"] = q[0], q[1], len(st["order"])
            if st["phase"] == "guess" and uid != st["subject"]:
                v["subject_answer"] = None          # the guesser must not see it
            v["history"] = [{**h, "q": KNOW_ME[h["q"]][0], "options": KNOW_ME[h["q"]][1]} for h in st["history"]]
        if game == "truthdare":
            v["total"] = TD_ROUNDS
            v["max_skips"] = TD_MAX_SKIPS
        return v

    def shape(row, uid):
        row = expire(row)
        st = row["state"] or {}
        if row["game"] == "memory" and row["status"] == "active" and memory_settle(st):
            new = save(row, state=st)
            row = new or row
            st = row["state"]
        other = other_of(row, uid)
        return {
            "id": row["id"], "game": row["game"], "status": row["status"], "version": row["version"],
            "inviter_id": row["inviter_id"], "you": uid, "now": time.time(), "partner_id": other, "partner_name": first_name(other),
            "state": view_state(row["game"], st, uid) if st else {},
        }

    def open_room(a, b):
        u1, u2 = pair(a, b)
        rows = S().table("couple_games").select("*").eq("user1_id", u1).eq("user2_id", u2).in_("status", ["invited", "active"]).execute().data
        for r in rows:
            r2 = expire(r)
            if r2["status"] in ("invited", "active"):
                return r2
        return None

    def guard(uid, row):
        """Room must exist, belong to the caller, and the pair must still be connected."""
        if not row:
            return err("Game not found", 404)
        if not can_play(uid, other_of(row, uid)):
            return err("You can only play with an active match", 403)
        return None

    # ───────────────────────── routes ─────────────────────────
    @app.route("/api/couple-games/with/<other_id>", methods=["GET"])
    @auth
    def cg_current(user_id, other_id):
        if not can_play(user_id, other_id):
            return err("You can only play with an active match", 403)
        r = open_room(user_id, other_id)
        return ok({"game": shape(r, user_id) if r else None})

    @app.route("/api/couple-games/with/<other_id>/invite", methods=["POST"])
    @auth
    def cg_invite(user_id, other_id):
        if not can_play(user_id, other_id):
            return err("You can only play with an active match", 403)
        game = (request.get_json(silent=True) or {}).get("game")
        if game not in GAMES:
            return err("Unknown game")
        if open_room(user_id, other_id):
            return err("A game room is already open with this match. Finish or leave it first", 409)
        u1, u2 = pair(user_id, other_id)
        try:
            row = S().table("couple_games").insert({
                "user1_id": u1, "user2_id": u2, "inviter_id": user_id, "game": game, "status": "invited",
                "state": {}, "version": 1, "created_at": iso(), "updated_at": iso()}).execute().data[0]
        except Exception:           # the one_open_couple_game unique index caught a double-click / race
            return err("A game room is already open with this match", 409)
        label = {"tictactoe": "Tic-Tac-Toe", "truthdare": "Truth or Dare", "knowme": "How Well Do You Know Me?",
                 "memory": "Memory Match", "puzzle": "Couple Puzzle Challenge"}[game]
        notify(other_id, user_id, "game_invite", f"{first_name(user_id)} invited you to play {label} 🎮", "Open the Couple Zone to join")
        return ok(shape(row, user_id), "Invite sent", 201)

    @app.route("/api/couple-games/<gid>", methods=["GET"])
    @auth
    def cg_get(user_id, gid):
        row = get_row(gid, user_id)
        bad = guard(user_id, row)
        if bad:
            return bad
        row = expire(row)
        if row["game"] == "memory" and row["status"] == "active":
            row = shape_row_settle(row)
        have = request.args.get("v", type=int)
        if have is not None and have == row["version"]:
            return ok({"unchanged": True, "version": row["version"]})
        return ok({"game": shape(row, user_id)})

    def shape_row_settle(row):
        st = dict(row["state"])
        if memory_settle(st):
            return save(row, state=st) or row
        return row

    @app.route("/api/couple-games/<gid>/respond", methods=["POST"])
    @auth
    def cg_respond(user_id, gid):
        row = get_row(gid, user_id)
        bad = guard(user_id, row)
        if bad:
            return bad
        row = expire(row)
        action = (request.get_json(silent=True) or {}).get("action")
        if row["status"] != "invited":
            return err("This invite is no longer open", 409)
        if action == "accept":
            if row["inviter_id"] == user_id:
                return err("You can't accept your own invite", 403)
            players = [row["inviter_id"], user_id]
            new = save(row, "active", init_game(row["game"], players))
        elif action == "decline":
            if row["inviter_id"] == user_id:
                return err("Use cancel for your own invite", 403)
            new = save(row, "declined")
            notify(row["inviter_id"], user_id, "game_declined", f"{first_name(user_id)} can't play right now")
        elif action == "cancel":
            if row["inviter_id"] != user_id:
                return err("Only the sender can cancel", 403)
            new = save(row, "cancelled")
        else:
            return err("action must be accept, decline or cancel")
        if not new:
            return err("The room was just updated, try again", 409)
        return ok({"game": shape(new, user_id)})

    @app.route("/api/couple-games/<gid>/move", methods=["POST"])
    @auth
    def cg_move(user_id, gid):
        row = get_row(gid, user_id)
        bad = guard(user_id, row)
        if bad:
            return bad
        row = expire(row)
        if row["status"] != "active":
            return err("This game isn't running", 409)
        d = request.get_json(silent=True) or {}
        import copy
        st = copy.deepcopy(row["state"])
        try:
            apply_move(row["game"], st, user_id, d)
        except Bad as e:
            return err(str(e), 409 if "turn" in str(e).lower() or "wait" in str(e).lower() else 400)
        new = save(row, "finished" if st["over"] else None, st)
        if not new:
            return err("Your partner moved at the same moment, try again", 409)
        return ok({"game": shape(new, user_id)})

    @app.route("/api/couple-games/<gid>/rematch", methods=["POST"])
    @auth
    def cg_rematch(user_id, gid):
        row = get_row(gid, user_id)
        bad = guard(user_id, row)
        if bad:
            return bad
        st = dict(row["state"])
        if row["status"] != "finished" or not st.get("over") or st.get("left_by") or st.get("expired"):
            return err("A rematch isn't available for this game", 409)
        if st.get("next_id"):
            return ok({"game": shape(row, user_id)})
        votes = list(st.get("rematch") or [])
        if user_id not in votes:
            votes.append(user_id)
        st["rematch"] = votes
        if len(votes) < 2:
            new = save(row, state=st)
            if not new:
                return err("The room was just updated, try again", 409)
            return ok({"game": shape(new, user_id)})
        if open_room(*pair(user_id, other_of(row, user_id))):
            return err("A game room is already open with this match", 409)
        a, b = st["players"]
        nxt_players = [b, a]                         # the other partner starts the rematch
        u1, u2 = pair(user_id, other_of(row, user_id))
        try:
            nrow = S().table("couple_games").insert({
                "user1_id": u1, "user2_id": u2, "inviter_id": row["inviter_id"], "game": row["game"], "status": "active",
                "state": init_game(row["game"], nxt_players, st["series"]), "version": 1,
                "created_at": iso(), "updated_at": iso()}).execute().data[0]
        except Exception:
            return err("Could not start the rematch", 409)
        st["next_id"] = nrow["id"]
        save(row, state=st)                          # lets the first voter's poll jump to the new room
        return ok({"game": shape(nrow, user_id)})

    @app.route("/api/couple-games/<gid>/exit", methods=["POST"])
    @auth
    def cg_exit(user_id, gid):
        row = get_row(gid, user_id)
        if not row:
            return err("Game not found", 404)
        if row["status"] in ("declined", "cancelled"):
            return ok({"game": None})
        if row["status"] == "invited":
            new = save(row, "cancelled" if row["inviter_id"] == user_id else "declined")
            return ok({"game": None})
        st = dict(row["state"])
        if not st.get("left_by"):
            st["left_by"], st["over"] = user_id, True
            if row["status"] == "active":
                save(row, "finished", st)
            else:
                save(row, state=st)
        return ok({"game": None})
