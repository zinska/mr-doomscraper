# Deploying the DoomScraper bot on a Linux VPS (systemd)

Run the Telegram bot 24/7 so it works with your computer off. Steps assume a
fresh Debian/Ubuntu box; adjust paths/user to taste. The included unit file
uses `/opt/doomscraper` and a `doomscraper` user — change both if you prefer.

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv git ffmpeg
```

## 2. Get the code

```bash
sudo mkdir -p /opt/doomscraper && sudo chown "$USER" /opt/doomscraper
git clone https://github.com/zinska/mr-doomscraper.git /opt/doomscraper
cd /opt/doomscraper
```

## 3. Virtualenv + dependencies

```bash
python3 -m venv /opt/doomscraper/venv
/opt/doomscraper/venv/bin/pip install -r requirements.txt
```

## 4. Configure

```bash
cp .env.example .env
nano .env
```

Fill in at least:

```
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_USER_ID=<numeric ID from @userinfobot>
```

Optional: set `OUTPUT_DIR` (and `GIT_SYNC=true` + `GIT_SYNC_DIR` if you want
notes auto-pushed to a git-synced vault).

## 5. Install and start the service

```bash
sudo cp deploy/doomscraper-bot.service /etc/systemd/system/
# edit the unit if you changed the user/paths:
sudo nano /etc/systemd/system/doomscraper-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now doomscraper-bot
```

## 6. Watch logs

```bash
journalctl -u doomscraper-bot -f
```

You should see `Bot starting` followed by repeated `getUpdates ... 200 OK`.
Send a link to your bot from Telegram to test.

## Useful commands

```bash
systemctl status doomscraper-bot    # health check
systemctl restart doomscraper-bot   # after pulling new code
journalctl -u doomscraper-bot -n 50 # recent logs
```

> ⚠️ Run the bot in **one** place only — a Telegram token can't be polled by two
> processes at once (they'll conflict with `409`).
