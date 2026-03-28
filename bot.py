#!/usr/bin/env python3
"""
Note Bot — Telegram bot that creates Jekyll posts via the GitHub Contents API.
Send a URL, text, or both → it creates a new post in your _posts/ directory.
"""

import html as html_mod
import os
import re
import base64
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

load_dotenv()

# ── Config (set via .env or environment variables) ────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # e.g. "skynet/skynet.github.io"
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
SITE_URL = os.environ.get("SITE_URL", "https://example.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── GitHub helpers ────────────────────────────────────────────────────────────

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def gh_create_file(client: httpx.AsyncClient, path: str, content: str, message: str):
    """Create a new file in the GitHub repo via the Contents API."""
    url = f"{GH_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
    }
    r = await client.put(url, headers=GH_HEADERS, json=payload, timeout=15)
    r.raise_for_status()


# ── URL / metadata helpers ───────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s]+")


def extract_youtube_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if "youtu.be" in parsed.netloc:
        return parsed.path.lstrip("/").split("?")[0]
    if "youtube.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


def _get_meta(page_html: str, properties: list) -> Optional[str]:
    """Extract the first matching meta tag content from HTML."""
    for prop in properties:
        # Try property= (Open Graph) and name= (Twitter/generic)
        for attr in ("property", "name"):
            pattern = rf'<meta\s+[^>]*{attr}=["\']?{re.escape(prop)}["\']?\s+content=["\']([^"\']+)["\']'
            match = re.search(pattern, page_html, re.IGNORECASE)
            if match:
                return html_mod.unescape(match.group(1).strip())
            # Also try reversed attribute order: content before property
            pattern2 = rf'<meta\s+content=["\']([^"\']+)["\']\s+[^>]*{attr}=["\']?{re.escape(prop)}["\']?'
            match2 = re.search(pattern2, page_html, re.IGNORECASE)
            if match2:
                return html_mod.unescape(match2.group(1).strip())
    return None


def _get_title(page_html: str) -> Optional[str]:
    """Extract <title> tag content."""
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    if match:
        return html_mod.unescape(match.group(1).strip())
    return None


# Titles that indicate a bot-protection challenge page rather than real content
BAD_TITLES = {"just a moment...", "just a moment", "attention required!", "please wait",
              "you are being redirected", "checking your browser", "access denied"}


def _title_from_url_path(url: str) -> Optional[str]:
    """Extract a human-readable title from the URL path as a fallback.

    e.g. "https://medium.com/@dan/my-great-article-abc123" -> "My Great Article"
    """
    path = urlparse(url).path.strip("/")
    # Use the last meaningful path segment
    segment = path.rsplit("/", 1)[-1] if "/" in path else path
    if not segment:
        return None
    # Strip trailing hex IDs common on Medium (e.g. "-a1b2c3d4e5f6")
    segment = re.sub(r"-[0-9a-f]{8,}$", "", segment)
    # Replace hyphens/underscores with spaces, title-case
    title = re.sub(r"[-_]+", " ", segment).strip()
    return title.title() if title else None


async def fetch_og_metadata(client: httpx.AsyncClient, url: str) -> dict:
    """Fetch Open Graph metadata from a URL.

    Returns dict with keys: title, description, image (all optional).
    Falls back to URL-path-based title if the page returns a bot challenge.
    """
    meta = {"title": None, "description": None, "image": None}
    try:
        r = await client.get(
            url, timeout=10, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )
        page = r.text
        meta["title"] = _get_meta(page, ["og:title", "twitter:title"]) or _get_title(page)
        meta["description"] = _get_meta(page, ["og:description", "twitter:description", "description"])
        meta["image"] = _get_meta(page, ["og:image", "twitter:image"])
    except Exception:
        pass

    # Detect Cloudflare/Medium challenge pages and fall back to URL path
    if meta["title"] and meta["title"].lower().strip().rstrip(".") in BAD_TITLES:
        meta["title"] = _title_from_url_path(url)
        meta["description"] = None
        meta["image"] = None

    if not meta["title"]:
        meta["title"] = _title_from_url_path(url)

    return meta


async def fetch_youtube_metadata(client: httpx.AsyncClient, video_id: str) -> dict:
    """Fetch YouTube video metadata via oEmbed."""
    meta = {"title": None, "description": None, "image": None}
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://youtu.be/{video_id}&format=json"
        r = await client.get(oembed_url, timeout=10)
        data = r.json()
        meta["title"] = data.get("title")
        meta["image"] = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        meta["description"] = data.get("author_name", "YouTube")
    except Exception:
        pass
    return meta


# ── Post body builders ───────────────────────────────────────────────────────

def build_preview_card(url: str, meta: dict) -> str:
    """Build an HTML preview card for a URL, similar to messenger link previews.

    Wrapped in {::nomarkdown}...{:/nomarkdown} so kramdown passes the HTML
    through untouched. This is the most reliable way to embed raw HTML in
    kramdown-processed markdown files.
    """
    title = meta.get("title") or urlparse(url).netloc
    description = meta.get("description") or ""
    image = meta.get("image") or ""
    domain = urlparse(url).netloc.replace("www.", "")

    # Truncate description
    if len(description) > 150:
        description = description[:147] + "..."

    # Escape HTML in text content
    title = html_mod.escape(title)
    description = html_mod.escape(description)

    image_html = ""
    if image:
        image_html = f'  <img src="{image}" alt="" loading="lazy">\n'

    desc_html = ""
    if description:
        desc_html = f'    <span class="link-preview-desc">{description}</span>\n'

    return (
        f'{{::nomarkdown}}\n'
        f'<div class="link-preview">\n'
        f'  <a href="{url}" target="_blank" rel="noopener">\n'
        f'{image_html}'
        f'    <span class="link-preview-text">\n'
        f'      <strong>{title}</strong>\n'
        f'{desc_html}'
        f'      <span class="link-preview-domain">{domain}</span>\n'
        f'    </span>\n'
        f'  </a>\n'
        f'</div>\n'
        f'{{:/nomarkdown}}'
    )


# ── Slug / post helpers ──────────────────────────────────────────────────────

def slugify(text: str, max_length: int = 50) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:max_length].rstrip("-")


def build_post(title: str, body: str, now: datetime,
               slug_override: Optional[str] = None,
               description: Optional[str] = None) -> Tuple[str, str]:
    """Build a Jekyll post file content and filename.

    Returns (filename, file_content).
    """
    slug = slugify(slug_override or title)
    # Append HHMM to avoid slug collisions on the same day
    filename = f"_posts/{now:%Y-%m-%d}-{slug}-{now:%H%M}.md"

    # Escape quotes in title for YAML
    safe_title = title.replace('"', '\\"')

    lines = [
        "---",
        f'title: "{safe_title}"',
        f"date: {now:%Y-%m-%d %H:%M:%S} +0000",
        "layout: note",
        "categories: [notes]",
    ]
    if description:
        safe_desc = description.replace('"', '\\"')
        lines.append(f'description: "{safe_desc}"')
    lines.append("---")
    lines.append("")
    lines.append("")

    front_matter = "\n".join(lines)
    return filename, front_matter + body


# ── Telegram handlers ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Send me a URL, some text, or both.")
        return

    await update.message.reply_text("Saving...")

    try:
        now = datetime.now(timezone.utc)
        urls = URL_RE.findall(text)
        non_url_text = URL_RE.sub("", text).strip()

        async with httpx.AsyncClient() as client:
            if urls:
                url = urls[0]

                # Fetch metadata
                yt_id = extract_youtube_id(url)
                if yt_id:
                    meta = await fetch_youtube_metadata(client, yt_id)
                else:
                    meta = await fetch_og_metadata(client, url)

                fetched_title = meta.get("title") or urlparse(url).netloc or url
                preview_card = build_preview_card(url, meta)

                if non_url_text:
                    # URL + commentary: commentary text + preview card
                    title = non_url_text[:80]
                    body = f"{non_url_text}\n\n{preview_card}\n"
                    filename, content = build_post(
                        title, body, now,
                        slug_override=fetched_title,
                        description=non_url_text,
                    )
                else:
                    # URL only: just the preview card
                    title = fetched_title
                    desc = meta.get("description") or title
                    body = f"{preview_card}\n"
                    filename, content = build_post(
                        title, body, now,
                        description=desc,
                    )

                await gh_create_file(client, filename, content, f"Add note: {title[:60]}")

            else:
                # Plain text note
                title = text[:80]
                body = f"{text}\n"
                filename, content = build_post(title, body, now)
                await gh_create_file(client, filename, content, f"Add note: {title[:60]}")

        # Use the same slug the post file used
        if urls and non_url_text:
            slug = slugify(fetched_title)
        else:
            slug = slugify(title)
        permalink = f"{SITE_URL}/blog/{slug}-{now:%H%M}/"
        await update.message.reply_text(f"Posted: {permalink}")

    except Exception as e:
        log.exception("Failed to create post")
        await update.message.reply_text(f"Error: {e}")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "Note Bot ready.\n\n"
        "Send me:\n"
        "- A URL — creates a note with a link preview\n"
        "- Text + URL — your commentary with a link preview\n"
        "- Plain text — saved as a quick thought\n\n"
        "Each message becomes a post on your blog."
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Note Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
