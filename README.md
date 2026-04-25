# electricPokdeng

## Postgres persistence

The repo now includes Postgres migrations at:

- [db/migrations/001_initial_schema.sql](/Users/naponice/Documents/projects/elec_pok/electricPokdeng/db/migrations/001_initial_schema.sql)
- [db/migrations/002_room_snapshot_columns.sql](/Users/naponice/Documents/projects/elec_pok/electricPokdeng/db/migrations/002_room_snapshot_columns.sql)

This schema is designed to support durable room recovery for production:

- `rooms`: table-wide phase, round, dealer, and timer deadlines
- `room_players`: persistent seats, scores, and player room status
- `player_round_state`: per-round hand/split/decision state
- `round_results`: showdown history and replay/debug payloads

Card arrays are stored as `jsonb` using the app's existing card-string format, for example:

```json
["2c", "2d", "Ah", "JK", "Ks"]
```

Timer recovery should be driven by persisted deadlines such as:

- `split_deadline_at`
- `decision_deadline_at`

instead of relying only on in-memory Python timers.

### Applying the schema

Example with `psql`:

```bash
psql "$DATABASE_URL" -f db/migrations/001_initial_schema.sql
psql "$DATABASE_URL" -f db/migrations/002_room_snapshot_columns.sql
```

When `DATABASE_URL` is set, the server now loads and saves room snapshots through Postgres instead of `room_state.json`. If `DATABASE_URL` is not set, it falls back to the local JSON snapshot file for development.

This is the first persistence pass. The next major reliability upgrade would be storing timer deadlines and reconstructing active countdowns directly from the database after restart.
