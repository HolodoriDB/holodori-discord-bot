# holodori-discord-bot

An unofficial **hololive Dreams** (holodori) Discord bot. All game data comes from
**holodori.best's public API** (`api.holodori.best`); images are served from its public CDN.

Ported from the sbuga-bot (Project Sekai) architecture.

## Features

- **Info:** `/ping`, `/help`, `/donate`
- **Cards:** `/card info` — art, stats, skills
- **Songs:** `/song jacket`, `/song info`, `/song difficulty`
- **Holomems:** `/holomem list`, `/holomem info` (with membership stickers), `/holomem leaderboard` (live rating rank)
- **Events:** `/event info`, `/event schedule`, `/event leaderboard`
- **User:** `/user settings`, `/user timezone`
- **Guessing game** (chat `-<guess>`): `/guess jacket` (+ `jacket_30px`, `jacket_bw`, `jacket_challenge`),
  `character` (+ `character_bw`), `chart` (expert), `chart_hard`, `notes`, `music`, `event_background`;
  plus `/guess stats`, `/guess leaderboard`. Per-mode stat tracking (no points/prizes).
- **Developer** (owner-only, mention prefix): `sync`, `reload`/`load`/`unload`, `refresh`, `ban`/`unban`, `eval`

## Setup

1. `pip install -r requirements.txt`
2. Copy `config.example.yml` → `config.yml`; fill in the Discord token, owner IDs, and Postgres
   connection. The `holodori.bypass_value` is required — `api.holodori.best` returns 403 without
   the `x-six-seven` header.
3. Create the schema: `python -m scripts.database_setup`
4. Run: `python main.py`

`/guess music` needs `ffmpeg` on PATH; without it that one mode is unavailable.

## Notes

- `api.holodori.best` read routes need the Cloudflare bypass header (config `holodori.bypass_*`).
  The extracted-asset CDN (`cdn.holodori.dev`) is public, so image URLs embed directly.
- Account/collection features aren't included (holodori accounts are website email/password, not
  in-game IDs). PJSK-only features (gacha, comics, PvP ranked, deck/ISV, aliases) have no holodori
  data source and were dropped.
