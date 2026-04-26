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
  submit_decision   {room, decision: 'play'|'fold'|'fold_reveal'}
  send_emote        {room, emoji}
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
  room_emote        {player, emoji}                                  → room
  showdown          {round, battles, play_players, fold_players,
                     deltas, scores, player_summaries}               → room
  round_started     {round, dealer}                                  → room
  player_left       {player_id}                                      → room
  game_state        {snapshot}                                       → you only

Card string format: "As"=A♠  "Kh"=K♥  "Tc"=10♣  "JK"=Joker
"""

import atexit
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from engine.card import Card, Rank, Suit, RANK_SYMBOLS, SUIT_SYMBOLS
from engine.game import Game, GameError, BattleResult, RoundResult, Phase
from engine.evaluator import hand_label_brow, hand_label_trow, hand_strength_brow
from db import store


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

SPLIT_SECONDS = 60
DECISION_SECONDS = 15
ALLOWED_EMOJIS = {"🔥", "😂", "🤑", "🤫", "💸", "😈", "😭", "😵", "👀", "🍀"}


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
    archived_scores: Dict[str, int]           = field(default_factory=dict)


STATE_PATH = Path(os.environ.get("ROOM_STATE_PATH", "room_state.json"))
_persist_lock = threading.Lock()


# ──────────────────────────────────────────────
# Flask / SocketIO setup
# ──────────────────────────────────────────────

app = Flask(__name__, static_folder="frontend", static_url_path="")
app.config["SECRET_KEY"] = "change-me-in-production"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


def _serialise_card_lists(cards: List[Card]) -> List[str]:
    return [card_to_str(card) for card in cards]


def _deserialise_card_lists(cards: List[str]) -> List[Card]:
    return [str_to_card(card) for card in cards]


def _game_to_dict(game: Game) -> dict:
    return {
        "room_id": game.room_id,
        "phase": game.phase.name,
        "round_number": game.round_number,
        "dealer_seat": game.dealer_seat,
        "decision_queue": list(game._decision_queue),
        "decision_pos": game._decision_pos,
        "fold_sequence": list(game._fold_sequence),
        "players": [
            {
                "player_id": p.player_id,
                "seat": p.seat,
                "hand": _serialise_card_lists(p.hand),
                "front": _serialise_card_lists(p.front),
                "back": _serialise_card_lists(p.back),
                "has_split": p.has_split,
                "decision": p.decision,
                "fold_order": p.fold_order,
                "reveal_on_fold": p.reveal_on_fold,
                "score_total": p.score_total,
                "score_round": p.score_round,
            }
            for p in game.players
        ],
    }


def _game_from_dict(data: dict) -> Game:
    game = Game(data["room_id"])
    saved_phase = Phase[data["phase"]]
    saved_round_number = data["round_number"]
    saved_dealer_seat = data["dealer_seat"]
    saved_decision_queue = list(data.get("decision_queue", []))
    saved_decision_pos = data.get("decision_pos", 0)
    saved_fold_sequence = list(data.get("fold_sequence", []))

    restored_players = []
    for player_data in data.get("players", []):
        player = game.add_player(player_data["player_id"])
        player.seat = player_data["seat"]
        player.hand = _deserialise_card_lists(player_data.get("hand", []))
        player.front = _deserialise_card_lists(player_data.get("front", []))
        player.back = _deserialise_card_lists(player_data.get("back", []))
        player.has_split = player_data.get("has_split", False)
        player.decision = player_data.get("decision")
        player.fold_order = player_data.get("fold_order")
        player.reveal_on_fold = player_data.get("reveal_on_fold", False)
        player.score_total = player_data.get("score_total", 0)
        player.score_round = player_data.get("score_round", 0)
        restored_players.append(player)

    game._players = sorted(restored_players, key=lambda p: p.seat)
    for seat, player in enumerate(game._players):
        player.seat = seat

    game.phase = saved_phase
    game.round_number = saved_round_number
    game._decision_queue = saved_decision_queue
    game._decision_pos = saved_decision_pos
    game._fold_sequence = saved_fold_sequence

    if game._players:
        game.dealer_seat = saved_dealer_seat % len(game._players)
    else:
        game.dealer_seat = 0

    return game


def _room_to_dict(rs: RoomState) -> dict:
    return {
        "game": _game_to_dict(rs.game),
        "game_started": rs.game_started,
        "waiting_players": list(rs.waiting_players),
        "left_players": list(rs.left_players),
        "archived_scores": dict(rs.archived_scores),
    }


def _room_from_dict(data: dict) -> RoomState:
    return RoomState(
        game=_game_from_dict(data["game"]),
        game_started=data.get("game_started", False),
        waiting_players=list(data.get("waiting_players", [])),
        left_players=set(data.get("left_players", [])),
        archived_scores=dict(data.get("archived_scores", {})),
    )


def _load_rooms() -> Dict[str, RoomState]:
    if store.database_enabled():
        return {
            room_id: _room_from_dict(room_data)
            for room_id, room_data in store.load_room_payloads().items()
        }
    if not STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        room_id: _room_from_dict(room_data)
        for room_id, room_data in payload.get("rooms", {}).items()
    }


def _save_rooms() -> None:
    if store.database_enabled():
        store.save_room_payloads({
            room_id: _room_to_dict(rs)
            for room_id, rs in rooms.items()
        })
        return

    payload = {
        "rooms": {
            room_id: _room_to_dict(rs)
            for room_id, rs in rooms.items()
        }
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(f"{STATE_PATH.suffix}.tmp")
    with _persist_lock:
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(STATE_PATH)


# Keyed by room_id string
rooms: Dict[str, RoomState] = _load_rooms()
atexit.register(_save_rooms)


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
        "darby": b.darby,
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
    player_summaries = []
    for p in rs.game._players:
        reveal_cards = p.decision == "play" or p.reveal_on_fold
        back_strength_bucket, back_strength_name = hand_strength_brow(p.back) if (p.back and reveal_cards) else (5, "")
        back_effect_tier = back_strength_bucket
        player_summaries.append({
            "player_id": p.player_id,
            "decision": p.decision,
            "reveal_cards": reveal_cards,
            "front_cards": cards_to_strs(p.front) if reveal_cards else [],
            "back_cards": cards_to_strs(p.back) if reveal_cards else [],
            "front_label": hand_label_trow(p.front) if (p.front and reveal_cards) else "",
            "back_label": hand_label_brow(p.back) if (p.back and reveal_cards) else "",
            "back_strength_bucket": back_strength_bucket,
            "back_strength_name": back_strength_name,
            "back_effect_tier": back_effect_tier,
        })
    return {
        "round":        rr.round_number,
        "battles":      [_battle_dict(b, rs) for b in rr.battles],
        "play_players": rr.play_player_ids,
        "fold_players": rr.fold_player_ids,
        "darby_winner": rr.darby_winner_id,
        "deltas":       rr.score_deltas,
        "scores":       rr.scores,
        "player_summaries": player_summaries,
    }


def _room_scores(rs: RoomState) -> Dict[str, int]:
    scores = dict(rs.archived_scores)
    scores.update(rs.game.get_scores())
    return scores


def _remove_left_players_for_next_round(rs: RoomState) -> None:
    for player_id in list(rs.left_players):
        try:
            player = rs.game._get_player(player_id)
        except GameError:
            continue

        rs.archived_scores[player_id] = player.score_total
        sid = rs.player_to_sid.pop(player_id, None)
        if sid:
            rs.sid_to_player.pop(sid, None)
        try:
            rs.game.remove_player(player_id)
        except GameError:
            continue


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
    expected_round = rs.game.round_number

    def _on_timeout():
        # Runs in a background thread — use socketio.emit, not emit
        with rs.lock:
            if rs.game.phase != Phase.FOLD_DECISION:
                return
            if rs.game.round_number != expected_round:
                return
            if rs.game.current_decision_player() != player_id:
                return     # player already acted; stale timer
            try:
                result = rs.game.auto_fold(player_id)
            except GameError:
                return
            _save_rooms()

        socketio.emit("player_decided",
                      {"player": player_id, "decision": "fold"},
                      to=room_id)
        if result.round_result:
            socketio.emit("showdown", _round_dict(result.round_result, rs), to=room_id)
        else:
            _prompt_next(room_id)

    rs.timer = threading.Timer(DECISION_SECONDS, _on_timeout)
    rs.timer.daemon = True
    rs.timer.start()


def _start_split_timer(room_id: str) -> None:
    """
    Start the simultaneous split countdown. When it expires, any player who
    has not submitted is auto-split using the dealt order: first 2 cards front,
    last 3 cards back.
    """
    rs = rooms[room_id]
    _cancel_timer(rs)
    expected_round = rs.game.round_number

    def _on_timeout():
        with rs.lock:
            if rs.game.phase != Phase.SPLITTING:
                return
            if rs.game.round_number != expected_round:
                return

            results = []
            for player in rs.game.players:
                if player.player_id in rs.left_players:
                    continue
                if player.has_split or not player.hand:
                    continue
                try:
                    result = rs.game.submit_split(
                        player.player_id,
                        player.hand[:2],
                        player.hand[2:],
                    )
                except GameError:
                    continue
                results.append((player.player_id, result))

            _save_rooms()

        for pid, result in results:
            player = rs.game._get_player(pid)
            socketio.emit("split_received", {
                "player": pid,
                "waiting_for": result.still_waiting,
                "front": cards_to_strs(player.front),
                "back": cards_to_strs(player.back),
            }, to=room_id)

        if results and results[-1][1].all_split:
            socketio.emit("all_split", {
                "next_to_decide": results[-1][1].next_to_decide,
                "queue": rs.game._decision_queue,
            }, to=room_id)
            _prompt_next(room_id)

    rs.timer = threading.Timer(SPLIT_SECONDS, _on_timeout)
    rs.timer.daemon = True
    rs.timer.start()


def _prompt_next(room_id: str) -> None:
    """Tell the room who decides next and start their timer."""
    rs = rooms[room_id]
    with rs.lock:
        if rs.game.phase != Phase.FOLD_DECISION:
            return
        player_id = rs.game.current_decision_player()
        round_number = rs.game.round_number
        if player_id is None:
            return
    socketio.emit("fold_decision_prompt",
                  {"player": player_id, "seconds": DECISION_SECONDS, "round": round_number},
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
        _save_rooms()

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
                "scores":          _room_scores(rs),
                "left_players":    list(rs.left_players),
                "reconnected":     True,
            })
            emit("game_state", rs.game.get_state_snapshot())
            try:
                player = rs.game._get_player(player_id)
                if player.hand and rs.game.phase in {Phase.SPLITTING, Phase.FOLD_DECISION}:
                    emit("deal_cards", {"cards": cards_to_strs(player.hand)})
            except GameError:
                pass
            return

        # ── Mid-game join: put player in the waiting room ──────────────
        if rs.game_started and not already_waiting:
            rs.waiting_players.append(player_id)
            rs.sid_to_player[request.sid] = player_id
            rs.player_to_sid[player_id]   = request.sid
            rs.left_players.discard(player_id)
            join_room(room_id)
            _save_rooms()
            active_players = [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players]
            emit("joined", {
                "room":            room_id,
                "your_player_id":  player_id,
                "players":         active_players,
                "scores":          _room_scores(rs),
                "left_players":    list(rs.left_players),
            })
            emit("waiting_room", {
                "players": active_players,
                "scores":  _room_scores(rs),
                "waiting": rs.waiting_players,
                "left_players": list(rs.left_players),
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
            rs.left_players.discard(player_id)
            emit("joined", {
                "room":            room_id,
                "your_player_id":  player_id,
                "players":         [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players],
                "scores":          _room_scores(rs),
                "left_players":    list(rs.left_players),
            })
            emit("waiting_room", {
                "players": [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players],
                "scores":  _room_scores(rs),
                "waiting": rs.waiting_players,
                "left_players": list(rs.left_players),
            })
            return

        # ── Normal pre-game join ───────────────────────────────────────
        try:
            player = rs.game.add_player(player_id)
            if player_id in rs.archived_scores:
                player.score_total = rs.archived_scores.pop(player_id)
            rs.left_players.discard(player_id)
        except GameError as e:
            return emit("error", {"message": str(e)})

        rs.sid_to_player[request.sid] = player_id
        rs.player_to_sid[player_id]   = request.sid
        join_room(room_id)
        _save_rooms()

    players = [{"player_id": p.player_id, "seat": p.seat} for p in rs.game.players]
    emit("joined", {
        "room":           room_id,
        "your_player_id": player_id,
        "players":        players,
        "scores":         _room_scores(rs),
        "left_players":   list(rs.left_players),
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
        _save_rooms()

    socketio.emit("game_started", {
        "round":  deal.round_number,
        "dealer": deal.dealer_id,
        "split_seconds": SPLIT_SECONDS,
    }, to=room_id)

    # Send each player their PRIVATE hand — never broadcast all hands together
    for pid, hand in deal.dealt_hands.items():
        if pid in rs.left_players:
            continue   # don't deal to players who intentionally left
        sid = rs.player_to_sid.get(pid)
        if sid:
            socketio.emit("deal_cards", {"cards": cards_to_strs(hand)}, to=sid)

    _start_split_timer(room_id)


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
        _save_rooms()

    room_id = data["room"]

    # Let everyone see who has split and who hasn't
    socketio.emit("split_received", {
        "player":      player_id,
        "waiting_for": result.still_waiting,
        "front":       data.get("front", []),
        "back":        data.get("back", []),
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
        _save_rooms()

    socketio.emit("player_decided", {
        "player":   player_id,
        "decision": decision,
    }, to=room_id)

    if result.round_result:
        socketio.emit("showdown", _round_dict(result.round_result, rs), to=room_id)
    else:
        _prompt_next(room_id)


@socketio.on("send_emote")
def handle_send_emote(data):
    rs, player_id = _resolve(data)
    if rs is None:
        return

    room_id = data["room"]
    emoji = str(data.get("emoji", "")).strip()
    if emoji not in ALLOWED_EMOJIS:
        return emit("error", {"message": "Invalid emote."})

    with rs.lock:
        if rs.game.phase not in {Phase.FOLD_DECISION, Phase.ROUND_END}:
            return emit("error", {"message": "Emotes are only available during decisions and showdown."})
        if player_id in rs.left_players:
            return emit("error", {"message": "Left players cannot send emotes."})
        if not any(player.player_id == player_id for player in rs.game.players):
            return emit("error", {"message": "Only active players can send emotes."})

    socketio.emit("room_emote", {
        "player": player_id,
        "emoji": emoji,
        "phase": rs.game.phase.name,
    }, to=room_id)


@socketio.on("next_round")
def handle_next_round(data):
    rs, player_id = _resolve(data)
    if rs is None:
        return

    room_id = data["room"]

    with rs.lock:
        _remove_left_players_for_next_round(rs)

        # Admit waiting players into the game before dealing
        newly_joined = []
        for pid in list(rs.waiting_players):
            try:
                player = rs.game.add_player(pid)
                if pid in rs.archived_scores:
                    player.score_total = rs.archived_scores.pop(pid)
                rs.waiting_players.remove(pid)
                newly_joined.append(pid)
            except GameError:
                pass

        try:
            deal = rs.game.next_round()
        except GameError as e:
            return emit("error", {"message": str(e)})
        _save_rooms()

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
        "split_seconds": SPLIT_SECONDS,
    }, to=room_id)

    for pid, hand in deal.dealt_hands.items():
        sid = rs.player_to_sid.get(pid)
        if sid:
            socketio.emit("deal_cards", {"cards": cards_to_strs(hand)}, to=sid)

    _start_split_timer(room_id)


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
        if player.hand and rs.game.phase in {Phase.SPLITTING, Phase.FOLD_DECISION}:
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
                        "front":       cards_to_strs(player_obj.front),
                        "back":        cards_to_strs(player_obj.back),
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
        _save_rooms()

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
                            "front":       cards_to_strs(player_obj.front),
                            "back":        cards_to_strs(player_obj.back),
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
        _save_rooms()

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
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    socketio.run(
        app,
        debug=debug,
        use_reloader=debug,
        host="0.0.0.0",
        port=port,
    )
