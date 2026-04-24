"""
evaluator.py — hand comparison for the split-hand Pok Deng variant.

Each player splits 5 cards into a 2-card front (trow) and a 3-card back (brow).
Hands are compared independently (trow vs trow, brow vs brow) in round-robin.

══════════════════════════════════════════════════════════
2-CARD FRONT HAND (trow) — Baccarat-style point scoring
══════════════════════════════════════════════════════════
  Card point values:
    2–9  →  face value  |  10, J, Q, K  →  0  |  A  →  1  |  Joker  →  wildcard

  Special 7.5-point hands (beat scores 0–7 but lose to 8 and 9):
    7.5 points (face)    — both cards are face cards (J/Q/K)
    7.5 points (55)      — both cards score 5 (two 5s)
    7.5 points (10-10)   — both cards are 10s
    7.5 points (suited)  — both cards score 0 AND are same suit (e.g. K♠–Q♠)

  Normal hand score  =  sum(card values) mod 10   (0–9)

  Tiebreakers (applied in this order):
    1. Score       9 > 8 > 7.5 > 7 > 6 > … > 1 > 0
    2. Pair        any pair beats no pair
    3. Suited      same suit beats mixed
    4. Kicker rank highest card value (2 < 3 < … < K < A)
    5. Kicker suit clubs < diamonds < hearts < spades

  Win multiplier:
    Winner hand has pair OR is suited  →  ×2
    Otherwise                         →  ×1

══════════════════════════════════════════════════════════
3-CARD BACK HAND (brow) — Hand-strength hierarchy
══════════════════════════════════════════════════════════
  Strength (strongest → weakest):
    0  Royal straight flush  Q–K–A same suit          ×10
    1  Straight flush        any 3 consecutive, same   ×5
    2  Trips                 three of a kind            ×5
    3  Zian                  all three are face cards   ×4
    4  Straight              3 consecutive, any suit    ×3
    5  Points                baccarat score, none above ×1 (×3 if suited)

  Within same category, tiebreakers use the highest card rank then suit.
  Zian has additional pair-first tiebreaking (see _zian_tiebreak).

══════════════════════════════════════════════════════════
JOKER — wildcard rules
══════════════════════════════════════════════════════════
  2-card: Joker is converted to make total score exactly 9.
          Two jokers → [A♠, 8♠]  (1 + 8 = 9).

  3-card: Joker tries to complete the best possible hand in priority order:
          wheel-special > trips > straight/SF > gut-shot > wheel-gap > make-9.

══════════════════════════════════════════════════════════
BATTLE
══════════════════════════════════════════════════════════
  compare_trow and compare_brow each return (winner: 0|1|2, multiplier: int).
  battle() calls both and returns (winner, net_points, swept).

  swept=True  → won BOTH hands → net = (trow_mult + brow_mult) × 2
  swept=False → split          → net = winner's total − loser's total
"""

from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from .card import Card, Rank, Suit


# ──────────────────────────────────────────────
# Card attribute helpers
# ──────────────────────────────────────────────

def card_game_score(card: Card) -> int:
    """
    Baccarat-style point value.
    2–9 → face value | 10, J, Q, K → 0 | A → 1 | Joker → 0
    """
    if card.is_joker:
        return 0
    if card.rank in (Rank.TEN, Rank.JACK, Rank.QUEEN, Rank.KING):
        return 0
    if card.rank == Rank.ACE:
        return 1
    return card.rank.value   # 2–9


def is_face(card: Card) -> bool:
    """True for J, Q, K only. Not 10, not Ace, not Joker."""
    return not card.is_joker and card.rank in (Rank.JACK, Rank.QUEEN, Rank.KING)


def _is_suited(cards: List[Card]) -> bool:
    """All non-joker cards share the same suit."""
    real_suits = {c.suit for c in cards if not c.is_joker}
    return len(real_suits) == 1


def _kicker(cards: List[Card]) -> Tuple[int, int]:
    """
    (rank_value, suit_value) of the highest card for tiebreaking purposes.
    Suit.CLUBS=1 < DIAMONDS=2 < HEARTS=3 < SPADES=4  (matches original suit_ranks).
    """
    best_rank = max(c.rank.value for c in cards)
    best_suit = max(c.suit.value for c in cards if c.rank.value == best_rank)
    return best_rank, best_suit


def _pair_rank(cards: List[Card]) -> int:
    """Return the rank value of the paired card (0 if no pair)."""
    counts = Counter(c.rank.value for c in cards)
    for rv, cnt in counts.items():
        if cnt >= 2:
            return rv
    return 0


def _can_straight(rank_vals: List[int]) -> Tuple[bool, int]:
    """
    For a list of rank values, find the minimum gap between any adjacent pair.
    Returns (gap < 3, min_gap).  Gap < 3 means a joker can bridge it.
    """
    sv = sorted(rank_vals)
    min_gap = sv[-1] - sv[0]   # fallback for non-adjacent pairs
    for i in range(len(sv) - 1):
        d = sv[i + 1] - sv[i]
        if d < 3:
            return True, d
        min_gap = min(min_gap, d)
    return False, min_gap


# Internal rank lookup
_VAL_TO_RANK: dict = {r.value: r for r in Rank}


# ──────────────────────────────────────────────
# Joker conversion — 2-card (trow)
# ──────────────────────────────────────────────

def trow_convert_joker(trow: List[Card]) -> List[Card]:
    """
    Replace jokers so that the 2-card hand achieves the highest possible
    baccarat score (target = 9).

    Two jokers → [A♠, 8♠]   (scores 1 + 8 = 9)
    One joker  → card of same suit as companion:
                   companion score 9 → add K (0 pts → total stays 9)
                   companion score 8 → add A (1 pt → total 9)
                   otherwise         → add the card that brings total to 9
    """
    joker_count = sum(1 for c in trow if c.is_joker)

    if joker_count == 0:
        return trow

    if joker_count == 2:
        return [Card(Rank.ACE, Suit.SPADES), Card(Rank.EIGHT, Suit.SPADES)]

    companion  = next(c for c in trow if not c.is_joker)
    score      = card_game_score(companion)
    suit       = companion.suit

    if score == 9:
        joker_rank = Rank.KING          # 0 pts, total stays 9
    elif score == 8:
        joker_rank = Rank.ACE           # 1 pt, total becomes 9
    else:
        needed = 9 - score              # 1–7 for scores 2–8, or 9 for score 0
        joker_rank = _VAL_TO_RANK.get(needed, Rank.NINE)

    return [companion, Card(joker_rank, suit)]


# ──────────────────────────────────────────────
# Joker conversion — 3-card (brow)
# ──────────────────────────────────────────────

def brow_convert_joker(brow: List[Card]) -> List[Card]:
    """
    Replace jokers in a 3-card hand with the best possible card(s).

    3 jokers  → sentinel (handled by compare_brow as automatic win)
    2 jokers  → build the highest straight flush using the real card's suit:
                  A/K/Q → Q-K-A (royal SF)  |  other → r, r+1, r+2
    1 joker   → in priority order:
                  1. Special A-2-3 wheel (non-jokers are 2 and 3)
                  2. Two face cards with a gap (not suited) → trips
                  3. Trips: both non-jokers share same rank
                  4. Connector (gap=1): complete the straight (or SF if suited)
                  5. Gut-shot  (gap=2): fill the middle card
                  6. A-2 gap   (gap=12): add 3 for wheel
                  7. A-3 gap   (vals=[3,14]): add 2 for wheel
                  8. Default: add the card that brings point score closest to 9
    """
    joker_count = sum(1 for c in brow if c.is_joker)

    if joker_count == 0:
        return brow

    if joker_count == 3:
        return brow   # sentinel — compare_brow treats all-joker as auto-win

    non_jokers = [c for c in brow if not c.is_joker]

    # ── 2 jokers + 1 real card ────────────────
    if joker_count == 2:
        card = non_jokers[0]
        suit = card.suit
        r    = card.rank
        if r in (Rank.ACE, Rank.KING, Rank.QUEEN):
            return [Card(Rank.QUEEN, suit), Card(Rank.KING, suit), Card(Rank.ACE, suit)]
        rv = r.value
        return [Card(_VAL_TO_RANK[rv], suit),
                Card(_VAL_TO_RANK[rv + 1], suit),
                Card(_VAL_TO_RANK[rv + 2], suit)]

    # ── 1 joker + 2 real cards ────────────────
    vals   = sorted(c.rank.value for c in non_jokers)   # [low, high]
    suited = _is_suited(non_jokers)
    suit   = non_jokers[0].suit   # only meaningful when suited=True

    # 1. Wheel special: non-jokers are 2 and 3 → complete A-2-3
    if vals == [2, 3]:
        joker_suit = suit if suited else Suit.SPADES
        return non_jokers + [Card(Rank.ACE, joker_suit)]

    straight_ok, distance = _can_straight(vals)

    # 2. Two face cards with a gap, not suited → make trips of the higher face card
    if all(is_face(c) for c in non_jokers) and distance >= 1 and not suited:
        higher     = max(non_jokers, key=lambda c: c.rank.value)
        used_suits = {c.suit for c in non_jokers if c.rank == higher.rank}
        joker_suit = Suit.HEARTS if Suit.SPADES in used_suits else Suit.SPADES
        return non_jokers + [Card(higher.rank, joker_suit)]

    # 3. Trips: both have the same rank
    if distance == 0:
        used_suits = {c.suit for c in non_jokers}
        for s in (Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS):
            if s not in used_suits:
                return non_jokers + [Card(_VAL_TO_RANK[vals[0]], s)]

    # 4. Connector (gap = 1) → complete straight or SF
    elif distance == 1:
        joker_suit = suit if suited else Suit.SPADES
        high_val   = vals[1]
        if high_val >= 13:    # K or A at the top: fill to Q-K-A
            missing = {Rank.QUEEN, Rank.KING, Rank.ACE} - {c.rank for c in non_jokers}
            return non_jokers + [Card(next(iter(missing)), joker_suit)]
        return non_jokers + [Card(_VAL_TO_RANK[high_val + 1], joker_suit)]

    # 5. Gut-shot (gap = 2) → fill the middle card
    elif distance == 2:
        joker_suit = suit if suited else Suit.SPADES
        return non_jokers + [Card(_VAL_TO_RANK[vals[1] - 1], joker_suit)]

    # 6. A-2 gap (14 - 2 = 12) → add 3 for the wheel
    elif distance == 12:
        joker_suit = suit if suited else Suit.SPADES
        return non_jokers + [Card(Rank.THREE, joker_suit)]

    # 7. A-3 gap → add 2 for the wheel
    if vals == [3, 14]:
        joker_suit = suit if suited else Suit.SPADES
        return non_jokers + [Card(Rank.TWO, joker_suit)]

    # 8. Default: add the card that brings baccarat score closest to 9
    current  = sum(card_game_score(c) for c in non_jokers) % 10
    needed   = (9 - current) % 10   # 0–9, modular

    if needed == 0:
        joker_rank = Rank.KING
    elif needed == 1:
        joker_rank = Rank.ACE
    else:
        joker_rank = _VAL_TO_RANK.get(needed, Rank.NINE)

    joker_suit = suit if suited else Suit.SPADES
    return non_jokers + [Card(joker_rank, joker_suit)]


# ──────────────────────────────────────────────
# Kicker comparison helper
# ──────────────────────────────────────────────

def _compare_kicker(rank1: int, suit1: int, rank2: int, suit2: int) -> int:
    """
    Compare two cards: rank first, then suit.
    Returns +1 (card-1 wins), -1 (card-2 wins), or 0 (tie).
    """
    if rank1 != rank2:
        return 1 if rank1 > rank2 else -1
    if suit1 != suit2:
        return 1 if suit1 > suit2 else -1
    return 0


# ──────────────────────────────────────────────
# 2-card comparison  (trow)
# ──────────────────────────────────────────────

def _trow_classify(trow: List[Card]):
    """
    Classify a concrete (joker-free) 2-card hand.
    Returns (score, has_pair, is_suited, kicker_rank, kicker_suit).
    score is 0.0–9.0 or 7.5 for special 7.5-point hands.
    """
    suited = _is_suited(trow)
    pair   = trow[0].rank == trow[1].rank
    kr, ks = _kicker(trow)

    if all(is_face(c) for c in trow):
        return 7.5, pair, True, kr, ks   # Pok face (suited overridden to True)

    if all(card_game_score(c) == 5 for c in trow):
        return 7.5, pair, True, kr, ks   # Pok 55

    if all(c.rank == Rank.TEN for c in trow):
        return 7.5, pair, True, kr, ks   # Pok 10-10

    raw = sum(card_game_score(c) for c in trow) % 10
    if raw == 0 and suited:
        return 7.5, pair, True, kr, ks   # Pok suited-zero (e.g. K♠-Q♠)

    return float(raw), pair, suited, kr, ks


def compare_trow(trow1: List[Card], trow2: List[Card]) -> Tuple[int, int]:
    """
    Compare two 2-card front hands.

    Returns
    -------
    (winner, multiplier)
      winner     : 1 if trow1 wins, 2 if trow2 wins, 0 for tie
      multiplier : 2 if winner has pair or is suited, else 1
    """
    trow1 = trow_convert_joker(trow1)
    trow2 = trow_convert_joker(trow2)

    s1, pair1, suited1, kr1, ks1 = _trow_classify(trow1)
    s2, pair2, suited2, kr2, ks2 = _trow_classify(trow2)

    if s1 != s2:
        winner = 1 if s1 > s2 else 2
    elif pair1 != pair2:
        winner = 1 if pair1 else 2
    elif suited1 != suited2:
        winner = 1 if suited1 else 2
    else:
        res = _compare_kicker(kr1, ks1, kr2, ks2)
        winner = {1: 1, -1: 2, 0: 0}[res]

    if winner == 1:
        return 1, 2 if (pair1 or suited1) else 1
    if winner == 2:
        return 2, 2 if (pair2 or suited2) else 1
    return 0, 0


# ──────────────────────────────────────────────
# 3-card hand classification  (brow)
# ──────────────────────────────────────────────

_HAND_STRENGTH = {
    "royal_straight_flush": 0,
    "straight_flush":       1,
    "trips":                2,
    "zian":                 3,
    "straight":             4,
    "points":               5,
}

_HAND_MULTIPLIER = {0: 10, 1: 5, 2: 5, 3: 3, 4: 3, 5: 1}

BROW_HAND_NAMES = {v: k.replace("_", " ") for k, v in _HAND_STRENGTH.items()}


def _brow_strength(brow: List[Card]) -> int:
    """Return the strength bucket (0–5, lower = stronger) for a concrete 3-card hand."""
    vals   = sorted(c.rank.value for c in brow)
    suited = _is_suited(brow)

    # Trips
    if len(set(vals)) == 1:
        return _HAND_STRENGTH["trips"]

    # Straight: consecutive or wheel (A-2-3)
    is_strt = (
        (vals[2] - vals[0] == 2 and len(set(vals)) == 3)
        or vals == [2, 3, 14]
    )

    if suited and is_strt:
        return (_HAND_STRENGTH["royal_straight_flush"]
                if vals == [12, 13, 14] else
                _HAND_STRENGTH["straight_flush"])

    # Zian checked BEFORE non-suited straight — matches original code order.
    # J-Q-K off-suit is Zian (×4), not Straight (×3), even though it's consecutive.
    if all(is_face(c) for c in brow):
        return _HAND_STRENGTH["zian"]

    if is_strt:
        return _HAND_STRENGTH["straight"]

    return _HAND_STRENGTH["points"]


def _straight_high(brow: List[Card]) -> int:
    """High-card rank value for straight tiebreaking. Wheel (A-2-3) high = 3."""
    vals = sorted(c.rank.value for c in brow)
    return 3 if vals == [2, 3, 14] else vals[2]


# ──────────────────────────────────────────────
# 3-card comparison  (brow)
# ──────────────────────────────────────────────

def compare_brow(brow1: List[Card], brow2: List[Card]) -> Tuple[int, int]:
    """
    Compare two 3-card back hands.

    Returns
    -------
    (winner, multiplier)
      winner     : 1 / 2 / 0
      multiplier : see _HAND_MULTIPLIER; "points" suited → ×3
    """
    brow1 = brow_convert_joker(brow1)
    brow2 = brow_convert_joker(brow2)

    # All-joker sentinel → automatic win
    if all(c.is_joker for c in brow1):
        return 1, 20
    if all(c.is_joker for c in brow2):
        return 2, 20

    suited1 = _is_suited(brow1)
    suited2 = _is_suited(brow2)
    hs1     = _brow_strength(brow1)
    hs2     = _brow_strength(brow2)

    if hs1 < hs2:
        winner = 1
    elif hs2 < hs1:
        winner = 2
    else:
        winner = _brow_tiebreak(brow1, brow2, hs1, suited1, suited2)

    if winner == 0:
        return 0, 0

    winning_hs   = hs1 if winner == 1 else hs2
    winning_suit = suited1 if winner == 1 else suited2

    mult = _HAND_MULTIPLIER[winning_hs]
    if winning_hs == 5 and winning_suit:
        mult = 3   # suited points hand = ×3

    return winner, mult


def _brow_tiebreak(
    brow1: List[Card], brow2: List[Card],
    shared_hs: int,
    suited1: bool, suited2: bool,
) -> int:
    """Resolve a tie within the same hand-strength bucket. Returns 1, 2, or 0."""
    kr1, ks1 = _kicker(brow1)
    kr2, ks2 = _kicker(brow2)

    # RSF, SF, Trips — kicker by high card
    if shared_hs in (0, 1, 2):
        if shared_hs in (1, 4):   # SF or Straight: compare straight-high
            h1 = _straight_high(brow1)
            h2 = _straight_high(brow2)
            # For suit tiebreak, use the highest card's suit
            ks1 = max(c.suit.value for c in brow1 if c.rank.value == max(c.rank.value for c in brow1))
            ks2 = max(c.suit.value for c in brow2 if c.rank.value == max(c.rank.value for c in brow2))
            res = _compare_kicker(h1, ks1, h2, ks2)
        else:
            res = _compare_kicker(kr1, ks1, kr2, ks2)
        return {1: 1, -1: 2, 0: 0}[res]

    if shared_hs == 4:   # Straight
        h1  = _straight_high(brow1)
        h2  = _straight_high(brow2)
        ks1 = max(c.suit.value for c in brow1 if c.rank.value == max(c.rank.value for c in brow1))
        ks2 = max(c.suit.value for c in brow2 if c.rank.value == max(c.rank.value for c in brow2))
        res = _compare_kicker(h1, ks1, h2, ks2)
        return {1: 1, -1: 2, 0: 0}[res]

    if shared_hs == 3:   # Zian
        return _zian_tiebreak(brow1, brow2)

    if shared_hs == 5:   # Points
        return _points_tiebreak(brow1, brow2, suited1, suited2)

    res = _compare_kicker(kr1, ks1, kr2, ks2)
    return {1: 1, -1: 2, 0: 0}[res]


def _zian_tiebreak(brow1: List[Card], brow2: List[Card]) -> int:
    """Tiebreak two Zian hands (all three face cards)."""
    pr1 = _pair_rank(brow1)
    pr2 = _pair_rank(brow2)

    if pr1 and not pr2:
        return 1
    if pr2 and not pr1:
        return 2

    if pr1 and pr2:
        if pr1 != pr2:
            return 1 if pr1 > pr2 else 2
        # Same pair rank → compare the odd (non-paired) card
        nk1 = next(c for c in brow1 if c.rank.value != pr1)
        nk2 = next(c for c in brow2 if c.rank.value != pr2)
        res = _compare_kicker(nk1.rank.value, nk1.suit.value,
                               nk2.rank.value, nk2.suit.value)
        if res != 0:
            return {1: 1, -1: 2}[res]
        # Same kicker → compare pair suit (highest suit of paired cards)
        ps1 = max(c.suit.value for c in brow1 if c.rank.value == pr1)
        ps2 = max(c.suit.value for c in brow2 if c.rank.value == pr2)
        res = _compare_kicker(pr1, ps1, pr2, ps2)
        return {1: 1, -1: 2, 0: 0}[res]

    # No pairs — compare card-by-card from highest to lowest
    s1 = sorted(brow1, key=lambda c: (c.rank.value, c.suit.value), reverse=True)
    s2 = sorted(brow2, key=lambda c: (c.rank.value, c.suit.value), reverse=True)
    for c1, c2 in zip(s1, s2):
        res = _compare_kicker(c1.rank.value, c1.suit.value,
                               c2.rank.value, c2.suit.value)
        if res != 0:
            return {1: 1, -1: 2}[res]
    return 0


def _points_tiebreak(
    brow1: List[Card], brow2: List[Card],
    suited1: bool, suited2: bool,
) -> int:
    """Tiebreak two Points hands (baccarat score, pair, suited, kicker)."""
    s1 = sum(card_game_score(c) for c in brow1) % 10
    s2 = sum(card_game_score(c) for c in brow2) % 10
    if s1 != s2:
        return 1 if s1 > s2 else 2

    if suited1 != suited2:
        return 1 if suited1 else 2

    pr1 = _pair_rank(brow1)
    pr2 = _pair_rank(brow2)
    if (pr1 > 0) != (pr2 > 0):
        return 1 if pr1 else 2

    if pr1 and pr2:
        if pr1 != pr2:
            return 1 if pr1 > pr2 else 2

    kr1, ks1 = _kicker(brow1)
    kr2, ks2 = _kicker(brow2)
    res = _compare_kicker(kr1, ks1, kr2, ks2)
    return {1: 1, -1: 2, 0: 0}[res]


# ──────────────────────────────────────────────
# Battle — combines both hands
# ──────────────────────────────────────────────

def battle(
    p1_front: List[Card], p1_back: List[Card],
    p2_front: List[Card], p2_back: List[Card],
) -> Tuple[int, int, bool]:
    """
    Full head-to-head comparison between two players.

    Returns
    -------
    (winner, net_points, swept)
      winner     : 1 / 2 / 0
      net_points : points the winner collects (loser surrenders the same)
      swept      : True if the winner won BOTH hands (sweep bonus applied)
    """
    wf, mf = compare_trow(p1_front, p2_front)
    wb, mb = compare_brow(p1_back,  p2_back)

    if wf == 1 and wb == 1:
        return 1, (mf + mb) * 2, True
    if wf == 2 and wb == 2:
        return 2, (mf + mb) * 2, True

    score = {1: 0, 2: 0}
    for w, m in [(wf, mf), (wb, mb)]:
        if w in (1, 2):
            score[w] += m

    if score[1] > score[2]:
        return 1, score[1] - score[2], False
    if score[2] > score[1]:
        return 2, score[2] - score[1], False
    return 0, 0, False


# ──────────────────────────────────────────────
# Human-readable labels  (for UI / logging)
# ──────────────────────────────────────────────

def hand_label_brow(brow: List[Card]) -> str:
    """Return a verbose debug description of a 3-card hand."""
    converted = brow_convert_joker(brow)
    if all(c.is_joker for c in converted):
        return "Three jokers | auto win | mult x20"

    hs = _brow_strength(converted)
    suited = _is_suited(converted)
    raw_points = sum(card_game_score(c) for c in converted) % 10
    pair_rank = _pair_rank(converted)
    kicker_rank, kicker_suit = _kicker(converted)
    resolved = _resolved_cards_text(brow, converted)

    if hs == _HAND_STRENGTH["royal_straight_flush"]:
        details = "Royal straight flush Q-K-A"
    elif hs == _HAND_STRENGTH["straight_flush"]:
        details = f"Straight flush {_straight_sequence_text(converted)}"
    elif hs == _HAND_STRENGTH["trips"]:
        details = f"Trips {_rank_name(converted[0].rank.value)}"
    elif hs == _HAND_STRENGTH["zian"]:
        if pair_rank:
            odd = next(c for c in converted if c.rank.value != pair_rank)
            details = (
                f"Zian | pair: {_rank_name(pair_rank)} | odd: {str(odd)}"
            )
        else:
            ordered = " ".join(str(c) for c in _sorted_cards_desc(converted))
            details = f"Zian | pair: no | order: {ordered}"
    elif hs == _HAND_STRENGTH["straight"]:
        high = _straight_high(converted)
        details = f"Straight {_straight_sequence_text(converted)} | high: {_rank_name(high)}"
    else:
        details = (
            f"Points {raw_points} | suited: {'yes' if suited else 'no'} "
            f"| pair: {_rank_name(pair_rank) if pair_rank else 'no'}"
        )

    extra = []
    if hs != _HAND_STRENGTH["points"]:
        extra.append(f"raw points: {raw_points}")
    if hs not in (
        _HAND_STRENGTH["trips"],
        _HAND_STRENGTH["straight"],
        _HAND_STRENGTH["straight_flush"],
        _HAND_STRENGTH["royal_straight_flush"],
        _HAND_STRENGTH["points"],
    ):
        extra.append(f"suited: {'yes' if suited else 'no'}")
    if hs not in (_HAND_STRENGTH["trips"], _HAND_STRENGTH["zian"], _HAND_STRENGTH["points"]):
        extra.append(f"pair: {_rank_name(pair_rank) if pair_rank else 'no'}")
    extra.append(f"kicker: {_rank_suit_text(kicker_rank, kicker_suit)}")

    label = details
    if resolved:
        label += f" | {resolved}"
    if extra:
        label += " | " + " | ".join(extra)
    return label


def hand_strength_brow(brow: List[Card]) -> Tuple[int, str]:
    """Return (strength_bucket, strength_name) for a 3-card back hand."""
    converted = brow_convert_joker(brow)
    if all(c.is_joker for c in converted):
        return 0, "Three jokers"

    hs = _brow_strength(converted)
    return hs, BROW_HAND_NAMES.get(hs, "unknown").replace("_", " ").title()


def hand_label_trow(trow: List[Card]) -> str:
    """Return a verbose debug description of a 2-card hand."""
    converted = trow_convert_joker(trow)
    score, pair, suited, kicker_rank, kicker_suit = _trow_classify(converted)
    resolved = _resolved_cards_text(trow, converted)
    raw_points = sum(card_game_score(c) for c in converted) % 10

    if score == 7.5:
        types = []
        if all(is_face(c) for c in converted):
            types.append("face")
        if all(c.rank == Rank.TEN for c in converted):
            types.append("10-10")
        if all(card_game_score(c) == 5 for c in converted):
            types.append("55")
        if raw_points == 0 and suited:
            types.append("suited")
        label = f"7.5 points | types: {', '.join(types)}"
        parts = [
            f"raw points: {raw_points}",
            f"pair: {_rank_name(converted[0].rank.value) if pair else 'no'}",
            f"suited: {'yes' if suited else 'no'}",
            f"kicker: {_rank_suit_text(kicker_rank, kicker_suit)}",
        ]
        if resolved:
            parts.insert(0, resolved)
        return label + " | " + " | ".join(parts)

    parts = [
        f"Score {int(score)}",
        f"pair: {_rank_name(converted[0].rank.value) if pair else 'no'}",
        f"suited: {'yes' if suited else 'no'}",
        f"kicker: {_rank_suit_text(kicker_rank, kicker_suit)}",
    ]
    if resolved:
        parts.append(resolved)
    return " | ".join(parts)


def _rank_name(rank_value: int) -> str:
    if rank_value <= 0:
        return "none"
    rank = _VAL_TO_RANK[rank_value]
    return {
        Rank.TWO: "2",
        Rank.THREE: "3",
        Rank.FOUR: "4",
        Rank.FIVE: "5",
        Rank.SIX: "6",
        Rank.SEVEN: "7",
        Rank.EIGHT: "8",
        Rank.NINE: "9",
        Rank.TEN: "10",
        Rank.JACK: "J",
        Rank.QUEEN: "Q",
        Rank.KING: "K",
        Rank.ACE: "A",
    }[rank]


def _suit_name(suit_value: int) -> str:
    return {
        Suit.CLUBS.value: "c",
        Suit.DIAMONDS.value: "d",
        Suit.HEARTS.value: "h",
        Suit.SPADES.value: "s",
    }[suit_value]


def _rank_suit_text(rank_value: int, suit_value: int) -> str:
    return f"{_rank_name(rank_value)}{_suit_name(suit_value)}"


def _sorted_cards_desc(cards: List[Card]) -> List[Card]:
    return sorted(cards, key=lambda c: (c.rank.value, c.suit.value), reverse=True)


def _straight_sequence_text(cards: List[Card]) -> str:
    vals = sorted(c.rank.value for c in cards)
    if vals == [2, 3, 14]:
        return "A-2-3"
    return "-".join(_rank_name(v) for v in vals)


def _resolved_cards_text(original: List[Card], converted: List[Card]) -> str:
    if not any(c.is_joker for c in original):
        return ""
    return "as: " + " ".join(str(c) for c in _sorted_cards_desc(converted))
