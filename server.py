"""
server.py — Flask-SocketIO server wiring Game to WebSocket clients.

One Game instance lives per room.  All game logic stays in game.py —
this file only handles:
  • connection / session bookkeeping
  • deserialising client JSON → Card objects
  • calling Game methods, catching GameError → emit 'error'
  • serialising results → JSON and broadcasting to the right recipients
  • per-turn auto-fold timers

══════════════════════════════════════════════════════
CLIENT → SERVER events
══════════════════════════════════════════════════════
  join              {room, username}
  start_game        {room}
  submit_split      {room, front: [card_str, ...], back: [card_str, ...]}
  submit_decision   {room, decision: 'fold'|'play'}
  next_round        {room}
  request_state     {room}          ← reconnecting client

══════════════════════════════════════════════════════
SERVER → CLIENT events
══════════════════════════════════════════════════════
  joined            {room, your_player_id, players, scores}          → you only
  player_joined     {player_id, players}                             → rest of room
  error             {message}                                        → you only
  game_started      {round, dealer}                                  → room
  deal_cards        {cards: [str, ...]}                              → you only (private!)
  split_received    {player, waiting_for: [...]}                     → room
  all_split         {next_to_decide, queue}                          → room
  fold_decision_prompt  {player, seconds}                            → room
  player_decided    {player, decision}                               → room
  showdown          {round, battles, play_players, fold_players,
                     deltas, scores}                                 → room
  round_started     {round, dealer}                                  → room
  player_left       {player_id}                                      → room
  game_state        {snapshot}                                       → you only

Card string format: "As"=A♠  "Kh"=K♥  "Tc"=10♣  "JK"=Joker
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from engine.card import Card, Rank, Suit, RANK_SYMBOLS, SUIT_SYMBOLS
from engine.game import Game, GameError, BattleResult, RoundResult, Phase


# ──────────────────────────────────────────────
# Card serialisation
# ──────────────────────────────────────────────

_RANK_FROM_SYM: Dict[str, Rank] = {v: k for k, v in RANK_SYMBOLS.items()}
_SUIT_FROM_SYM: Dict[str, Suit] = {v: k for k, v in SUIT_SYMBOLS.items()}


def card_to_str(card: Card) -> str:
    return str(card)          # "As", "Kh", "Tc", "JK"


def str_to_card(s: str) -> Card:
    """Parse a card string back into a Card object. Raises ValueError on bad input."""
    if s == "JK":
        return Card(Rank.TWO, Suit.CLUBS, is_joker=True)
    if len(s) < 2:
        raise ValueError(f"Cannot parse card '{s}'")
    rank_sym, suit_sym = s[:-1], s[-1]
    if rank_sym not in _RANK_FROM_SYM or suit_sym not in _SUIT_FROM_SYM:
        raise ValueError(f"Unknown card '{s}'")
    return Card(_RANK_FROM_SYM[rank_sym], _SUIT_FROM_SYM[suit_sym])


def cards_to_strs(cards: List[Card]) -> List[str]:
    return [card_to_str(c) for c in cards]


def strs_to_cards(strs: List[str]) -> List[Card]:
    return [str_to_card(s) for s in strs]


# ──────────────────────────────────────────────
# Room state
# ──────────────────────────────────────────────

TURN_SECONDS = 60    # seconds per fold/play decision


@dataclass
class RoomState:
    game:            Game
    sid_to_player:   Dict[str, str]           = field(default_factory=dict)
    player_to_sid:   Dict[str, str]           = field(default_factory=dict)
    lock:            threading.Lock           = field(default_factory=threading.Lock)
    timer:           Optional[threading.Timer] = None
    game_started:    bool                     = False
    waiting_players: List[str]                = field(default_factory=list)
    left_players:    set                      = field(default_factory=set)


# Keyed by room_id string
rooms: Dict[str, RoomState] = {}


# ──────────────────────────────────────────────
# Flask / SocketIO setup
# ──────────────────────────────────────────────

app = Flask(__name__, static_folder="frontend", static_url_path="")
app.config["SECRET_KEY"] = "change-me-in-production"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


# ──────────────────────────────────────────────
# Serialisation helpers
# ──────────────────────────────────────────────

def _battle_dict(b: BattleResult, rs) -> dict:
    """Serialise a BattleResult, including actual card strings for the frontend."""
    def hand_strs(pid, which):
        try:
            p = rs.game._get_player(pid)
            return cards_to_strs(p.front if which == "front" else p.back)
        except Exception:
            return []
    return {
        "p1": b.p1_id,           "p2": b.p2_id,
        "winner": b.winner_id,   "net_points": b.net_points,
        "swept": b.swept,
        "front_winner": b.front_winner_id,
        "back_winner":  b.back_winner_id,
        "front_mult":   b.front_mult,
        "back_mult":    b.back_mult,
        "p1_front_label":  b.p1_front_label,
        "p1_back_label":   b.p1_back_label,
        "p2_front_label":  b.p2_front_label,
        "p2_back_label":   b.p2_back_label,
        "p1_front_cards":  hand_strs(b.p1_id, "front"),
        "p1_back_cards":   hand_strs(b.p1_id, "back"),
        "p2_front_cards":  hand_strs(b.p2_id, "front"),
        "p2_back_cards":   hand_strs(b.p2_id, "back"),
    }


def _round_dict(rr: RoundResult, rs) -> dict:
    return {
        "round":        rr.round_number,
        "battles":      [_battle_dict(b, rs) for b in rr.battles],
        "play_players": rr.play_player_ids,
        "fold_players": rr.fold_player_ids,
        "deltas":       rr.score_deltas,
        "scores":       rr.scores,
    }


# ──────────────────────────────────────────────
# Timer helpers
# ──────────────────────────────────────────────

def _cancel_timer(rs: RoomState) -> None:
    if rs.timer:
        rs.timer.cancel()
        rs.timer = None


def _start_timer(room_id: str, player_id: str) -> None:
    """
    Start the per-turn countdown.  If it fires before the player decides,
    auto-fold them and broadcast the result.
    """
    rs = rooms[room_id]
    _cancel_timer(rs)

    def _on_timeout():
        # Runs in a background thread — use socketio.emit, not emit
        with rs.lock:
            if rs.game.current_decision_player() != player_id:
                return     # player already acted; stale timer
            try:
                result = rs.game.auto_fold(player_id)
            except GameError:
                return

        socketio.emit("player_decided",
                      {"player": player_id, "decision": "fold"},
                      to=room_id)
        if result.round_result:
            socketio.emit("showdown", _round_dict(result.round_result, rs), to=room_id)
        else:
            _prompt_next(room_id)

    rs.timer = threading.Timer(TURN_SECONDS, _on_timeout)
    rs.timer.daemon = True
    rs.timer.start()


def _prompt_next(room_id: str) -> None:
    """Tell the room who decides next and start their timer."""
    rs = rooms[room_id]
    player_id = rs.game.current_decision_player()
    if player_id is None:
        return
    socketio.emit("fold_decision_prompt",
                  {"player": player_id, "seconds": TURN_SECONDS},
                  to=room_id)
    _start_timer(room_id, player_id)


# ──────────────────────────────────────────────
# Convenience — resolve room + player from event
# ──────────────────────────────────────────────

def _resolve(data: dict):
    """
    Return (RoomState, player_id) from an incoming event dict,
    or (None, None) and emit an error if anything is missing.
    """
    room_id = data.get("room")
    if not room_id or room_id not in rooms:
        emit("error", {"message": "Room not found."})
        return None, None
    rs = rooms[room_id]
    player_id = rs.sid_to_player.get(request.sid)
    if not player_id:
        emit("error", {"message": "You are not in this room."})
        return None, None
    return rs, player_id


# ──────────────────────────────────────────────
# SocketIO event handlers
# ──────────────────────────────────────────────

@socketio.on("join")
def handle_join(data):
    room_id   = data.get("room", "").strip()
    player_id = data.get("username", "").strip()

    if not room_id or not player_id:
        return emit("error", {"message": "room and username are required."})

    if room_id not in rooms:
        rooms[room_id] = RoomState(game=Game(room_id))

    rs = rooms[room_id]

    with rs.lock:
        already_active  = any(p.player_id == player_id for p in rs.game.players)
        already_waiting = player_id in rs.waiting_players

        # ── Reconnection: player is already in the game ────────────────
        if already_active:
            old_sid = rs.player_to_sid.get(player_id)
            if old_sid:
                rs.sid_to_player.pop(old_sid, None)
            rs.sid_to_player[request.sid] = player_id
            rs.player_to_sid[player_id]   = request.sid
            join_room(room_id)
            emit("joined", {
                "room":            room_id,
                "your_player_id":  player_id,
                "players":         [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players],
                "scores":          rs.game.get_scores(),
                "reconnected":     True,
            })
            emit("game_state", rs.game.get_state_snapshot())
            try:
                player = rs.game._get_player(player_id)
                if player.hand:
                    emit("deal_cards", {"cards": cards_to_strs(player.hand)})
            except GameError:
                pass
            return

        # ── Mid-game join: put player in the waiting room ──────────────
        if rs.game_started and not already_waiting:
            rs.waiting_players.append(player_id)
            rs.sid_to_player[request.sid] = player_id
            rs.player_to_sid[player_id]   = request.sid
            join_room(room_id)
            active_players = [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players]
            emit("joined", {
                "room":            room_id,
                "your_player_id":  player_id,
                "players":         active_players,
                "scores":          rs.game.get_scores(),
            })
            emit("waiting_room", {
                "players": active_players,
                "scores":  rs.game.get_scores(),
                "waiting": rs.waiting_players,
            })
            socketio.emit("player_waiting", {
                "player_id": player_id,
                "waiting":   rs.waiting_players,
            }, to=room_id, include_self=False)
            return

        if rs.game_started and already_waiting:
            # Reconnect of a waiting player — just refresh their sid
            old_sid = rs.player_to_sid.get(player_id)
            if old_sid:
                rs.sid_to_player.pop(old_sid, None)
            rs.sid_to_player[request.sid] = player_id
            rs.player_to_sid[player_id]   = request.sid
            join_room(room_id)
            emit("joined", {
                "room":            room_id,
                "your_player_id":  player_id,
                "players":         [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players],
                "scores":          rs.game.get_scores(),
            })
            emit("waiting_room", {
                "players": [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players],
                "scores":  rs.game.get_scores(),
                "waiting": rs.waiting_players,
            })
            return

        # ── Normal pre-game join ───────────────────────────────────────
        try:
            rs.game.add_player(player_id)
        except GameError as e:
            return emit("error", {"message": str(e)})

        rs.sid_to_player[request.sid] = player_id
        rs.player_to_sid[player_id]   = request.sid
        join_room(room_id)

    players = [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players]
    emit("joined", {
        "room":           room_id,
        "your_player_id": player_id,
        "players":        players,
        "scores":         rs.game.get_scores(),
    })
    emit("player_joined", {"player_id": player_id, "players": players},
         to=room_id, include_self=False)


@socketio.on("start_game")
def handle_start_game(data):
    room_id = data.get("room", "")
    if room_id not in rooms:
        return emit("error", {"message": "Room not found."})

    rs = rooms[room_id]

    with rs.lock:
        if rs.game_started:
            return emit("error", {"message": "Game already started."})
        try:
            deal = rs.game.start_round()
        except GameError as e:
            return emit("error", {"message": str(e)})
        rs.game_started = True

    socketio.emit("game_started", {
        "round":  deal.round_number,
        "dealer": deal.dealer_id,
    }, to=room_id)

    # Send each player their PRIVATE hand — never broadcast all hands together
    for pid, hand in deal.dealt_hands.items():
        if pid in rs.left_players:
            continue   # don't deal to players who intentionally left
        sid = rs.player_to_sid.get(pid)
        if sid:
            socketio.emit("deal_cards", {"cards": cards_to_strs(hand)}, to=sid)


@socketio.on("submit_split")
def handle_submit_split(data):
    rs, player_id = _resolve(data)
    if rs is None:
        return

    try:
        front = strs_to_cards(data.get("front", []))
        back  = strs_to_cards(data.get("back",  []))
    except ValueError as e:
        return emit("error", {"message": f"Invalid card: {e}"})

    with rs.lock:
        try:
            result = rs.game.submit_split(player_id, front, back)
        except GameError as e:
            return emit("error", {"message": str(e)})

    room_id = data["room"]

    # Let everyone see who has split and who hasn't
    socketio.emit("split_received", {
        "player":      player_id,
        "waiting_for": result.still_waiting,
    }, to=room_id)

    # Once everyone is in, open the fold/play phase
    if result.all_split:
        socketio.emit("all_split", {
            "next_to_decide": result.next_to_decide,
            "queue":          rs.game._decision_queue,
        }, to=room_id)
        _prompt_next(room_id)


@socketio.on("submit_decision")
def handle_submit_decision(data):
    rs, player_id = _resolve(data)
    if rs is None:
        return

    decision = data.get("decision", "")
    room_id  = data["room"]

    with rs.lock:
        try:
            result = rs.game.submit_decision(player_id, decision)
        except GameError as e:
            return emit("error", {"message": str(e)})
        _cancel_timer(rs)

    socketio.emit("player_decided", {
        "player":   player_id,
        "decision": decision,
    }, to=room_id)

    if result.round_result:
        socketio.emit("showdown", _round_dict(result.round_result, rs), to=room_id)
    else:
        _prompt_next(room_id)


@socketio.on("next_round")
def handle_next_round(data):
    rs, player_id = _resolve(data)
    if rs is None:
        return

    room_id = data["room"]

    with rs.lock:
        # Admit waiting players into the game before dealing
        newly_joined = []
        for pid in list(rs.waiting_players):
            try:
                rs.game.add_player(pid)
                rs.waiting_players.remove(pid)
                newly_joined.append(pid)
            except GameError:
                pass

        try:
            deal = rs.game.next_round()
        except GameError as e:
            return emit("error", {"message": str(e)})

    all_players = [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players]

    # Notify newly added players they're in — they need to transition to game screen
    for pid in newly_joined:
        sid = rs.player_to_sid.get(pid)
        if sid:
            socketio.emit("joined_game", {
                "players": all_players,
            }, to=sid)

    socketio.emit("round_started", {
        "round":       deal.round_number,
        "dealer":      deal.dealer_id,
        "players":     all_players,
        "left_players": list(rs.left_players),
    }, to=room_id)

    for pid, hand in deal.dealt_hands.items():
        if pid in rs.left_players:
            continue   # don't deal to players who intentionally left
        sid = rs.player_to_sid.get(pid)
        if sid:
            socketio.emit("deal_cards", {"cards": cards_to_strs(hand)}, to=sid)


@socketio.on("request_state")
def handle_request_state(data):
    """
    A reconnecting client requests a full snapshot so it can rebuild its UI.
    The server re-sends their private hand separately.
    """
    rs, player_id = _resolve(data)
    if rs is None:
        return

    emit("game_state", rs.game.get_state_snapshot())

    # Re-send their private hand if the round is still live
    try:
        player = rs.game._get_player(player_id)
        if player.hand:
            emit("deal_cards", {"cards": cards_to_strs(player.hand)})
    except GameError:
        pass


@socketio.on("leave_game")
def handle_leave_game(data):
    """
    Player intentionally leaves. Mark them as left so they are skipped in
    future rounds and their current-round action is resolved immediately.
    """
    rs, player_id = _resolve(data)
    if rs is None:
        return

    room_id = data.get("room", "")
    rs.left_players.add(player_id)

    # Remove from waiting list if they were there
    if player_id in rs.waiting_players:
        rs.waiting_players.remove(player_id)

    with rs.lock:
        # SPLITTING: auto-submit so the round isn't blocked
        if rs.game.phase == Phase.SPLITTING:
            try:
                player_obj = rs.game._get_player(player_id)
                if not player_obj.has_split and player_obj.hand:
                    split_result = rs.game.submit_split(
                        player_id,
                        player_obj.hand[:2],
                        player_obj.hand[2:],
                    )
                    socketio.emit("split_received", {
                        "player":      player_id,
                        "waiting_for": split_result.still_waiting,
                    }, to=room_id)
                    if split_result.all_split:
                        socketio.emit("all_split", {
                            "next_to_decide": split_result.next_to_decide,
                            "queue":          rs.game._decision_queue,
                        }, to=room_id)
                        _prompt_next(room_id)
            except GameError:
                pass

        # FOLD_DECISION: auto-fold if it's their turn
        elif rs.game.current_decision_player() == player_id:
            try:
                result = rs.game.auto_fold(player_id)
                _cancel_timer(rs)
                socketio.emit("player_decided",
                              {"player": player_id, "decision": "fold"},
                              to=room_id)
                if result.round_result:
                    socketio.emit("showdown", _round_dict(result.round_result, rs), to=room_id)
                else:
                    _prompt_next(room_id)
            except GameError:
                pass

    # Broadcast left status — clients show red dot
    socketio.emit("player_left", {
        "player_id":   player_id,
        "intentional": True,
    }, to=room_id)


@socketio.on("disconnect")
def handle_disconnect():
    for room_id, rs in rooms.items():
        if request.sid not in rs.sid_to_player:
            continue

        player_id = rs.sid_to_player.pop(request.sid, None)
        if player_id:
            rs.player_to_sid.pop(player_id, None)

        with rs.lock:
            # SPLITTING: auto-submit split so game isn't blocked
            if rs.game.phase == Phase.SPLITTING:
                try:
                    player_obj = rs.game._get_player(player_id)
                    if not player_obj.has_split and player_obj.hand:
                        split_result = rs.game.submit_split(
                            player_id,
                            player_obj.hand[:2],
                            player_obj.hand[2:],
                        )
                        socketio.emit("split_received", {
                            "player":      player_id,
                            "waiting_for": split_result.still_waiting,
                        }, to=room_id)
                        if split_result.all_split:
                            socketio.emit("all_split", {
                                "next_to_decide": split_result.next_to_decide,
                                "queue":          rs.game._decision_queue,
                            }, to=room_id)
                            _prompt_next(room_id)
                except GameError:
                    pass

            # FOLD_DECISION: auto-fold if it's their turn
            elif rs.game.current_decision_player() == player_id:
                try:
                    result = rs.game.auto_fold(player_id)
                    _cancel_timer(rs)
                except GameError:
                    result = None

                if result is not None:
                    socketio.emit("player_decided",
                                  {"player": player_id, "decision": "fold"},
                                  to=room_id)
                    if result.round_result:
                        socketio.emit("showdown",
                                      _round_dict(result.round_result, rs),
                                      to=room_id)
                    else:
                        _prompt_next(room_id)

        socketio.emit("player_left", {
            "player_id":   player_id,
            "intentional": False,
        }, to=room_id)
        break


# ──────────────────────────────────────────────
# HTTP routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/health")
def health():
    return {"status": "ok", "rooms": len(rooms)}


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, debug=True, host="0.0.0.0", port=port)