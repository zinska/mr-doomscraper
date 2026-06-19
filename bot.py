#!/usr/bin/env python3
"""DoomScraper Phase 2 — Telegram bot that wraps scrape.py."""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_uid = os.getenv("TELEGRAM_USER_ID")

if not TELEGRAM_BOT_TOKEN:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN is not set in .env")
if not _uid:
    sys.exit("ERROR: TELEGRAM_USER_ID is not set in .env")

TELEGRAM_USER_ID = int(_uid)

# Where scrape.py writes notes (resolution mirrors scrape.py).
if os.getenv("OUTPUT_DIR"):
    NOTES_DIR = Path(os.getenv("OUTPUT_DIR"))
elif os.getenv("VAULT_PATH"):
    NOTES_DIR = Path(os.getenv("VAULT_PATH")) / os.getenv("NOTES_SUBDIR", "Active projects/doomScraper")
else:
    NOTES_DIR = Path("notes")

# Optional: auto commit + push new notes to a git repo (e.g. a git-synced
# Obsidian vault). Off by default — plain local files work with no git.
GIT_SYNC     = os.getenv("GIT_SYNC", "false").lower() in ("1", "true", "yes")
GIT_SYNC_DIR = Path(os.getenv("GIT_SYNC_DIR") or os.getenv("VAULT_PATH") or NOTES_DIR)

SCRAPE_PY = Path(__file__).parent / "scrape.py"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# "[DEPTH] URL [| note]"   depth and note are optional
_MSG_RE = re.compile(
    r"^(?:(?P<depth>[LMDlmd])\s+)?(?P<url>https?://\S+)(?:\s*\|\s*(?P<note>.+))?$",
    re.DOTALL,
)

_URL_RE = re.compile(r"https?://\S+")

# Depth markers inside a *separate* context message (mobile = text msg, then URL msg).
# Explicit "depth X" (optionally wrapped in dashes/colon) wins over a bare standalone letter.
_DEPTH_EXPLICIT_RE   = re.compile(r"(?:^|\s|[—–-])depth\s*[:\-—–]?\s*([LMD])\b", re.IGNORECASE)
# Bounded by start/end, whitespace, or dashes only — NOT apostrophes, so "I'm" won't match "m".
_DEPTH_STANDALONE_RE = re.compile(r"(?:^|(?<=[\s—–-]))([LMD])(?=$|[\s—–-])", re.IGNORECASE)

# chat_id -> (monotonic_ts, text) of the most recent non-URL message, held for pairing.
_LAST_MSG: dict[int, tuple[float, str]] = {}
_PAIR_WINDOW_SECONDS = 90


def _clean_note(s: str) -> str:
    """Trim whitespace and stray dash/pipe/colon punctuation left after stripping the depth marker."""
    return s.strip().strip("—–-|: ").strip()


def parse_context(text: str) -> tuple[str, str | None]:
    """Parse a standalone context message into (depth, note). Depth defaults to L; note is the rest."""
    m = _DEPTH_EXPLICIT_RE.search(text)
    if not m:
        m = _DEPTH_STANDALONE_RE.search(text)
    depth = None
    if m:
        depth = m.group(1).upper()
        text = text[:m.start()] + " " + text[m.end():]
    return (depth or "L"), (_clean_note(text) or None)


def _fmt_note(note: str | None) -> str:
    if not note:
        return "no note"
    shown = note if len(note) <= 120 else note[:117] + "..."
    return f"note: '{shown}'"


async def _run_scrape(url: str, depth: str, note: str | None) -> tuple[int, str, str]:
    cmd = [sys.executable, str(SCRAPE_PY), url, "--depth", depth]
    if note:
        cmd += ["--note", note]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "scrape.py timed out after 5 minutes"
    output = stdout.decode(errors="replace")
    return proc.returncode, output, ""


async def _git_commit_push(filename: str) -> tuple[bool, str]:
    repo = str(GIT_SYNC_DIR)
    steps = [
        ["git", "-C", repo, "pull", "--rebase"],
        ["git", "-C", repo, "add", str(NOTES_DIR)],
        ["git", "-C", repo, "commit", "-m", f"doomScraper: {filename}"],
        ["git", "-C", repo, "push"],
    ]
    for step in steps:
        proc = await asyncio.create_subprocess_exec(
            *step,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, f"git step timed out: {' '.join(step[2:])}"
        if proc.returncode != 0:
            out = stdout.decode(errors="replace").strip()
            return False, f"`{' '.join(step[2:])}` failed:\n{out}"
    return True, ""


def _extract_filename(scrape_output: str) -> str | None:
    for line in scrape_output.splitlines():
        if line.startswith("Wrote note:"):
            path = line.split("Wrote note:", 1)[1].strip()
            return Path(path).name
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != TELEGRAM_USER_ID:
        sender_id = user.id if user else "unknown"
        log.warning("Rejected message from unauthorized sender_id=%s", sender_id)
        await update.message.reply_text("Not authorized.")
        return

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    url_match = _URL_RE.search(text)

    # No URL: stash as context for a URL message that may arrive next, then wait.
    if not url_match:
        if text:
            _LAST_MSG[chat_id] = (time.monotonic(), text)
            log.info("Stored context for chat=%s: %s", chat_id, text)
        return

    url = url_match.group(0)
    remainder = (text[:url_match.start()] + text[url_match.end():]).strip()

    if remainder:
        # Old desktop one-liner: URL shares the message with other text.
        m = _MSG_RE.match(text)
        if m:
            depth = (m.group("depth") or "L").upper()
            url = m.group("url")
            note = (m.group("note") or "").strip() or None
        else:
            # URL embedded in free-form text — treat the rest as context.
            depth, note = parse_context(remainder)
        _LAST_MSG.pop(chat_id, None)  # self-contained; never pair with older text
    else:
        # Bare URL (mobile share): pair with the most recent text message in the window.
        prev = _LAST_MSG.pop(chat_id, None)
        if prev and (time.monotonic() - prev[0]) <= _PAIR_WINDOW_SECONDS:
            depth, note = parse_context(prev[1])
        else:
            depth, note = "L", None

    log.info("Processing depth=%s url=%s note=%s", depth, url, note)
    ack = await update.message.reply_text(
        f"⏳ Processing at depth {depth} with {_fmt_note(note)}"
    )

    returncode, output, err = await _run_scrape(url, depth, note)

    if returncode == -1:
        log.error("scrape.py timeout for url=%s", url)
        await ack.edit_text(f"❌ {err}")
        return

    # Dedup: scrape.py exits 0 and prints "already exists"
    if returncode == 0 and "already exists" in output.lower():
        log.info("Duplicate URL skipped: %s", url)
        await ack.edit_text("ℹ️ A note for this URL already exists.")
        return

    if returncode != 0:
        log.error("scrape.py failed (rc=%d) for url=%s\n%s", returncode, url, output)
        snippet = output.strip()[-800:] if output.strip() else "(no output)"
        await ack.edit_text(f"❌ scrape.py failed:\n```\n{snippet}\n```", parse_mode="Markdown")
        return

    filename = _extract_filename(output)
    log.info("Note written: %s", filename)
    note_name = filename or "note"

    if not GIT_SYNC:
        await ack.edit_text(f"✅ Note saved: `{note_name}`", parse_mode="Markdown")
        return

    ok, git_err = await _git_commit_push(filename or "new note")
    if not ok:
        log.error("git failed: %s", git_err)
        await ack.edit_text(
            f"✅ Note saved: `{note_name}`\n"
            f"⚠️ Git push failed — commit manually:\n{git_err}",
            parse_mode="Markdown",
        )
        return

    await ack.edit_text(f"✅ Note saved and pushed: `{note_name}`", parse_mode="Markdown")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting — authorized user_id=%d", TELEGRAM_USER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
