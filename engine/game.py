"""
game.py — pure game state machine for the split-hand Pok Deng variant.

No I/O, no networking, no threading.  The server layer drives this by
calling methods and broadcasting whatever the method returns.

Round lifecycle
───────────────
  WAITING       Players join or leave freely.
  SPLITTING     Every player privately picks their 2-card front and 3-card back.
                Submissions are simultaneous — anyone can submit in any order.
                Phase ends automatically once every player has submitted.
  FOLD_DECISION One player at a time decides: fold or play.
                Order: left of dealer → clockwise → dealer is last.
                The server is responsible for the per-player timer; if the
                timer fires it calls game.auto_fold(player_id).
  ROUND_END     Showdown has run, scores updated.  Call next_round() to loop.

Scoring
───────
  Fold penalty (computed at end of round, once all decisions are in):
    Each folder    loses  3 × (number of players who chose "play")
    Each folder    gains  3 × (number of players who fold AFTER them)
    Each player    gains  3 × (total number of folders this round)

  Battle (round-robin among players who chose "play"):
    compare_trow + compare_brow → battle() → net point exchange per pair.

  score_round  accumulates all fold + battle deltas for the current round.
  score_total  is the running lifetime total; updated at ROUND_END.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

from .card import Card, Deck
from .evaluator import (
    battle,
    compare_brow,
    compare_trow,
    hand_label_brow,
    hand_label_trow,
)


# ──────────────────────────────────────────────
# Phase enum
# ──────────────────────────────────────────────

class Phase(Enum):
    WAITING       = auto()
    SPLITTING     = auto()
    FOLD_DECISION = auto()
    ROUND_END     = auto()


# ──────────────────────────────────────────────
# Data classes — player and result objects
# ──────────────────────────────────────────────

@dataclass
class Player:
    """Mutable per-player state.  Seat order is fixed for the lifetime of a game."""
    player_id:  str
    seat:       int               # 0-indexed position around the table

    # Per-round state (reset by _deal_round)
    hand:       List[Card] = field(default_factory=list)
    front:      List[Card] = field(default_factory=list)   # 2-card split
    back:       List[Card] = field(default_factory=list)   # 3-card split
    has_split:  bool = False
    decision:   Optional[str] = None    # None | "fold" | "play"
    fold_order: Optional[int] = None   # 0-indexed position among folders

    # Running score
    score_total: int = 0
    score_round: int = 0               # delta for the current round only

    def __repr__(self) -> str:
        return f"Player({self.player_id!r}, seat={self.seat}, total={self.score_total})"


@dataclass
class BattleResult:
    """Result of one head-to-head battle between two players."""
    p1_id:          str
    p2_id:          str
    winner_id:      Optional[str]   # None = tie
    net_points:     int
    swept:          bool            # True if winner won BOTH hands

    front_winner_id: Optional[str]
    back_winner_id:  Optional[str]
    front_mult:      int
    back_mult:       int

    # Human-readable labels for the UI
    p1_front_label: str = ""
    p1_back_label:  str = ""
    p2_front_label: str = ""
    p2_back_label:  str = ""


@dataclass
class RoundResult:
    """Everything the server needs to broadcast at the end of a round."""
    round_number:    int
    battles:         List[BattleResult]
    play_player_ids: List[str]
    fold_player_ids: List[str]         # in fold order (earliest folder first)
    score_deltas:    Dict[str, int]    # player_id → points earned this round
    scores:          Dict[str, int]    # player_id → lifetime total after this round


@dataclass
class DealResult:
    """Returned by start_round() and next_round()."""
    round_number: int
    dealer_id:    str
    dealt_hands:  Dict[str, List[Card]]   # player_id → 5 cards (private per player)


@dataclass
class SplitResult:
    """Returned by submit_split()."""
    player_id:       str
    all_split:       bool             # True when every player has now submitted
    next_to_decide:  Optional[str]    # first player in fold-decision queue, if all_split
    still_waiting:   List[str]        # player_ids who haven't split yet


@dataclass
class DecisionResult:
    """Returned by submit_decision() and auto_fold()."""
    player_id:     str
    decision:      str                    # "fold" | "play"
    next_to_decide: Optional[str]         # None when all decisions are in
    round_result:  Optional[RoundResult]  # populated only on the last decision


# ──────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────

class GameError(Exception):
    """Raised when a caller violates game rules or calls in the wrong phase."""


# ──────────────────────────────────────────────
# Game
# ──────────────────────────────────────────────

class Game:
    """
    Pure game logic — no I/O.

    Usage::

        game = Game("room-42")
        game.add_player("alice")
        game.add_player("bob")
        deal = game.start_round()          # → DealResult (send each hand privately)

        game.submit_split("alice", front_cards, back_cards)
        result = game.submit_split("bob", front_cards, back_cards)
        # result.all_split is True → prompt result.next_to_decide

        result = game.submit_decision("alice", "fold")
        result = game.submit_decision("bob",   "play")
        # last decision → result.round_result is populated

        deal = game.next_round()           # advances dealer, deals again
    """

    MIN_PLAYERS = 2
    MAX_PLAYERS = 10

    def __init__(self, room_id: str) -> None:
        self.room_id      = room_id
        self.phase        = Phase.WAITING
        self.round_number = 0

        self._players:        List[Player] = []
        self.dealer_seat:     int = 0           # index into _players
        self._decision_queue: List[str] = []    # player_ids in decision order
        self._decision_pos:   int = 0
        self._fold_sequence:  List[str] = []    # player_ids in the order they folded

    # ── Player management ─────────────────────

    def add_player(self, player_id: str) -> Player:
        """Add a player to the table.  Only allowed in WAITING phase."""
        if self.phase != Phase.WAITING:
            raise GameError("Players can only join between rounds.")
        if any(p.player_id == player_id for p in self._players):
            raise GameError(f"'{player_id}' is already at the table.")
        if len(self._players) >= self.MAX_PLAYERS:
            raise GameError(f"Table is full ({self.MAX_PLAYERS} players max).")
        player = Player(player_id=player_id, seat=len(self._players))
        self._players.append(player)
        return player

    def remove_player(self, player_id: str) -> None:
        """Remove a player.  Only allowed in WAITING phase."""
        if self.phase != Phase.WAITING:
            raise GameError("Players can only leave between rounds.")
        self._players = [p for p in self._players if p.player_id != player_id]
        for i, p in enumerate(self._players):
            p.seat = i

    @property
    def players(self) -> List[Player]:
        """Read-only view of the player list (in seat order)."""
        return list(self._players)

    # ── Round lifecycle ────────────────────────

    def start_round(self) -> DealResult:
        """
        Start the very first round.  Picks a random dealer, deals 5 cards
        to each player, and enters SPLITTING phase.
        """
        if self.phase != Phase.WAITING:
            raise GameError("Use next_round() to continue after the first round.")
        if len(self._players) < self.MIN_PLAYERS:
            raise GameError(f"Need at least {self.MIN_PLAYERS} players to start.")
        self.dealer_seat  = random.randrange(len(self._players))
        self.round_number = 1
        return self._deal_round()

    def next_round(self) -> DealResult:
        """
        Advance the dealer button one seat clockwise and start a new round.
        Must be called from ROUND_END phase.
        """
        if self.phase != Phase.ROUND_END:
            raise GameError(f"next_round() called from phase {self.phase.name}.")
        self.dealer_seat  = (self.dealer_seat + 1) % len(self._players)
        self.round_number += 1
        return self._deal_round()

    def _deal_round(self) -> DealResult:
        """Reset per-round state, deal 5 cards each, enter SPLITTING phase."""
        for p in self._players:
            p.hand       = []
            p.front      = []
            p.back       = []
            p.has_split  = False
            p.decision   = None
            p.fold_order = None
            p.score_round = 0

        self._decision_queue = []
        self._decision_pos   = 0
        self._fold_sequence  = []

        deck = Deck().shuffle()
        dealt: Dict[str, List[Card]] = {}
        for p in self._players:
            p.hand       = deck.deal(5)
            dealt[p.player_id] = list(p.hand)

        self.phase = Phase.SPLITTING
        return DealResult(
            round_number = self.round_number,
            dealer_id    = self._players[self.dealer_seat].player_id,
            dealt_hands  = dealt,
        )

    # ── Splitting ─────────────────────────────

    def submit_split(
        self,
        player_id: str,
        front: List[Card],
        back:  List[Card],
    ) -> SplitResult:
        """
        Record a player's 2+3 card split.  Any player may submit in any order
        during SPLITTING phase.  Once the last player submits, the game
        automatically transitions to FOLD_DECISION.
        """
        if self.phase != Phase.SPLITTING:
            raise GameError(f"submit_split called in phase {self.phase.name}.")

        player = self._get_player(player_id)
        if player.has_split:
            raise GameError(f"'{player_id}' has already submitted their split.")

        self._validate_split(player, front, back)

        player.front     = list(front)
        player.back      = list(back)
        player.has_split = True

        all_split = all(p.has_split for p in self._players)
        if all_split:
            self._build_decision_queue()
            self.phase = Phase.FOLD_DECISION

        return SplitResult(
            player_id      = player_id,
            all_split      = all_split,
            next_to_decide = self.current_decision_player() if all_split else None,
            still_waiting  = self.waiting_for_splits(),
        )

    def _validate_split(
        self,
        player: Player,
        front:  List[Card],
        back:   List[Card],
    ) -> None:
        if len(front) != 2:
            raise GameError(f"Front hand must have exactly 2 cards, got {len(front)}.")
        if len(back) != 3:
            raise GameError(f"Back hand must have exactly 3 cards, got {len(back)}.")
        submitted = sorted(str(c) for c in front + back)
        dealt     = sorted(str(c) for c in player.hand)
        if submitted != dealt:
            raise GameError("Split cards do not match the dealt hand.")

    # ── Fold / play decisions ──────────────────

    def submit_decision(self, player_id: str, decision: str) -> DecisionResult:
        """
        Record a 'fold' or 'play' decision for the current player in the queue.
        Raises GameError if called out of turn or in the wrong phase.
        When the last player decides, the showdown runs automatically and
        DecisionResult.round_result is populated.
        """
        if self.phase != Phase.FOLD_DECISION:
            raise GameError(f"submit_decision called in phase {self.phase.name}.")
        if decision not in ("fold", "play"):
            raise GameError(f"decision must be 'fold' or 'play', got '{decision!r}'.")

        expected = self.current_decision_player()
        if player_id != expected:
            raise GameError(
                f"It is '{expected}'s turn to decide, not '{player_id}'s."
            )

        player          = self._get_player(player_id)
        player.decision = decision

        if decision == "fold":
            player.fold_order = len(self._fold_sequence)
            self._fold_sequence.append(player_id)

        self._decision_pos += 1

        round_result = None
        if self._decision_pos >= len(self._decision_queue):
            # All players have decided — run the showdown
            round_result = self._run_showdown()
            self.phase   = Phase.ROUND_END

        return DecisionResult(
            player_id      = player_id,
            decision       = decision,
            next_to_decide = self.current_decision_player(),
            round_result   = round_result,
        )

    def auto_fold(self, player_id: str) -> DecisionResult:
        """
        Force a fold for the current player.
        Called by the server when the per-turn timer expires.
        Validates that player_id is actually the current turn player.
        """
        return self.submit_decision(player_id, "fold")

    # ── Showdown ──────────────────────────────

    def _run_showdown(self) -> RoundResult:
        """
        Compute fold penalties and run round-robin battles.
        Updates score_round and score_total on every player.

        Fold rule: when a player folds they give 3 points to every player
        who comes AFTER them in the decision queue, regardless of whether
        those later players also fold or choose to play.

        Example (queue: P1 -> P2 -> P3, P1 folds, P2 folds, P3 plays):
          P1 folds at pos 0 -> pays 3 to P2 and P3: P1 -6
          P2 folds at pos 1 -> pays 3 to P3 only:   P2 +3-3 = 0
          P3 plays at pos 2 -> gained 3+3 from folds: P3 +6
        """
        play_players = [p for p in self._players if p.decision == "play"]
        fold_players = sorted(
            (p for p in self._players if p.decision == "fold"),
            key=lambda p: p.fold_order,   # type: ignore[arg-type]
        )

        # ── Fold penalty scoring ───────────────
        # Walk the decision queue in order. Each folder at position i
        # gives 3 to every player at positions i+1 … n-1.
        n = len(self._decision_queue)
        for i, pid in enumerate(self._decision_queue):
            player = self._get_player(pid)
            if player.decision == "fold":
                subsequent_count = n - i - 1
                player.score_round -= 3 * subsequent_count      # folder pays
                for j in range(i + 1, n):
                    recipient = self._get_player(self._decision_queue[j])
                    recipient.score_round += 3                   # each later player gains

        # ── Battle scoring (round-robin) ───────
        battles: List[BattleResult] = []

        for p1, p2 in itertools.combinations(play_players, 2):
            winner, net_pts, swept = battle(p1.front, p1.back, p2.front, p2.back)
            wf, mf = compare_trow(p1.front, p2.front)
            wb, mb = compare_brow(p1.back,  p2.back)

            if winner == 1:
                p1.score_round += net_pts
                p2.score_round -= net_pts
                winner_id = p1.player_id
            elif winner == 2:
                p1.score_round -= net_pts
                p2.score_round += net_pts
                winner_id = p2.player_id
            else:
                winner_id = None

            battles.append(BattleResult(
                p1_id           = p1.player_id,
                p2_id           = p2.player_id,
                winner_id       = winner_id,
                net_points      = net_pts,
                swept           = swept,
                front_winner_id = (p1.player_id if wf == 1 else
                                   p2.player_id if wf == 2 else None),
                back_winner_id  = (p1.player_id if wb == 1 else
                                   p2.player_id if wb == 2 else None),
                front_mult      = mf,
                back_mult       = mb,
                p1_front_label  = hand_label_trow(p1.front),
                p1_back_label   = hand_label_brow(p1.back),
                p2_front_label  = hand_label_trow(p2.front),
                p2_back_label   = hand_label_brow(p2.back),
            ))

        # ── Commit to lifetime totals ──────────
        for p in self._players:
            p.score_total += p.score_round

        return RoundResult(
            round_number    = self.round_number,
            battles         = battles,
            play_player_ids = [p.player_id for p in play_players],
            fold_player_ids = [p.player_id for p in fold_players],
            score_deltas    = {p.player_id: p.score_round for p in self._players},
            scores          = {p.player_id: p.score_total for p in self._players},
        )

    # ── Decision queue helpers ─────────────────

    def _build_decision_queue(self) -> None:
        """
        Decision order: left of dealer first, clockwise, dealer last.
        (Mirrors poker: left of button = UTG, button acts last.)
        """
        n     = len(self._players)
        start = (self.dealer_seat + 1) % n
        self._decision_queue = [
            self._players[(start + i) % n].player_id
            for i in range(n)
        ]
        self._decision_pos = 0

    def current_decision_player(self) -> Optional[str]:
        """The player_id whose fold/play turn it currently is, or None."""
        if self.phase != Phase.FOLD_DECISION:
            return None
        if self._decision_pos >= len(self._decision_queue):
            return None
        return self._decision_queue[self._decision_pos]

    # ── Queries ───────────────────────────────

    def waiting_for_splits(self) -> List[str]:
        """Player IDs who have not yet submitted their split."""
        return [p.player_id for p in self._players if not p.has_split]

    def get_scores(self) -> Dict[str, int]:
        """Current lifetime scores for all players."""
        return {p.player_id: p.score_total for p in self._players}

    def dealer_id(self) -> Optional[str]:
        """Player ID of the current dealer, or None if no players."""
        if not self._players:
            return None
        return self._players[self.dealer_seat].player_id

    def get_state_snapshot(self) -> dict:
        """
        Full state snapshot.  Useful for a reconnecting client or for
        logging.  Does NOT include private hand data — the server must
        send each player's hand separately.
        """
        return {
            "room_id":               self.room_id,
            "phase":                 self.phase.name,
            "round_number":          self.round_number,
            "dealer_id":             self.dealer_id(),
            "current_decision_player": self.current_decision_player(),
            "decision_queue":        list(self._decision_queue),
            "players": [
                {
                    "player_id":   p.player_id,
                    "seat":        p.seat,
                    "has_split":   p.has_split,
                    "decision":    p.decision,
                    "score_total": p.score_total,
                    "score_round": p.score_round,
                }
                for p in self._players
            ],
        }

    # ── Internal helpers ───────────────────────

    def _get_player(self, player_id: str) -> Player:
        for p in self._players:
            if p.player_id == player_id:
                return p
        raise GameError(f"'{player_id}' is not at this table.")
