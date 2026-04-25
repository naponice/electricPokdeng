BEGIN;

CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open',
    round_number INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'WAITING',
    dealer_player_id TEXT,
    decision_pos INTEGER NOT NULL DEFAULT 0,
    split_deadline_at TIMESTAMPTZ,
    decision_deadline_at TIMESTAMPTZ,
    game_started BOOLEAN NOT NULL DEFAULT FALSE,
    darby_winner_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rooms_phase_check CHECK (
        phase IN ('WAITING', 'SPLITTING', 'FOLD_DECISION', 'ROUND_END')
    )
);

CREATE TABLE IF NOT EXISTS room_players (
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    seat INTEGER NOT NULL,
    score_total INTEGER NOT NULL DEFAULT 0,
    score_round INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    left_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (room_id, player_id),
    CONSTRAINT room_players_status_check CHECK (
        status IN ('active', 'waiting', 'left')
    )
);

CREATE TABLE IF NOT EXISTS player_round_state (
    room_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    hand_cards JSONB NOT NULL DEFAULT '[]'::jsonb,
    front_cards JSONB NOT NULL DEFAULT '[]'::jsonb,
    back_cards JSONB NOT NULL DEFAULT '[]'::jsonb,
    has_split BOOLEAN NOT NULL DEFAULT FALSE,
    decision TEXT,
    fold_order INTEGER,
    reveal_on_fold BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (room_id, player_id),
    FOREIGN KEY (room_id, player_id)
        REFERENCES room_players(room_id, player_id)
        ON DELETE CASCADE,
    CONSTRAINT player_round_state_decision_check CHECK (
        decision IS NULL OR decision IN ('play', 'fold', 'fold_reveal')
    )
);

CREATE TABLE IF NOT EXISTS round_results (
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    results_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (room_id, round_number)
);

CREATE INDEX IF NOT EXISTS idx_room_players_room_status
    ON room_players (room_id, status);

CREATE INDEX IF NOT EXISTS idx_room_players_room_seat
    ON room_players (room_id, seat);

CREATE INDEX IF NOT EXISTS idx_player_round_state_room
    ON player_round_state (room_id);

CREATE INDEX IF NOT EXISTS idx_round_results_room_round_desc
    ON round_results (room_id, round_number DESC);

CREATE INDEX IF NOT EXISTS idx_rooms_phase
    ON rooms (phase);

COMMIT;
