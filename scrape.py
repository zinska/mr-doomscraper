#!/usr/bin/env python3
"""DoomScraper Phase 1 — turn a social media post URL into a structured Markdown note."""

import argparse
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import anthropic
import openai
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

# --- Where notes are written ----------------------------------------------
# OUTPUT_DIR: base folder for generated notes. Defaults to ./notes so the tool
# works with no Obsidian vault at all. Point it at a folder inside your vault
# to use it with Obsidian.
# Back-compat: if VAULT_PATH is set (and OUTPUT_DIR is not), notes go to
# VAULT_PATH/NOTES_SUBDIR, preserving the original Obsidian layout.
if os.getenv("OUTPUT_DIR"):
    NOTES_BASE = Path(os.getenv("OUTPUT_DIR"))
elif os.getenv("VAULT_PATH"):
    NOTES_BASE = Path(os.getenv("VAULT_PATH")) / os.getenv("NOTES_SUBDIR", "Active projects/doomScraper")
else:
    NOTES_BASE = Path("notes")

TOOL_OUT         = NOTES_BASE / "Tools to investigate"
INSIGHT_OUT      = NOTES_BASE / "Insights to review"
RANDOM_FIND_OUT  = NOTES_BASE / "Random finds"

# --- Templates -------------------------------------------------------------
# TEMPLATE_DIR: folder holding Tool.md / Insight.md / RandomFind.md.
# Defaults to the templates/ folder bundled with this repo, so it works out of
# the box. Point it at your vault's template folder to use your own.
TEMPLATE_DIR     = Path(os.getenv("TEMPLATE_DIR", Path(__file__).parent / "templates"))
TOOL_TPL         = TEMPLATE_DIR / "Tool.md"
INSIGHT_TPL      = TEMPLATE_DIR / "Insight.md"
RANDOM_FIND_TPL  = TEMPLATE_DIR / "RandomFind.md"

# --- Optional personalization ---------------------------------------------
# Comma-separated interests and current projects used to tailor the "Project
# idea" section of Tool notes. Both optional; leave blank for generic ideas.
USER_INTERESTS   = os.getenv("USER_INTERESTS", "").strip()
USER_PROJECTS    = os.getenv("USER_PROJECTS", "").strip()

DEPTH_MAX_SEARCHES = {"L": 3, "M": 7, "D": 12}
DEPTH_LABEL        = {"L": "light", "M": "medium", "D": "deep"}
DEPTH_MODEL        = {"L": "claude-haiku-4-5-20251001", "M": "claude-sonnet-4-6", "D": "claude-sonnet-4-6"}

# Whisper artifact pattern: entire transcript is just noise tags like [Music] or (silence)
_ARTIFACT_RE = re.compile(r'^(?:\s*[\[\(][^\]\)]*[\]\)]\s*)+$', re.IGNORECASE)


def parse_args():
    p = argparse.ArgumentParser(description="DoomScraper: turn a post URL into a Markdown note")
    p.add_argument("url", help="Post URL (Instagram, TikTok, YouTube Shorts, X)")
    p.add_argument(
        "--depth", choices=["L", "M", "D"], default="L",
        help="Research depth: L=light (~3 sources), M=medium (~7), D=deep (~12). Default: L",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-process even if a note for this URL already exists.",
    )
    p.add_argument(
        "--note", default=None, metavar="TEXT",
        help="User-supplied context passed directly to Claude (quoted tweet, extra info, etc.).",
    )
    return p.parse_args()


def get_post_metadata(url: str) -> dict:
    """Fetch post metadata (title, description, uploader, duration) without downloading media."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if any(w in err for w in ("login", "authentication", "cookie", "sign in")):
            print("ERROR: This URL requires browser cookies to access.")
            print()
            print("Fix:")
            print("  1. Install the 'Get cookies.txt LOCALLY' extension in your browser.")
            print("  2. Export cookies from the platform (while logged in).")
            print("  3. Save as  cookies.txt  in this folder.")
            print("  4. In scrape.py, add  'cookiefile': 'cookies.txt'  to ydl_opts.")
        else:
            print(f"ERROR: yt-dlp could not fetch metadata — {e}")
        sys.exit(1)


def build_content(meta: dict, transcript: str | None, user_note: str | None = None) -> str:
    """Combine post metadata and audio transcript into a single content string for Claude."""
    parts = []
    if user_note:
        parts.append(f"User note (treat as authoritative context):\n{user_note}")
    uploader    = meta.get("uploader", "")
    uploader_id = meta.get("uploader_id", "")
    if uploader or uploader_id:
        creator = uploader
        if uploader_id:
            handle = uploader_id if uploader_id.startswith("@") else f"@{uploader_id}"
            creator += f" ({handle})"
        parts.append(f"Creator: {creator.strip()}")
    if meta.get("title"):
        parts.append(f"Title: {meta['title']}")
    if meta.get("description"):
        parts.append(f"Caption / description:\n{meta['description']}")
    if transcript:
        parts.append(f"Audio transcript:\n{transcript}")

    # X/Twitter reply and quote context — include whatever fields yt-dlp returned
    reply_lines = []
    screen_name = meta.get("in_reply_to_screen_name")
    status_id   = meta.get("in_reply_to_status_id_str") or meta.get("in_reply_to_status_id")
    if screen_name and status_id:
        parent_url = f"https://x.com/{screen_name}/status/{status_id}"
        reply_lines.append(f"This post is a reply to: {parent_url}")
    elif meta.get("in_reply_to_user_id"):
        reply_lines.append(f"This post is a reply (in_reply_to_user_id: {meta['in_reply_to_user_id']}, parent URL could not be constructed — status ID missing).")
    for field in ("quoted_status", "quoted_tweet", "referenced_tweets"):
        val = meta.get(field)
        if val:
            reply_lines.append(f"{field}: {val}")
    for field in ("conversation_id", "thread_id"):
        val = meta.get(field)
        if val:
            reply_lines.append(f"{field}: {val}")
    if reply_lines:
        parts.append("Reply / quote context:\n" + "\n".join(reply_lines))

    return "\n\n".join(parts)


def is_meaningful_transcript(text: str) -> bool:
    """Return False if the transcript is empty, too short, all artifact tags, or a Whisper loop."""
    stripped = text.strip()
    if not stripped:
        return False
    words = stripped.split()
    if len(words) < 5:
        return False
    if _ARTIFACT_RE.match(stripped):
        return False
    # Looping detection: if unique words are < 25% of total the model is stuck repeating
    unique = {w.lower().strip(".,!?\"'") for w in words}
    if len(unique) < max(3, len(words) * 0.25):
        return False
    return True


def find_existing_note(url: str) -> Path | None:
    """Return the path of any existing note whose source: frontmatter matches this URL."""
    for folder in (TOOL_OUT, INSIGHT_OUT, RANDOM_FIND_OUT):
        if not folder.exists():
            continue
        for md_file in folder.glob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
                if re.search(rf"^source:\s*{re.escape(url)}\s*$", text, re.MULTILINE):
                    return md_file
            except OSError:
                continue
    return None


def download_audio(url: str, tmpdir: str) -> str:
    out_tpl = os.path.join(tmpdir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio",
        "outtmpl": out_tpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        print(f"ERROR: yt-dlp failed — {e}")
        sys.exit(1)

    files = list(Path(tmpdir).iterdir())
    if not files:
        print("ERROR: yt-dlp ran but produced no output file.")
        sys.exit(1)

    return str(files[0])


def transcribe(audio_path: str) -> str:
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    with open(audio_path, "rb") as f:
        return client.audio.transcriptions.create(model="whisper-1", file=f).text


def load_template(path: Path, depth_label: str) -> str:
    text = path.read_text(encoding="utf-8")
    text = text.replace('<% tp.date.now("YYYY-MM-DD") %>', str(date.today()))
    text = re.sub(r"research_depth: \w+", f"research_depth: {depth_label}", text)
    text = text.replace("# <% tp.file.title %>", "# {{TITLE}}")
    return text


def research(url: str, content: str, depth: str) -> str:
    client       = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    depth_label  = DEPTH_LABEL[depth]
    max_searches = DEPTH_MAX_SEARCHES[depth]
    model        = DEPTH_MODEL[depth]
    print(f"Running depth={depth} with {model}")

    tool_tpl        = load_template(TOOL_TPL, depth_label)
    insight_tpl     = load_template(INSIGHT_TPL, depth_label)
    random_find_tpl = load_template(RANDOM_FIND_TPL, depth_label)

    # Personalize the "Project idea" brainstorming from USER_INTERESTS / USER_PROJECTS
    # (both optional). With neither set, it just suggests a generic concrete project.
    if USER_INTERESTS:
        personalization = (
            "The reader's interests — draw on these when brainstorming project ideas in Tool notes:\n"
            f"{USER_INTERESTS}.\n"
        )
        if USER_PROJECTS:
            personalization += (
                f"\nThe reader's current projects: {USER_PROJECTS}.\n"
                'Only connect a Tool note\'s "Project idea" to one of these if the fit is genuinely very strong.\n'
            )
        personalization += "The default is to suggest a brand-new project idea."
    else:
        personalization = (
            'When brainstorming a Tool note\'s "Project idea", suggest a concrete, specific new '
            "project the reader could build with the tool."
        )

    system = f"""You are a research assistant that turns social media posts into structured Markdown notes.

Today's date: {date.today()}

{personalization}

## Your job

1. Read the post content and URL. The caption/description is the PRIMARY signal — it's what
   the user actually saved. The audio transcript (if present) is SUPPLEMENTARY context only;
   treat it as background, not the main source. Research the topic the caption points at.
2. Decide which of the three categories fits:
   - **Tool**: source pitches a software product, app, platform, model, framework, hardware,
     or any usable tool. Has a name, website, pricing, features. -> use the Tool template.
   - **Insight**: source makes a specific factual claim, prediction, argument, take, or framing
     that could be verified, contested, or chewed on. Has a point to evaluate.
     -> use the Insight template.
   - **Random Find**: source is about a place to visit, restaurant to try, DJ technique,
     recipe, song, book, event, life tip, or anything in the "save for later / remember this"
     bucket. No product to evaluate, no claim to verify — just a thing to check out or try.
     -> use the Random Find template.
   When in doubt between Insight and Random Find: if it could be wrong, it's an Insight;
   if it's just a thing, it's a Random Find.
   If the content is too vague to extract any topic, write a Random Find note and use
   the "Dismiss on reflection" action — never skip silently.
3. Use web_search to research the topic at {depth_label} depth (at most {max_searches} searches).
   Research means: official site, Reddit, Hacker News, X/Twitter, blog reviews.
   Always note the date of community discussion so the user knows how fresh it is.
   The post is a pointer to a topic — not the source of truth. Do the actual research.
   If the post metadata includes a parent post URL (reply/quote context), use web_fetch to
   retrieve it and incorporate its content into the note. If web_fetch on X/Twitter URLs
   fails or returns nothing useful (X blocks automated access), write explicitly in the note:
   "Parent post content not retrievable via web_fetch (X blocks automated access). Note based
   on reply content only." Do NOT guess or fabricate what the parent post said.
4. Fill in EVERY section of the chosen template with real researched content.
   Do not leave any placeholder text or blank sections.
   Fill the `source:` frontmatter field with the post URL.
   Replace {{{{TITLE}}}} with a descriptive, specific title (no special characters).
5. Output ONLY the completed markdown note.
   Start with the opening `---` of the frontmatter. Nothing before it. Nothing after the last line.
   Do NOT write conversational text, explanations, or refusals — output the markdown note only.

CRITICAL: The frontmatter block is machine-parsed by the script that called you.
The `type:` field MUST be present and set to exactly "tool", "insight", or "random-find" — no other values.
An empty, missing, or placeholder frontmatter will break the pipeline. Always output the
complete frontmatter block as the very first thing in your response.

## Tool template
{tool_tpl}

## Insight template
{insight_tpl}

## Random Find template
{random_find_tpl}"""

    user = f"URL: {url}\n\nPost content:\n{content}"

    response = client.messages.create(
        model=model,
        max_tokens=8096,
        system=system,
        tools=[
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": max_searches,
                "allowed_callers": ["direct"],
            },
            {
                "type": "web_fetch_20260209",
                "name": "web_fetch",
                "max_uses": 3,
                "max_content_tokens": 10000,
                "allowed_callers": ["direct"],
            },
        ],
        messages=[{"role": "user", "content": user}],
    )

    full_text = "".join(b.text for b in response.content if b.type == "text")

    # All preamble/fence/frontmatter cleanup is handled by normalize_note.
    return full_text.strip()


def note_is_valid(note: str) -> bool:
    """True if the note looks like a real structured note (not a refusal/clarification)."""
    has_frontmatter = bool(re.search(r"^---\s*$", note, re.MULTILINE))
    has_valid_type  = bool(re.search(r"^type:\s*(tool|insight|random-find)\s*$", note, re.MULTILINE))
    title_match     = re.search(r"^#\s+(.+)$", note, re.MULTILINE)
    has_title       = bool(title_match) and title_match.group(1).strip() not in ("{{TITLE}}", "")
    has_sections    = bool(re.search(r"^##\s+.+$", note, re.MULTILINE))
    return has_frontmatter and has_valid_type and has_title and has_sections


def _infer_type(note: str) -> str:
    """Infer type from template section headers when frontmatter type is absent or invalid."""
    tool_markers        = {"## Pricing", "## Key features", "## What it actually does",
                           "## What people are actually saying"}
    insight_markers     = {"## The topic, in context", "## The specific claim from the source",
                           "## Verifying the claim"}
    random_find_markers = {"## The specifics", "## What people are saying"}
    if any(m in note for m in tool_markers):
        return "tool"
    if any(m in note for m in insight_markers):
        return "insight"
    if any(m in note for m in random_find_markers):
        return "random-find"
    return "random-find"


def _strip_code_fences(text: str) -> str:
    """Drop Markdown code-fence lines wrapping the whole note (``` / ```markdown ... ```)."""
    lines = text.strip().splitlines()
    while lines and lines[0].lstrip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def normalize_note(note: str, url: str) -> str:
    """Rebuild a single clean YAML frontmatter (valid `type:` + `source:`) and body.

    Smaller/cheaper models (depth L = Haiku) mangle the template's frontmatter in
    several ways: collapsing it to a bare '---', dropping the opening '---', wrapping
    the whole note in a ``` code fence, or prepending chatter. `type:` categorizes the
    note and `source:` drives the URL-dedup in find_existing_note, so both must survive.
    Everything up to the first Markdown heading is treated as frontmatter material;
    the heading onward is the body.
    """
    text = _strip_code_fences(note)
    lines = text.splitlines()

    # Skip any leading chatter before the note actually starts.
    start = len(lines)
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s == "---" or s.startswith("#") or re.match(r"^[\w-]+:\s", ln):
            start = idx
            break
    lines = lines[start:]

    # Collect frontmatter fields: everything before the first heading. Skip fence
    # lines and blanks; stop at non-YAML prose (e.g. a refusal) so it lands in the body.
    fields: dict[str, str] = {}
    order: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if ln.startswith("#"):
            break
        if s == "---" or s == "":
            i += 1
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", ln)
        if not m:
            break
        key, val = m.group(1), m.group(2)
        if key not in fields:
            order.append(key)
        fields[key] = val
        i += 1

    body = _strip_code_fences("\n".join(lines[i:]))

    if fields.get("type", "").strip() not in ("tool", "insight", "random-find"):
        fields["type"] = _infer_type(body)
        if "type" not in order:
            order.insert(0, "type")
    if not fields.get("source", "").strip():
        fields["source"] = url
        if "source" not in order:
            order.append("source")

    rebuilt_fm = "\n".join(f"{key}: {fields[key]}" for key in order)
    return f"---\n{rebuilt_fm}\n---\n\n{body}\n"


def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")[:80]


def write_note(note: str, content: str) -> Path:
    # Determine type from frontmatter
    note_type = None
    m = re.search(r"^type:\s*([\w-]+)", note, re.MULTILINE)
    if m and m.group(1).lower() in ("tool", "insight", "random-find"):
        note_type = m.group(1).lower()

    if note_type is None:
        note_type = _infer_type(note)
        print(f"WARNING: frontmatter missing valid type — inferred '{note_type}' from section headers.")
        note = re.sub(r"^type:\s*\S*", f"type: {note_type}", note, flags=re.MULTILINE)

    # Require a real title — missing title is a failure, not a fallback
    title_match = re.search(r"^#\s+(.+)$", note, re.MULTILINE)
    if not title_match or title_match.group(1).strip() in ("{{TITLE}}", ""):
        print("ERROR: Claude's output has no title.")
        print(f"  Post content: {content[:200]}")
        sys.exit(1)
    title = title_match.group(1).strip()

    if note_type == "tool":
        out_dir = TOOL_OUT
    elif note_type == "random-find":
        out_dir = RANDOM_FIND_OUT
    else:
        out_dir = INSIGHT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / (slugify(title) + ".md")
    counter = 1
    while out_path.exists():
        out_path = out_dir / f"{slugify(title)}-{counter}.md"
        counter += 1

    out_path.write_text(note, encoding="utf-8")
    return out_path


def main():
    args = parse_args()

    print("Fetching post metadata...")
    meta = get_post_metadata(args.url)

    if not args.force:
        existing = find_existing_note(args.url)
        if existing:
            print(f"Note for this URL already exists at {existing}. Skipping.")
            print("  Use --force to re-process and overwrite.")
            sys.exit(0)

    transcript = None
    duration = meta.get("duration") or 0
    if duration > 0:
        with tempfile.TemporaryDirectory() as tmpdir:
            print("Downloading audio...")
            audio = download_audio(args.url, tmpdir)
            print("Transcribing...")
            raw_transcript = transcribe(audio)
        if is_meaningful_transcript(raw_transcript):
            transcript = raw_transcript
        else:
            print("Transcript appears meaningless (music/silence/loop) — using caption only.")
    else:
        print("No audio detected — using post text only.")

    content = build_content(meta, transcript, args.note)
    if not content.strip():
        sys.exit("ERROR: No content extracted from this URL (no caption, title, or transcript).")

    # Try the requested depth; if the model refuses or returns an unstructured
    # response, escalate to the next depth up (more context, smarter model).
    depth_order = ["L", "M", "D"]
    note = None
    for depth in depth_order[depth_order.index(args.depth):]:
        candidate = research(args.url, content, depth)
        candidate = normalize_note(candidate, args.url)
        if note_is_valid(candidate):
            note = candidate
            break
        print(f"Depth {depth} did not produce a structured note — escalating to a higher depth.")

    if note is None:
        print("ERROR: Claude did not produce a structured note at any depth.")
        print(f"  Post content: {content[:200]}")
        sys.exit(1)

    out = write_note(note, content)
    print(f"Wrote note: {out}")


if __name__ == "__main__":
    main()
