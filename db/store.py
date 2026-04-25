from __future__ import annotations

import os
from typing import Any, Dict

try:
    from psycopg import connect
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - dependency installed in deployment
    connect = None
    Jsonb = None


def database_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL")) and connect is not None


def _get_conn():
    if connect is None:
        raise RuntimeError("psycopg is not installed.")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured.")
    return connect(dsn)


def load_room_payloads() -> Dict[str, dict]:
    if not database_enabled():
        return {}

    payloads: Dict[str, dict] = {}
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    round_number,
                    phase,
                    dealer_seat,
                    decision_pos,
                    decision_queue,
                    fold_sequence,
                    waiting_players,
                    left_players,
                    archived_scores,
                    game_started
                FROM rooms
                """
            )
            room_rows = cur.fetchall()

            for row in room_rows:
                (
                    room_id,
                    round_number,
                    phase,
                    dealer_seat,
                    decision_pos,
                    decision_queue,
                    fold_sequence,
                    waiting_players,
                    left_players,
                    archived_scores,
                    game_started,
                ) = row

                cur.execute(
                    """
                    SELECT
                        rp.player_id,
                        rp.seat,
                        rp.score_total,
                        rp.score_round,
                        prs.hand_cards,
                        prs.front_cards,
                        prs.back_cards,
                        prs.has_split,
                        prs.decision,
                        prs.fold_order,
                        prs.reveal_on_fold
                    FROM room_players rp
                    LEFT JOIN player_round_state prs
                        ON prs.room_id = rp.room_id
                       AND prs.player_id = rp.player_id
                    WHERE rp.room_id = %s
                      AND rp.status = 'active'
                    ORDER BY rp.seat ASC, rp.player_id ASC
                    """,
                    (room_id,),
                )
                player_rows = cur.fetchall()

                players = []
                for prow in player_rows:
                    (
                        player_id,
                        seat,
                        score_total,
                        score_round,
                        hand_cards,
                        front_cards,
                        back_cards,
                        has_split,
                        decision,
                        fold_order,
                        reveal_on_fold,
                    ) = prow
                    players.append(
                        {
                            "player_id": player_id,
                            "seat": seat,
                            "hand": hand_cards or [],
                            "front": front_cards or [],
                            "back": back_cards or [],
                            "has_split": has_split or False,
                            "decision": decision,
                            "fold_order": fold_order,
                            "reveal_on_fold": reveal_on_fold or False,
                            "score_total": score_total or 0,
                            "score_round": score_round or 0,
                        }
                    )

                payloads[room_id] = {
                    "game": {
                        "room_id": room_id,
                        "phase": phase,
                        "round_number": round_number,
                        "dealer_seat": dealer_seat,
                        "decision_queue": decision_queue or [],
                        "decision_pos": decision_pos or 0,
                        "fold_sequence": fold_sequence or [],
                        "players": players,
                    },
                    "game_started": game_started,
                    "waiting_players": waiting_players or [],
                    "left_players": left_players or [],
                    "archived_scores": archived_scores or {},
                }

    return payloads


def save_room_payload(room_id: str, payload: dict) -> None:
    if not database_enabled():
        return

    game = payload["game"]
    players = game.get("players", [])
    waiting_players = list(payload.get("waiting_players", []))
    left_players = list(payload.get("left_players", []))
    archived_scores = dict(payload.get("archived_scores", {}))
    active_ids = {player["player_id"] for player in players}

    room_player_rows = [
        (
            room_id,
            player["player_id"],
            player["seat"],
            player.get("score_total", 0),
            player.get("score_round", 0),
            "active",
        )
        for player in players
    ]

    for player_id in waiting_players:
        if player_id in active_ids:
            continue
        room_player_rows.append(
            (room_id, player_id, -1, archived_scores.get(player_id, 0), 0, "waiting")
        )

    for player_id in left_players:
        if player_id in active_ids or player_id in waiting_players:
            continue
        room_player_rows.append(
            (room_id, player_id, -1, archived_scores.get(player_id, 0), 0, "left")
        )

    round_state_rows = [
        (
            room_id,
            player["player_id"],
            player.get("hand", []),
            player.get("front", []),
            player.get("back", []),
            player.get("has_split", False),
            player.get("decision"),
            player.get("fold_order"),
            player.get("reveal_on_fold", False),
        )
        for player in players
    ]

    with _get_conn() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rooms (
                        id,
                        round_number,
                        phase,
                        dealer_player_id,
                        dealer_seat,
                        decision_pos,
                        decision_queue,
                        fold_sequence,
                        waiting_players,
                        left_players,
                        archived_scores,
                        game_started,
                        updated_at
                    ) VALUES (
                        %(id)s,
                        %(round_number)s,
                        %(phase)s,
                        %(dealer_player_id)s,
                        %(dealer_seat)s,
                        %(decision_pos)s,
                        %(decision_queue)s,
                        %(fold_sequence)s,
                        %(waiting_players)s,
                        %(left_players)s,
                        %(archived_scores)s,
                        %(game_started)s,
                        NOW()
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        round_number = EXCLUDED.round_number,
                        phase = EXCLUDED.phase,
                        dealer_player_id = EXCLUDED.dealer_player_id,
                        dealer_seat = EXCLUDED.dealer_seat,
                        decision_pos = EXCLUDED.decision_pos,
                        decision_queue = EXCLUDED.decision_queue,
                        fold_sequence = EXCLUDED.fold_sequence,
                        waiting_players = EXCLUDED.waiting_players,
                        left_players = EXCLUDED.left_players,
                        archived_scores = EXCLUDED.archived_scores,
                        game_started = EXCLUDED.game_started,
                        updated_at = NOW()
                    """,
                    {
                        "id": room_id,
                        "round_number": game["round_number"],
                        "phase": game["phase"],
                        "dealer_player_id": players[game["dealer_seat"]]["player_id"]
                        if players and game["dealer_seat"] < len(players)
                        else None,
                        "dealer_seat": game["dealer_seat"],
                        "decision_pos": game.get("decision_pos", 0),
                        "decision_queue": Jsonb(game.get("decision_queue", [])),
                        "fold_sequence": Jsonb(game.get("fold_sequence", [])),
                        "waiting_players": Jsonb(waiting_players),
                        "left_players": Jsonb(left_players),
                        "archived_scores": Jsonb(archived_scores),
                        "game_started": payload.get("game_started", False),
                    },
                )

                cur.execute("DELETE FROM player_round_state WHERE room_id = %s", (room_id,))
                cur.execute("DELETE FROM room_players WHERE room_id = %s", (room_id,))

                for row in room_player_rows:
                    cur.execute(
                        """
                        INSERT INTO room_players (
                            room_id,
                            player_id,
                            seat,
                            score_total,
                            score_round,
                            status,
                            created_at,
                            updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        (
                            row[0],
                            row[1],
                            Jsonb(row[2]),
                            Jsonb(row[3]),
                            Jsonb(row[4]),
                            row[5],
                            row[6],
                            row[7],
                            row[8],
                        ),
                    )

                for row in round_state_rows:
                    cur.execute(
                        """
                        INSERT INTO player_round_state (
                            room_id,
                            player_id,
                            hand_cards,
                            front_cards,
                            back_cards,
                            has_split,
                            decision,
                            fold_order,
                            reveal_on_fold,
                            created_at,
                            updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        row,
                    )


def save_room_payloads(payloads: Dict[str, dict]) -> None:
    if not database_enabled():
        return
    for room_id, payload in payloads.items():
        save_room_payload(room_id, payload)
