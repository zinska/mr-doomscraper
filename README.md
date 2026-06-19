<p align="center">
  <img src="assets/logo.png" alt="Mr. DoomScraper" width="180">
</p>

<h1 align="center">Mr. DoomScraper</h1>

<p align="center">
  Turn the reels, shorts, and posts you doomscroll into <strong>researched, structured Markdown notes</strong> — automatically.
</p>

---

You save a link (or send it to a Telegram bot); DoomScraper pulls the post, transcribes the audio, **researches the actual topic on the web with Claude**, and writes a clean note you can actually act on. The post is treated as a *pointer* to a topic — not the source of truth — so you get real research (official sites, Reddit, Hacker News, reviews with dates), not a reel summary.

Works as a plain command-line tool writing Markdown to a folder, or as an always-on Telegram bot. **No Obsidian required** (but it fits an Obsidian vault nicely).

---

## How it works

```
post URL ──▶ yt-dlp (metadata + audio) ──▶ Whisper (transcript)
                                              │
                                              ▼
                         Claude (web search + web fetch research)
                                              │
                                              ▼
                    structured Markdown note (Tool / Insight / Random Find)
```

Each note is auto-classified into one of three types, each with its own template:

- **Tool** — the post pitches a product/app/model. Note covers what it does, pricing, key features, what people actually say, skepticism flags, and a concrete project idea.
- **Insight** — the post makes a claim or take. Note researches the topic, states the claim, and verifies it against real sources.
- **Random Find** — a place, recipe, technique, song, book, etc. Note captures the concrete specifics and what people say.

**Research depth** is selectable per link:

| Depth | Model | Web searches | Use for |
|-------|-------|--------------|---------|
| `L` (light) | Claude Haiku 4.5 | ~3 | quick capture (default) |
| `M` (medium) | Claude Sonnet 4.6 | ~7 | things worth a real look |
| `D` (deep) | Claude Sonnet 4.6 | ~12 | deep dives |

If a note comes back unstructured, it automatically retries at the next depth up.

---

## Requirements

- **Python 3.10+**
- **[ffmpeg](https://ffmpeg.org/)** (yt-dlp uses it for audio)
- An **[OpenAI API key](https://platform.openai.com/)** (Whisper transcription)
- An **[Anthropic API key](https://console.anthropic.com/)** (Claude research)

> 💸 **Cost:** you pay per note (Whisper transcription + Claude usage). Light notes are cheap; deep notes cost more. You run it; you pay your own API bill.

---

## Quick start (CLI)

```bash
git clone https://github.com/zinska/mr-doomscraper.git
cd mr-doomscraper
pip install -r requirements.txt        # use a venv if you like
cp .env.example .env                    # then add your two API keys
python scrape.py "https://www.instagram.com/reel/XXXXXXXXX/" --depth L
```

The note lands in `./notes/<type folder>/`. That's it — no vault, no config.

Flags:

```
--depth {L,M,D}   research depth (default L)
--note "TEXT"     extra context passed to Claude (e.g. a quoted tweet)
--force           re-process even if a note for this URL already exists
```

Supported sources: anything yt-dlp handles — Instagram, TikTok, YouTube (incl. Shorts), X/Twitter, and more. Some platforms may require browser cookies (see Troubleshooting).

---

## Telegram bot (optional, always-on)

Send links from your phone and get notes back. Runs as a long-polling bot — host it on a small VPS so it works with your computer off.

1. Create a bot with [@BotFather](https://t.me/BotFather), grab the token.
2. Get your numeric user ID from [@userinfobot](https://t.me/userinfobot).
3. Put both in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`). Only that user is allowed; everyone else is rejected.
4. Run it:

```bash
python bot.py
```

Message formats (send any of these to the bot):

```
https://...                 # depth L
M https://...               # depth M
D https://... | extra note  # depth D, with extra context
```

For 24/7 hosting on a Linux VPS, see [`deploy/INSTALL.md`](deploy/INSTALL.md) (systemd unit included).

> ⚠️ A Telegram token can only be polled by **one** process at a time. Run the bot in exactly one place (e.g. the VPS), not on your laptop *and* the server.

---

## Using it with Obsidian

DoomScraper started as an Obsidian tool and still fits one perfectly:

- Set `OUTPUT_DIR` to a folder inside your vault, e.g. `OUTPUT_DIR=/home/you/Vault/Clippings`.
- Keep using the bundled templates, or point `TEMPLATE_DIR` at your own (the section headers must match — they drive note-type detection).
- If your vault syncs via git, set `GIT_SYNC=true` (and `GIT_SYNC_DIR` to the repo) so the bot commits + pushes each new note to all your devices.

---

## Configuration

All config is via `.env` (see [`.env.example`](.env.example)). Everything except the API keys is optional.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | — | **Required.** Whisper transcription |
| `ANTHROPIC_API_KEY` | — | **Required.** Claude research |
| `COOKIES_FILE` | `./cookies.txt` if present | yt-dlp cookies for gated/rate-limited sites |
| `OUTPUT_DIR` | `./notes` | Base folder for generated notes |
| `TEMPLATE_DIR` | bundled `templates/` | Note templates |
| `USER_INTERESTS` | — | Personalizes Tool "Project idea" suggestions |
| `USER_PROJECTS` | — | Current projects to tie ideas to |
| `TELEGRAM_BOT_TOKEN` | — | Bot only |
| `TELEGRAM_USER_ID` | — | Bot only — the one allowed user |
| `GIT_SYNC` | `false` | Bot: auto commit + push new notes |
| `GIT_SYNC_DIR` | `VAULT_PATH`/`OUTPUT_DIR` | Git repo to sync |
| `VAULT_PATH` / `NOTES_SUBDIR` | — | Legacy Obsidian layout (back-compat) |

---

## Troubleshooting

- **"This URL requires browser cookies" / Instagram rate-limited** — some platforms gate access, and Instagram throttles anonymous fetches from datacenter IPs (common on a VPS). Export a `cookies.txt` from a logged-in browser (the "Get cookies.txt LOCALLY" extension), then set `COOKIES_FILE` or just drop the file next to `scrape.py` — it's passed to yt-dlp automatically. Keep `cookies.txt` private; it's gitignored.
- **No audio / meaningless transcript** — DoomScraper detects music/silence/loops and falls back to the caption automatically.
- **Note came back unstructured** — it auto-retries at a higher depth; you can also just rerun with `--depth M`.

---

## License

[MIT](LICENSE) — do what you like.

This tool downloads publicly accessible post media via yt-dlp for personal research. Respect the terms of service of the platforms you use and the rights of content creators.
