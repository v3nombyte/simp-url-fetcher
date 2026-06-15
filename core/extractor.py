"""Per-post URL extraction from forum HTML files.

Supports SimpCity, SocialMediaGirls, and CelebForum formats.
Extracts URLs grouped by individual forum post.
"""

import os
import re
import json
import base64
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

from .models import Post, ScanResult


# ── Config ──────────────────────────────────────────────────────────

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# User-editable files (not in git). Created by user or first-run copy from .default.json
PATTERNS_PATH = os.path.join(PROJECT_DIR, "url_patterns.json")
SKIP_PATH = os.path.join(PROJECT_DIR, "skip_url.json")
# Version-controlled default templates (shipped with repo)
PATTERNS_DEFAULT_PATH = os.path.join(PROJECT_DIR, "url_patterns.default.json")
SKIP_DEFAULT_PATH = os.path.join(PROJECT_DIR, "skip_url.default.json")

# Default patterns to match if file doesn't exist
DEFAULT_PATTERNS = [
    "bunkr\\.cr", "bunkr\\.ru", "bunkr\\.ac", "bunkr\\.ci",
    "bunkr\\.fi", "bunkr\\.media", "bunkr\\.black", "bunkr\\.ph",
    "bunkr\\.pk", "bunkr\\.red", "bunkr\\.si", "bunkr\\.site",
    "bunkr\\.sk", "bunkr\\.ws", "bunkrrr\\.org", "bunkr\\.ax",
    "bunkr\\.bz", "bunkr\\.cat", "bunkrr\\.su",
    "cyberdrop\\.me", "cyberfile\\.me", "cyberfile\\.su",
    "cyberdrop\\.cr", "cyberfiles-static\\.b-cdn\\.net",
    "pixl\\.li", "jpg4\\.su", "jpg5\\.su", "jpg6\\.su", "jpg7\\.cr",
    "jpg\\.church",
    "pixeldrain\\.com",
    "saint2\\.su",
    "selti-delivery\\.ru",
    "simpcity\\.cr/attachments/",
    "celebforum\\.to/attachments/",
    "forums\\.socialmediagirls\\.com/attachments",
    "smgmedia\\.socialmediagirls\\.com",
    "anonfiles\\.com",
    "gofile\\.io",
    "imgbox\\.com",
    "giphy\\.com",
    "mega\\.nz",
    "mega\\.co\\.nz",
    "host\\\\.church",
    "media\\.redgifs\\.com",
    "media\\.imagepond\\.net/media/",
    "www\\.imagebam\\.com/image",
    "1fichier\\.com",
    "drive\\.google\\.com/drive/folders",
    "e-hentai\\.org/g/",
    "cdn\\.camwhores\\.tv/contents/",
    "gotanynudes\\.com/",
    "i\\.gyazo\\.com",
    "i\\.imgur\\.com",
    "turbo\\.cr/",
    "redgifs\\.com/",
]

DEFAULT_SKIP = [
    "jpg6\\.su/upload",
    "jpg6\\.su/sdk/pup-sc",
    "coomer\\.party",
    "gofile\\.io/dist/img",
    "bunkr\\.fi/images/fav",
    "fav\\.ico",
    "favicon",
    "preview",
    "thumbnail",
    "thumb_",
    "simp.*selti-delivery\\.ru/simpo/data/avatars",
    "pixeldrain\\.com/res/img",
    "celebforum\\.to/data/assets/",
    "celebforum\\.to/data/attachments/",
    "celebforum\\.to/threads",
    "forums\\.socialmediagirls\\.com/threads",
    "x\\.com/",
    "reddit\\.com/",
    "instagram\\.com/",
    "cover\\.png",
]


# ── Helpers ─────────────────────────────────────────────────────────

def load_json(path, default):
    """Load a JSON file or return default."""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def extract_page_number(filename: str) -> int:
    """Extract page number from filename like '... Page 2 ...' or '... Page 10 ...'."""
    m = re.search(r'[Pp]age\s*(\d+)', filename)
    if m:
        return int(m.group(1))
    return 1


def identify_forum_source(soup: BeautifulSoup) -> str:
    """Identify the forum source from HTML metadata."""
    try:
        og = soup.find('meta', property='og:url')
        if og and og.get('content') and 'celebforum.to' in og['content']:
            return 'celebforum'
        title_tag = soup.find('title')
        if title_tag and 'Social Media Girls' in title_tag.text:
            return 'socialmediagirls'
        icon = soup.find('link', rel='icon')
        if icon and icon.get('href') and 'simpcity' in icon['href']:
            return 'simpcity'
    except Exception:
        pass
    return ''


def get_base_url(soup: BeautifulSoup) -> str:
    """Get base URL from og:url meta tag."""
    try:
        og = soup.find('meta', property='og:url')
        if og and og.get('content'):
            return og['content']
    except Exception:
        pass
    return ''


# ── URL Extraction ──────────────────────────────────────────────────

def extract_post_urls(post_article, base_url: str) -> list[str]:
    """
    Extract all image/media URLs from a single post article.
    Strips signatures and quoted content, extracts from img/a/source tags,
    srcset, inline styles, and attachment sections.
    """
    urls: set[str] = set()

    # Strip quoted content to avoid duplicate URLs (CoomerDL-inspired)
    for quote in post_article.select("blockquote.bbCodeBlock--quote, .bbCodeBlock--quote"):
        quote.decompose()

    # Strip signature if it's inside the post
    for sig in post_article.select(".message-signature"):
        sig.decompose()

    # Tags and their URL attributes to scan
    attrs = {
        "a": "href",
        "img": "src",
        "script": "src",
        "link": "href",
        "iframe": "src",
        "source": "src",
        "video": "src",
    }

    # 1. Standard tag attributes
    for tag, attr in attrs.items():
        for element in post_article.find_all(tag):
            url = element.get(attr) or element.get("data-url") or element.get("data-src")
            if url:
                urls.add(url.strip())

    # 2. noscript fallback images
    for ns_img in post_article.select("noscript img"):
        url = ns_img.get("src")
        if url:
            urls.add(url.strip())

    # 3. Attachment links (SocialMediaGirls format)
    for attach_link in post_article.select("a.file-preview"):
        url = attach_link.get("href")
        if url:
            urls.add(url.strip())

    # 3b. Attachment section links (SimpCity format — CoomerDL-inspired)
    for attach_sec in post_article.select("section.message-attachments"):
        for a_tag in attach_sec.find_all("a", href=True):
            url = a_tag.get("href")
            if url:
                urls.add(url.strip())

    # 3c. Protocol-relative URLs in onclick handlers (loadMedia, popup, etc.)
    # SimpCity embeds redgifs/turbo.cr/etc. via JS onclick instead of iframe
    for element in post_article.find_all(onclick=True):
        onclick = element.get("onclick", "")
        # loadMedia(this, '//redgifs.com/ifr/...')
        for m in re.finditer(r"""['"](//[^'"]+)['"]""", onclick):
            url = m.group(1)
            if url.startswith("//"):
                # Resolve protocol-relative to absolute
                url = "https:" + url
            if url.startswith("http"):
                urls.add(url)

    # 4. srcset attributes
    for element in post_article.find_all(srcset=True):
        for part in element["srcset"].split(","):
            url = part.strip().split(" ")[0].strip()
            if url:
                urls.add(url)

    # 5. Inline styles (background-image)
    style_pattern = re.compile(r'url\(["\']?(.*?)["\']?\)')
    for element in post_article.find_all(style=True):
        for match in style_pattern.findall(element["style"]):
            url = match.strip()
            if url:
                urls.add(url)

    # 6. Resolve relative URLs to absolute
    if base_url:
        urls = {urljoin(base_url, url) for url in urls}

    # 7. Keep only http/https URLs
    urls = {url for url in urls if url.startswith("http://") or url.startswith("https://")}

    # 8. Decode simpcity.cr redirect URLs to actual target URLs
    #    e.g. https://simpcity.cr/redirect/?to=aHR0cHM6Ly9nb2ZpbGUuaW8vZC9HZEZHbHg&e=1&m=b64
    resolved = set()
    for url in urls:
        parsed = urlparse(url)
        if ("simpcity.cr" in parsed.netloc or "simpcity.su" in parsed.netloc) and "/redirect/" in parsed.path:
            qs = parse_qs(parsed.query)
            to_b64 = qs.get("to", [""])[0]
            if to_b64:
                try:
                    # Add padding for base64 decode
                    padded = to_b64 + "=" * ((4 - len(to_b64) % 4) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
                    if decoded.startswith("http"):
                        # Replace with the real URL — it will be filtered below
                        url = decoded
                        parsed = urlparse(url)
                except Exception:
                    pass
            # Failed to decode — skip the redirect URL entirely
            if "simpcity.cr" in parsed.netloc:
                continue
        # Filter noise: CDN emoji assets, member profile pages
        if "cdn.jsdelivr.net" in parsed.netloc:
            continue
        if parsed.netloc == "simpcity.cr" and parsed.path.startswith("/members/"):
            continue
        if parsed.netloc == "www.instagram.com":
            continue
        if parsed.netloc in ("x.com", "www.x.com", "www.reddit.com", "reddit.com"):
            continue
        if parsed.netloc in ("t.me", "www.tiktok.com", "tiktok.com"):
            continue
        resolved.add(url)

    return sorted(resolved)


def filter_urls(urls: list[str], patterns: list[str], skip_patterns: list[str]) -> list[str]:
    """
    Filter URLs: keep those matching any pattern, skip those matching skip patterns.
    """
    result = []
    for url in urls:
        # Check skip patterns first
        skipped = False
        for sp in skip_patterns:
            if re.search(sp, url, re.IGNORECASE):
                skipped = True
                break
        if skipped:
            continue
        # Check include patterns (any match = keep)
        matched = False
        for p in patterns:
            if re.search(p, url, re.IGNORECASE):
                matched = True
                break
        if matched:
            result.append(url)
    return result


# ── Main Extraction ────────────────────────────────────────────────

def extract_from_html(
    html_path: str,
    patterns: list[str] | None = None,
    skip_patterns: list[str] | None = None,
    mode: str = "normal",
) -> ScanResult:
    """
    Extract posts and URLs from a single HTML file.

    Args:
        html_path: Path to the HTML file.
        patterns: URL inclusion patterns (regex).
        skip_patterns: URL skip patterns (regex).
        mode: "normal" (apply both), "no_filter" (all URLs), "reverse" (NOT matching patterns).

    Returns:
        ScanResult with posts.
    """
    if patterns is None:
        patterns = load_json(PATTERNS_PATH, None) or load_json(PATTERNS_DEFAULT_PATH, DEFAULT_PATTERNS)
    if skip_patterns is None:
        skip_patterns = load_json(SKIP_PATH, None) or load_json(SKIP_DEFAULT_PATH, DEFAULT_SKIP)

    filename = os.path.basename(html_path)
    page_num = extract_page_number(filename)

    with open(html_path, 'r', encoding='utf-8', errors='replace') as f:
        soup = BeautifulSoup(f, 'lxml')

    forum_source = identify_forum_source(soup)
    base_url = get_base_url(soup)

    model_name = ""
    name_match = re.search(r'(.+?)\s*[_|]\s*(?:Page\s*\d+|SimpCity|Social Media)', filename)
    if name_match:
        model_name = name_match.group(1).strip()
    else:
        # Fallback: use directory name or filename
        model_name = filename.split('(')[-1].split(')')[-1].strip() if '(' in filename else filename

    result = ScanResult(
        model_name=model_name,
        forum_source=forum_source,
    )

    # Dedup URLs globally even within a single page (e.g. quoted posts)
    seen_urls: set[str] = set()

    # Find all posts
    post_articles = soup.select("article.message-body")
    if not post_articles:
        # Fallback: try broader selector
        post_articles = soup.select("[class*='message'] [class*='body']")

    for idx, article in enumerate(post_articles, start=1):
        post_id = article.get("id", "") or ""
        # Get post ID from parent article
        parent = article.find_parent("article", class_="message")
        if parent:
            post_id = parent.get("id", "")
        author = article.get("data-author", "") or parent.get("data-author", "") if parent else ""
        if not author:
            author_el = article.find_previous("a", class_="username")
            if author_el:
                author = author_el.text.strip()

        urls = extract_post_urls(article, base_url)

        if mode == "normal":
            urls = filter_urls(urls, patterns, skip_patterns)
        elif mode == "reverse":
            # Keep URLs NOT matching patterns
            urls = [u for u in urls if not any(re.search(p, u) for p in patterns)]

        # Globally dedup URLs within this page
        unique_urls = [u for u in urls if u not in seen_urls]
        seen_urls.update(unique_urls)

        post = Post(
            post_id=post_id or f"post-{page_num}-{idx}",
            page=page_num,
            post_index=idx,
            author=author,
            urls=unique_urls,
            source_file=filename,
        )
        result.posts.append(post)

    result.total_posts = len(result.posts)
    result.total_urls = sum(len(p.urls) for p in result.posts)

    return result


def extract_from_folder(
    folder_path: str,
    patterns: list[str] | None = None,
    skip_patterns: list[str] | None = None,
    mode: str = "normal",
) -> list[ScanResult]:
    """
    Extract from all HTML files in a folder (flat, no recursion into subfolders).

    Returns a list of ScanResult objects (one per file).
    """
    results = []
    for fname in sorted(os.listdir(folder_path)):
        if fname.endswith('.html'):
            fpath = os.path.join(folder_path, fname)
            try:
                sr = extract_from_html(fpath, patterns, skip_patterns, mode)
                results.append(sr)
            except Exception as e:
                print(f"  [ERROR] Failed to process '{fname}': {e}")
    return results


def merge_results(results: list[ScanResult], model_name: str = "") -> ScanResult:
    """Merge multiple ScanResults into one, deduplicating URLs globally."""
    if not results:
        return ScanResult(model_name=model_name or "unknown", forum_source="")

    merged = ScanResult(
        model_name=model_name or results[0].model_name,
        forum_source=results[0].forum_source,
    )
    seen_urls: set[str] = set()
    for r in results:
        for post in r.posts:
            # Remove URLs already seen in earlier posts (e.g. quoted content)
            unique_urls = [u for u in post.urls if u not in seen_urls]
            seen_urls.update(unique_urls)
            merged.posts.append(Post(
                post_id=post.post_id,
                page=post.page,
                post_index=post.post_index,
                author=post.author,
                urls=unique_urls,
                source_file=post.source_file,
            ))
    merged.total_posts = len(merged.posts)
    merged.total_urls = sum(len(p.urls) for p in merged.posts)
    return merged
