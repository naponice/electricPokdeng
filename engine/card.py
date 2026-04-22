"""
card.py — primitives for the card game engine.
56-card deck: standard 52 + 4 jokers.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List
import random


class Suit(IntEnum):
    CLUBS    = 1
    DIAMONDS = 2
    HEARTS   = 3
    SPADES   = 4


class Rank(IntEnum):
    TWO   = 2
    THREE = 3
    FOUR  = 4
    FIVE  = 5
    SIX   = 6
    SEVEN = 7
    EIGHT = 8
    NINE  = 9
    TEN   = 10
    JACK  = 11
    QUEEN = 12
    KING  = 13
    ACE   = 14


RANK_SYMBOLS = {
    Rank.TWO: "2", Rank.THREE: "3", Rank.FOUR: "4",
    Rank.FIVE: "5", Rank.SIX: "6", Rank.SEVEN: "7",
    Rank.EIGHT: "8", Rank.NINE: "9", Rank.TEN: "T",
    Rank.JACK: "J", Rank.QUEEN: "Q", Rank.KING: "K", Rank.ACE: "A",
}

SUIT_SYMBOLS = {
    Suit.CLUBS: "c", Suit.DIAMONDS: "d",
    Suit.HEARTS: "h", Suit.SPADES: "s",
}


@dataclass(frozen=True, order=False)
class Card:
    rank: Rank
    suit: Suit
    is_joker: bool = False

    def __str__(self) -> str:
        if self.is_joker:
            return "JK"
        return f"{RANK_SYMBOLS[self.rank]}{SUIT_SYMBOLS[self.suit]}"

    def __repr__(self) -> str:
        return str(self)

    def __lt__(self, other: Card) -> bool:
        # Jokers are "unranked" as a raw card — evaluator handles them
        return self.rank < other.rank


# All 52 standard cards, for Joker substitution enumeration
ALL_STANDARD_CARDS: List[Card] = [
    Card(rank, suit)
    for suit in Suit
    for rank in Rank
]

JOKER = Card(rank=Rank.TWO, suit=Suit.CLUBS, is_joker=True)  # sentinel values unused


@dataclass
class Deck:
    """56-card deck. Call shuffle() before dealing."""
    _cards: List[Card] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._cards = list(ALL_STANDARD_CARDS) + [
            Card(Rank.TWO, Suit.CLUBS, is_joker=True) for _ in range(4)
        ]

    def shuffle(self) -> "Deck":
        random.shuffle(self._cards)
        return self

    def deal(self, n: int) -> List[Card]:
        if n > len(self._cards):
            raise ValueError(f"Cannot deal {n} cards — only {len(self._cards)} remain.")
        hand, self._cards = self._cards[:n], self._cards[n:]
        return hand

    def __len__(self) -> int:
        return len(self._cards)
