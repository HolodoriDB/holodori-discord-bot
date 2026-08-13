# holodori-discord-bot

An unofficial **hololive Dreams** (holodori) Discord bot. All game data comes from [holodori.best](https://holodori.best/)

## Setup
1. `pip install -r requirements.txt`
2. Copy `config.example.yml` as `config.yml` and fill it in
3. Run `python -m scripts.database_setup`
4. Run `python -m scripts.upload_emojis`.
5. Run `python main.py`

`/guess music` needs `ffmpeg` on PATH.

```bash
sudo apt-get install ffmpeg
```