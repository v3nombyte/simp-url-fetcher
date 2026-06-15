"""
SimpScraper download client.

Replaces JDownloader for downloading extracted media URLs.
Host support:
  - Direct pass-through: imgur, pixl.li, postimg, ibb, catbox, saint2, etc.
  - Referer-based: pixhost.to, imagepond, imagebam, imgbox
  - API-based: gofile.io, pixeldrain.com, redgifs.com
  - HTTP scrape: bunkr, cyberdrop, coomer, sendvid, erome
  - Playwright: jpg6.su, turbo.cr, selti-delivery (legacy)
"""

import asyncio
import json
import os
import re
import shutil
import time
import threading
import html as html_mod
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import zlib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from .unpacker import try_extract_archive

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

BS4_AVAILABLE = False
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    pass

MEGA_AVAILABLE = False
try:
    from mega import Mega
    MEGA_AVAILABLE = True
except Exception:
    pass


_log_callback = None  # Set by app.py to pipe logs to webapp UI
_gofile_token = None  # Set by resolver for CDN download auth


def _is_album_url(url: str) -> tuple[str, str] | None:
    """Check if a URL is an album/list page that needs expansion.
    Returns (host_type, page_url) or None.
    host_type is 'cyberdrop_album' or 'pixeldrain_list'."""
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path
    if 'cyberdrop' in host and ('/a/' in path):
        return ('cyberdrop_album', url)
    if 'pixeldrain' in host and '/l/' in path:
        return ('pixeldrain_list', url)
    return None


def _scrape_album_files(host_type: str, page_url: str,
                         session: requests.Session) -> list[dict]:
    """Scrape an album/list page for individual file URLs.
    Returns list of dicts: [{'url': str, 'filename': str}, ...]"""
    files = []
    try:
        resp = session.get(page_url, timeout=20,
                           headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if resp.status_code != 200:
            return files
        html = resp.text

        if host_type == 'cyberdrop_album':
            # CyberDrop album pages have file links in the page
            # Pattern: href="/XXXXX/filename.ext" or direct file links
            if BS4_AVAILABLE:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if any(href.lower().endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov', '.mkv', '.avi')):
                        full_url = urljoin(page_url, href)
                        fname = os.path.basename(urlparse(full_url).path)
                        if fname:
                            files.append({'url': full_url, 'filename': fname})
            else:
                # Regex fallback
                for m in re.finditer(r'href=\"([^\"]+\.(?:jpg|jpeg|png|gif|webp|mp4|webm|mov|mkv|avi))\"', html, re.I):
                    full_url = urljoin(page_url, m.group(1))
                    fname = os.path.basename(urlparse(full_url).path)
                    if fname:
                        files.append({'url': full_url, 'filename': fname})

        elif host_type == 'pixeldrain_list':
            # Return a single ZIP archive entry instead of individual file URLs
            list_id = urlparse(page_url).path.strip('/').split('/')[-1]
            file_count = 0
            try:
                api_resp = session.get(
                    f'https://pixeldrain.com/api/list/{list_id}',
                    timeout=15,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                if api_resp.status_code == 200:
                    api_data = api_resp.json()
                    if api_data.get('success'):
                        file_count = len(api_data.get('files', []))
            except Exception:
                pass
            # Encode count so the download loop can update its total
            files.append({
                'url': f'__PIXELDRAIN_LIST_ZIP__:{list_id}:{file_count}',
                'filename': f'pixeldrain_{list_id}.zip'
            })

    except Exception as e:
        _log(f'Failed to scrape album {page_url[:60]}: {e}', 'ERROR')
    return files

def _log(msg: str, level: str = 'INFO'):
    ts = time.strftime('%H:%M:%S')
    print(f'[{ts}] [{level}] {msg}')
    if _log_callback:
        _log_callback(msg, level)


def _set_pixeldrain_cookies(session: requests.Session, cookies_json: str):
    """Parse a JSON array of cookies and set pd_auth_key on the session."""
    if not cookies_json:
        return
    try:
        cookies = json.loads(cookies_json)
        if not isinstance(cookies, list):
            return
        for c in cookies:
            name = c.get('name', '')
            value = c.get('value', '')
            domain = c.get('domain', '')
            if name == 'pd_auth_key' and value:
                session.cookies.set(name, value, domain=domain or 'pixeldrain.com')
                _log(f'Set pd_auth_key cookie for pixeldrain.com', 'INFO')
                return
    except (json.JSONDecodeError, TypeError) as e:
        _log(f'Failed to parse pixeldrain cookies JSON: {e}', 'WARN')


# ── Host classification ─────────────────────────────────────────────

# URLs to these hosts can be downloaded as-is (direct image/video URLs).
# Hosts resolvable via Chevereto oEmbed API (no browser needed)
OEMBED_HOSTS = {
    'jpg4.su', 'jpg5.su', 'jpg6.su', 'jpg7.cr', 'jpg.church', 'host.church',
}

DIRECT_HOSTS = {
    'i.imgur.com', 'imgur.com',
    'i.postimg.cc', 'postimg.cc', 'postimgs.org',
    'ibb.co', 'i.ibb.co',
    'simgbb.com',
    'files.catbox.moe', 'catbox.moe',
    'i.ytimg.com',
    'pbs.twimg.com',
    'media1.tenor.com',
    'lh3.googleusercontent.com',
    'i.kym-cdn.com',
    'i0.wp.com',
    'cdn.turbo.cr',
    'thcf4.redgifs.com',
    'thumbs.saint2.cr', 'saint2.cr', 'saint2.su', 'tp2.saint2.su',
    'i.marcus.pw',
    'simpcity.cr',
    'simp6.cuckcapital.cr', 'simp6.cuckcapital.cr.cdn',
    'smgmedia.socialmediagirls.com',
    'images.socialmediagirls.com',
    'cdn.camwhores.tv',
    'i.gyazo.com',
    's6.erome.com', 's48.erome.com', 's57.erome.com', 's58.erome.com', 's109.erome.com',
    'static.bunkr.ru',  # bunkr CDN subdomains are direct
    'cdn10.bunkr.ru', 'cdn11.bunkr.ru', 'cdn12.bunkr.ru',
    'cdn4.bunkr.ru', 'cdn8.bunkr.ru', 'cdn9.bunkr.ru',
    'i-beer.bunkr.ru', 'i-burger.bunkr.ru', 'i-fries.bunkr.ru',
    'i-kebab.bunkr.ru', 'i-maple.bunkr.ru', 'i-meatballs.bunkr.ru',
    'i-milkshake.bunkr.ru', 'i-nachos.bunkr.ru', 'i-pizza.bunkr.ru',
    'i-rice.bunkr.ru', 'i-soup.bunkr.ru', 'i-sushi.bunkr.ru',
    'i-taquito.bunkr.ru', 'i-wiener.bunkr.ru', 'i-wings.bunkr.ru',
    'i-ramen.bunkr.ru', 'i-bacon.bunkr.ru', 'i-cake.bunkr.ru',
    'cdn2.bunkr.sk', 'cdn3.bunkr.sk', 'cdn6.bunkr.sk',
    'stream.bunkr.sk', 'dash.bunkr.pk',
    'f.cyberdrop.cc', 'cdn.cyberdrop.to', 'fs-01.cyberdrop.to',
    'static2.onlyfans.com',
    'media.imagepond.net',
    'thumbs2.imagebam.com',
    'static.scdn.st',
}

# URLs that need a Referer header to serve content.
REFERER_HOSTS = {
    'pixhost.to', 't84.pixhost.to', 't95.pixhost.to', 't96.pixhost.to',
    'www.imagepond.net',
    'imgbox.com', 'thumbs2.imgbox.com',
    'leakimedia.com',
    'fanspornclips.com',
    'pixl.li', 'i.pixl.li', 'i3.pixl.li',  # some need referer
}

# Hosts resolved by scraping the page HTML.
SCRAPE_HOSTS = {
    'bunkr.cr', 'bunkr.sk', 'bunkr.media', 'bunkr.site', 'bunkrrr.org',
    'bunkrr.su', 'bunkr.ws', 'bunkr.bz', 'bunkr.cat', 'bunkr.fi',
    'bunkr.red', 'bunkr.si', 'bunkr.black', 'bunkr.ac',
    'bunkr.ci', 'bunkr.ph', 'bunkr.pk', 'bunkr.ax', 'bunkr.ps',
    'cyberdrop.to', 'cyberdrop.me', 'cyberdrop.cc', 'cyberdrop.cr',
    'cyberfile.su', 'cyberfile.me',
    'erome.com', 'www.erome.com',
    'redgifs.com', 'www.redgifs.com',
    'sendvid.com', 'www.sendvid.com',
    'coomer.party', 'coomer.st',
    'porntn.com', 'hqporner.com', 'm.hqporner.com',
    'nobodyhome.tv',
    'thothub.to',
    'www.imagebam.com', 'imagebam.com',
    'www.mediafire.com', 'mediafire.com',
    'wetransfer.com', 'www.wetransfer.com',
    'we.tl',
}

# Hosts with public REST APIs.
API_HOSTS = {
    'gofile.io', 'www.gofile.io',
    'pixeldrain.com', 'www.pixeldrain.com',
}


# ── URL Resolution ──────────────────────────────────────────────────

class URLResolver:
    """Resolve protected/media URLs to directly downloadable URLs."""

    DIRECT_MAP = {
        'simp6.selti-delivery.ru': 'simp6.cuckcapital.cr',
        'simp1.selti-delivery.ru': 'simp1.cuckcapital.cr',
        'simp2.selti-delivery.ru': 'simp2.cuckcapital.cr',
        'simp3.selti-delivery.ru': 'simp3.cuckcapital.cr',
        'simp4.selti-delivery.ru': 'simp4.cuckcapital.cr',
        'simp5.selti-delivery.ru': 'simp5.cuckcapital.cr',
    }

    def __init__(self, pixeldrain_api_key: str = "", pixeldrain_cookies_json: str = ""):
        self._playwright_ctx = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._resolve_count = 0
        self._pixeldrain_api_key = pixeldrain_api_key
        self._session = self._build_session(self._pixeldrain_api_key)
        if pixeldrain_cookies_json:
            _set_pixeldrain_cookies(self._session, pixeldrain_cookies_json)

    def _build_session(self, pixeldrain_api_key: str = "") -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })
        if pixeldrain_api_key:
            s.headers.update({'X-API-Key': pixeldrain_api_key})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount('http://', adapter)
        s.mount('https://', adapter)
        return s

    # ── Browser management ──────────────────────────────────────────

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        if not PLAYWRIGHT_AVAILABLE:
            _log('Playwright not available — browser resolution disabled', 'WARN')
            return
        try:
            self._playwright_ctx = async_playwright()
            self._playwright = await self._playwright_ctx.__aenter__()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            self._context = await self._browser.new_context(
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
            )
            await self._context.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
            )
        except Exception as e:
            _log(f'Browser launch failed: {e}', 'WARN')
            self._browser = None

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright_ctx:
            await self._playwright_ctx.__aexit__(None, None, None)
        self._browser = None
        self._playwright = None
        self._playwright_ctx = None
        self._session.close()

    # ── Host classification helpers ─────────────────────────────────

    @staticmethod
    def _get_netloc(url: str) -> str:
        return urlparse(url).netloc.lower()

    @staticmethod
    def _host_matches(host: str, hosts_set: set) -> bool:
        """Check if host matches any entry in a set (supports suffix match)."""
        if host in hosts_set:
            return True
        # Handle www. prefix variants
        if host.startswith('www.') and host[4:] in hosts_set:
            return True
        if not host.startswith('www.') and f'www.{host}' in hosts_set:
            return True
        return False

    def classify_host(self, url: str) -> str:
        """Classify a URL into resolution strategy: direct|referer|api|scrape|browser|unknown."""
        host = self._get_netloc(url)

        # Known direct map
        if host in self.DIRECT_MAP:
            return 'browser'  # needs transformation + possible browser
        if self._host_matches(host, DIRECT_HOSTS):
            return 'direct'
        if self._host_matches(host, REFERER_HOSTS):
            return 'referer'
        if self._host_matches(host, API_HOSTS):
            return 'api'
        if self._host_matches(host, SCRAPE_HOSTS):
            return 'scrape'

        # jpg*.su/church/cr — Chevereto oEmbed API
        if self._host_matches(host, OEMBED_HOSTS):
            return 'oembed'
        # turbo.cr
        if 'turbo.cr' in host:
            return 'browser'
        # mega.nz
        if 'mega.nz' in host or 'mega.co.nz' in host:
            return 'mega'

        return 'unknown'

    # ── Resolution methods ─────────────────────────────────────────

    def resolve_direct(self, url: str) -> Optional[str]:
        """Transform selti-delivery → cuckcapital URLs."""
        parsed = urlparse(url)
        host = parsed.netloc

        if host in self.DIRECT_MAP:
            new_url = url.replace(host, self.DIRECT_MAP[host])
            new_url = re.sub(r'\.md\.(jpg|png|jpeg|webp|gif|mp4)$', r'.\1', new_url)
            return new_url

        return None

    def resolve_passthrough(self, url: str) -> str:
        """For direct hosts, return the URL as-is (no transformation needed)."""
        return url

    def resolve_referer(self, url: str) -> dict:
        """Return download info with a Referer header for specific hosts."""
        host = self._get_netloc(url)
        referer_map = {
            'pixhost.to': 'https://pixhost.to/',
            't84.pixhost.to': 'https://pixhost.to/',
            't95.pixhost.to': 'https://pixhost.to/',
            't96.pixhost.to': 'https://pixhost.to/',
            'www.imagepond.net': 'https://www.imagepond.net/',
            'www.imagebam.com': 'https://www.imagebam.com/',
            'thumbs2.imagebam.com': 'https://www.imagebam.com/',
            'imgbox.com': 'https://imgbox.com/',
            'thumbs2.imgbox.com': 'https://imgbox.com/',
        }
        # Try exact, then with www, then without
        ref = referer_map.get(host) or referer_map.get(host.replace('www.', '')) or referer_map.get(f'www.{host}')
        return {'url': url, 'referer': ref or url}

    def resolve_api(self, url: str) -> Optional[str]:
        """Resolve via host API (gofile.io, pixeldrain.com)."""
        host = self._get_netloc(url)
        path = urlparse(url).path.strip('/')

        try:
            if 'gofile' in host:
                return self._resolve_gofile(url, path)
            elif 'pixeldrain' in host:
                return self._resolve_pixeldrain(url, path)
        except Exception as e:
            _log(f'API resolution failed for {url[:60]}: {e}', 'ERROR')
        return None

    def _resolve_gofile(self, url: str, path: str) -> Optional[str]:
        """Resolve gofile.io contentId → direct download URL.
        Uses Gofile API v2 with optional guest token.
        """
        content_id = path.split('/')[-1] if path else ''
        if not content_id or content_id in ('gofile.io', 'www.gofile.io'):
            return None

        # Step 1: Get guest token
        token = None
        try:
            acct = self._session.post('https://api.gofile.io/accounts', json={}, timeout=10)
            if acct.status_code == 200:
                data = acct.json()
                if data.get('status') == 'ok':
                    token = data['data']['token']
        except Exception:
            pass

        # Store guest token for subsequent CDN downloads from Gofile
        global _gofile_token
        _gofile_token = token

        # Step 2: Get content info
        api_url = f'https://api.gofile.io/contents/{content_id}'
        params = {}
        if token:
            params['token'] = token
            params['wt'] = '4fd6sg89d7s6'
        resp = self._session.get(api_url, params=params, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get('status') != 'ok':
            return None

        contents = data.get('data', {}).get('contents', {})
        for cid, cdata in contents.items():
            link = cdata.get('link', '')
            if link:
                return link
        return None

    def _resolve_pixeldrain(self, url: str, path: str) -> Optional[str]:
        """Resolve pixeldrain.com file hash → direct download URL.
        Handles /u/{hash} (file), /l/{hash} (list), and /api/file/{hash} (API).
        When the API hotlink-block is detected, fall back to scraping
        the user-facing page for a non-API download URL."""
        parts = [p for p in path.split('/') if p]
        if len(parts) < 2:
            return None
        kind = parts[0]
        # Extract the actual file ID regardless of URL pattern
        if kind == 'api' and len(parts) >= 3 and parts[1] == 'file':
            file_id = parts[2]  # /api/file/{fid}
        elif kind in ('u', 'l'):
            file_id = parts[1]  # /u/{fid} or /l/{fid}
        else:
            return None

        if kind == 'l':
            # List/collection — handled by _scrape_album_files
            return None

        # Check the info endpoint for availability
        try:
            info_resp = self._session.get(
                f'https://pixeldrain.com/api/file/{file_id}/info',
                timeout=10,
            )
            if info_resp.status_code == 200:
                info = info_resp.json()
                if isinstance(info, dict):
                    availability = info.get('availability', '')
                    if availability == 'file_rate_limited_captcha_required':
                        _log(f'PixelDrain {file_id}: requires captcha/subscription', 'WARN')
                        return f'__PIXELDRAIN_BLOCKED__:{file_id}'
                    # If availability is empty/null, allow the download
        except Exception:
            pass

        return f'https://pixeldrain.com/api/file/{file_id}'

    def _resolve_redgifs(self, url: str, path: str) -> Optional[str]:
        """Resolve redgifs.com page → video URL via the RedGifs v2 API.
        Uses anonymous token (no auth required) and returns HD MP4 URL.
        Falls back to page scrape (og:video) if API fails.
        """
        vid = path.split('/')[-1] if path else ''
        if not vid or vid in ('redgifs.com', 'www.redgifs.com'):
            return None
        # Try API first (token cached in instance)
        try:
            tok = getattr(self, '_redgifs_token', None)
            if not tok:
                tr = self._session.get('https://api.redgifs.com/v2/auth/temporary', timeout=15)
                if tr.status_code == 200:
                    self._redgifs_token = tr.json().get('token', '')
                    tok = self._redgifs_token
            if tok:
                ar = self._session.get(f'https://api.redgifs.com/v2/gifs/{vid}', headers={
                    'Authorization': f'Bearer {tok}',
                }, timeout=15)
                if ar.status_code == 200:
                    urls = ar.json().get('gif', {}).get('urls', {})
                    hd = urls.get('hd') or urls.get('sd')
                    if hd:
                        return hd
                elif ar.status_code == 401:
                    self._redgifs_token = ''
        except Exception:
            pass
        # Fallback: scrape og:video from the watch page
        try:
            resp = self._session.get(f'https://www.redgifs.com/watch/{vid}', timeout=15)
            if resp.status_code == 200:
                m = re.search(
                    r'<meta\s+property="og:video"\s+content="([^"]+)"',
                    resp.text, re.I
                )
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    # ── Chevereto oEmbed resolution ──────────────────────────────

    def resolve_oembed(self, url: str) -> Optional[str]:
        """Resolve jpg*.su URLs via Chevereto oEmbed API.
        
        For /img/ URLs: uses the oEmbed API to get the cuckcapital CDN URL.
        For /a/ (album) URLs: scrapes the album page for the first image's URL.
        Returns the full-size image URL from cuckcapital CDN.
        """
        from urllib.parse import quote, urlparse
        
        parsed = urlparse(url)
        path = parsed.path
        host = parsed.netloc.lower()
        
        # Album URLs — scrape for individual image links
        if path.startswith('/a/'):
            return self._resolve_jpg_album(url)
        
        # Image URLs — use oEmbed API
        # Use the actual host (jpg6.su, jpg7.cr, etc.) for the oEmbed endpoint
        oembed_url = f'https://{host}/oembed/?url={quote(url, safe="")}&format=json'
        if host in ('jpg5.su', 'jpg4.su', 'jpg.church', 'host.church'):
            # Older Chevereto instances might use different oEmbed path
            oembed_url = f'https://{host}/oembed/?url={quote(url, safe="")}&format=json'
        try:
            resp = self._session.get(oembed_url, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            thumb_url = data.get('url')
            if not thumb_url:
                return None
            # Full-size: remove .md from thumbnail URL
            # Chevereto suffix format: <hash>.md.jpg → <hash>.jpg
            full_url = re.sub(r'\.md\.(jpg|png|jpeg|webp|gif)$', r'.\1', thumb_url)
            return full_url
        except Exception:
            return None

    def _resolve_jpg_album(self, url: str) -> Optional[str]:
        """Scrape a jpg6.su album page for image URLs.
        Returns the first image's full URL (each image will be resolved individually).
        """
        try:
            resp = self._session.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                return None
            html = resp.text
            
            if not BS4_AVAILABLE:
                # Regex fallback: find data-object JSON blobs
                for m in re.finditer(r'data-object=\'([^\']+)\'', html):
                    try:
                        import json
                        obj = json.loads(html_mod.unescape(m.group(1)))
                        full_url = obj.get('image', {}).get('url', '')
                        if full_url:
                            return full_url
                    except:
                        pass
                return None
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Try data-object attribute first (contains full URLs)
            for item in soup.select('[data-object]'):
                try:
                    obj = json.loads(html_mod.unescape(item['data-object']))
                    full_url = obj.get('image', {}).get('url', '')
                    if full_url:
                        return full_url
                except:
                    pass
                break  # Only need the first one
            
            # Fallback: find the first image container link
            first_link = soup.select_one('a.image-container.--media[href]')
            if first_link:
                img_url = urljoin(url, first_link['href'])
                # Resolve this image URL via oEmbed recursively
                return self.resolve_oembed(img_url)
            
            return None
        except Exception:
            return None

    # ── HTTP scrape resolution ─────────────────────────────────────

    def resolve_scrape(self, url: str) -> Optional[str]:
        """Scrape page HTML to find actual media URL (bunkr, cyberdrop, etc.)."""
        host = self._get_netloc(url)

        try:
            if 'bunkr' in host:
                return self._scrape_bunkr(url)
            elif 'cyberdrop' in host or 'cyberfile' in host:
                return self._scrape_cyberdrop(url)
            elif 'erome' in host:
                return self._scrape_erome(url)
            elif 'redgifs' in host:
                path = urlparse(url).path.strip('/')
                return self._resolve_redgifs(url, path)
            elif 'sendvid' in host:
                return self._scrape_sendvid(url)
            elif 'coomer' in host:
                return self._scrape_coomer(url)
            elif 'mediafire' in host:
                return self._scrape_mediafire(url)
            elif 'porntn' in host or 'hqporner' in host:
                return self._scrape_generic_video(url)
            elif 'nobodyhome' in host:
                return self._scrape_generic_video(url)
            elif 'thothub' in host:
                return self._scrape_generic_video(url)
            elif 'imagebam' in host:
                return self._scrape_imagebam(url)
            elif 'wetransfer' in host or 'we.tl' in host:
                return self._scrape_wetransfer(url)
        except Exception as e:
            _log(f'Scrape failed for {url[:60]}: {e}', 'ERROR')
        return None

    def _scrape_bunkr(self, url: str) -> Optional[str]:
        """Scrape bunkr page for direct media URL."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            # Fallback: regex-based extraction
            # og:video meta tag first
            m = re.search(r'<meta\s+property="og:video"\s+content="([^"]+)"', html, re.I)
            if m:
                return html_mod.unescape(m.group(1))
            
            # Bunkr-specific: jsCDN + signUrl (no BS4 fallback)
            m = re.search(r'var\s+jsCDN\s*=\s*"([^"]+)"', html)
            if m:
                raw_url = html_mod.unescape(m.group(1)).replace('\\/', '/')
                sm = re.search(r'var\s+signUrl\s*=\s*"([^"]+)"', html)
                if sm:
                    signed = self._sign_bunkr_url(raw_url, sm.group(1))
                    if signed:
                        return signed
                return raw_url
            
            # og:image fallback
            m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html, re.I)
            if m:
                return html_mod.unescape(m.group(1))
            # Check for direct link in JSON-LD or script data
            m = re.search(r'"url"\s*:\s*"(https?://[^"]+)"', html)
            if m:
                return html_mod.unescape(m.group(1)).replace('\\/', '/')
            m = re.search(r'"(https?://[^"]+\.(mp4|jpg|png|gif|jpeg|webp|mov))"', html)
            if m:
                return html_mod.unescape(m.group(1))
            return None

        soup = BeautifulSoup(html, 'html.parser')

        # og:video meta tag (actual video URL, not thumbnail)
        meta_video = soup.find('meta', property='og:video')
        if meta_video and meta_video.get('content'):
            return meta_video['content']

        # Bunkr-specific: jsCDN variable with the actual media URL
        m = re.search(r'var\s+jsCDN\s*=\s*"([^"]+)"', html)
        if m:
            raw_url = html_mod.unescape(m.group(1)).replace('\\/', '/')
            # Also extract signUrl for CDN authentication
            sm = re.search(r'var\s+signUrl\s*=\s*"([^"]+)"', html)
            if sm:
                sign_url = sm.group(1)
                signed = self._sign_bunkr_url(raw_url, sign_url)
                if signed:
                    return signed
            # If no signUrl or signing failed, return raw URL
            return raw_url
        m = re.search(r'(?:srcUrl|fileUrl|mediaUrl)\s*=\s*"([^"]+)"', html)
        if m:
            return html_mod.unescape(m.group(1)).replace('\\\\/', '/')

        # Check for video/audio elements with src
        for tag in soup.find_all(['video', 'audio']):
            src = tag.get('src')
            if src:
                return src
            source = tag.find('source')
            if source and source.get('src'):
                return source['src']

        # Check for download links (contain actual media extension)
        for a in soup.find_all('a', href=True):
            href = a['href']
            if any(href.lower().endswith(ext) for ext in ['.mp4', '.jpg', '.png', '.gif', '.jpeg', '.webp', '.mov', '.webm']):
                if not href.startswith('http'):
                    href = urljoin(url, href)
                return href

        # Fallback: og:image (thumbnail, better than nothing)
        meta_img = soup.find('meta', property='og:image')
        if meta_img and meta_img.get('content'):
            return meta_img['content']

        # JSON data in scripts (common in bunkr pages)
        for script in soup.find_all('script'):
            if script.string:
                for pat in [r'"src"\s*:\s*"([^"]+)"', r'"url"\s*:\s*"(https?://[^"]+)"']:
                    m = re.search(pat, script.string)
                    if m:
                        val = html_mod.unescape(m.group(1)).replace('\\/', '/')
                        if not val.startswith('http'):
                            val = urljoin(url, val)
                        return val

        return None

    def _sign_bunkr_url(self, raw_url: str, sign_api_url: str) -> Optional[str]:
        """Sign a bunkr CDN URL via the signing API.
        
        Bunkr uses a token-based auth for CDN access. The signing API
        returns {token, ex} that need to be appended as query params.
        """
        from urllib.parse import urlencode, urlparse
        try:
            path = urlparse(raw_url).path
            sign_url = f'{sign_api_url.rstrip("/")}?path={path}'
            resp = self._session.get(sign_url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': sign_api_url,
                'Accept': 'application/json',
            })
            if resp.status_code != 200:
                return None
            data = resp.json()
            token = data.get('token', '')
            ex = data.get('ex', '')
            if not token:
                return None
            return f'{raw_url}?token={token}&ex={ex}'
        except Exception:
            return None

    def _scrape_imagebam(self, url: str) -> Optional[str]:
        """Scrape imagebam.com page for actual image URL."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            m = re.search(r'<meta\s+property="og:image"[^>]+content="([^"]+)"', html, re.I)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'<img[^>]+id="main-image"[^>]+src="([^"]+)"', html)
            if m:
                return html_mod.unescape(m.group(1))
            return None

        soup = BeautifulSoup(html, 'html.parser')

        # og:image meta
        meta = soup.find('meta', property='og:image')
        if meta and meta.get('content'):
            return meta['content']

        # Main image by ID
        img = soup.find('img', id='main-image')
        if img and img.get('src'):
            return img['src']

        # Image in download link
        a = soup.find('a', class_='btn-download')
        if a and a.get('href'):
            href = a['href']
            if not href.startswith('http'):
                href = f'https://www.imagebam.com{href}'
            return href

        return None

    def _scrape_cyberdrop(self, url: str) -> Optional[str]:
        """Scrape cyberdrop/cyberfile page for direct media URL."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            m = re.search(r'<meta\s+property="og:video"[^>]+content="([^"]+)"', html, re.I)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'<meta\s+property="og:image"[^>]+content="([^"]+)"', html, re.I)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'id="downloadUrl"[^>]+value="([^"]+)"', html)
            if m:
                return html_mod.unescape(m.group(1))
            return None

        soup = BeautifulSoup(html, 'html.parser')

        for prop in ('og:video', 'og:image'):
            meta = soup.find('meta', property=prop)
            if meta and meta.get('content'):
                return meta['content']

        # Check for download URL input
        input_el = soup.find('input', {'id': 'downloadUrl'})
        if input_el and input_el.get('value'):
            return input_el['value']

        # Check for video/audio
        for tag in soup.find_all(['video', 'audio']):
            src = tag.get('src')
            if src:
                return src
            source = tag.find('source')
            if source and source.get('src'):
                return source['src']

        return None

    def _scrape_erome(self, url: str) -> Optional[str]:
        """Scrape erome.com page for video URL."""
        resp = self._session.get(url, timeout=20, headers={'Referer': 'https://www.erome.com/'})
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            m = re.search(r'data-url="([^"]+\.mp4[^"]*)"', html)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'src="([^"]+\.mp4[^"]*)"', html)
            if m:
                return html_mod.unescape(m.group(1))
            return None

        soup = BeautifulSoup(html, 'html.parser')

        # Erome uses data-url on video elements
        for div in soup.find_all('div', attrs={'data-url': True}):
            url_val = div.get('data-url')
            if '.mp4' in url_val or '.webm' in url_val:
                return url_val

        # Or video source elements
        for video in soup.find_all('video'):
            src = video.get('src')
            if src:
                return src
            source = video.find('source')
            if source and source.get('src'):
                return source['src']

        return None

    def _scrape_sendvid(self, url: str) -> Optional[str]:
        """Scrape sendvid.com page for video URL."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            m = re.search(r'<source[^>]+src="([^"]+)"', html)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'"videoUrl"\s*:\s*"([^"]+)"', html)
            if m:
                return html_mod.unescape(m.group(1)).replace('\\\\/', '/')
            return None

        soup = BeautifulSoup(html, 'html.parser')
        source = soup.find('source')
        if source and source.get('src'):
            return source['src']

        # Check script for videoUrl
        for script in soup.find_all('script'):
            if script.string and 'videoUrl' in script.string:
                m = re.search(r'"videoUrl"\s*:\s*"([^"]+)"', script.string)
                if m:
                    return html_mod.unescape(m.group(1)).replace('\\\\/', '/')

        # Check for video element with src
        video = soup.find('video')
        if video and video.get('src'):
            return video['src']

        return None

    def _scrape_coomer(self, url: str) -> Optional[str]:
        """Scrape coomer.party page for media URL."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text

        if not BS4_AVAILABLE:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*post__attachment-link[^"]*"', html)
            if m:
                return html_mod.unescape(m.group(1))
            m = re.search(r'<img[^>]+src="([^"]+)"[^>]*class="[^"]*post__attachment[^"]*"', html)
            if m:
                return html_mod.unescape(m.group(1))
            return None

        soup = BeautifulSoup(html, 'html.parser')
        # Attachment link
        a = soup.find('a', class_='post__attachment-link')
        if a and a.get('href'):
            href = a['href']
            if not href.startswith('http'):
                href = f'https://coomer.party{href}'
            return href
        # Direct image
        img = soup.find('img', class_='post__attachment')
        if img and img.get('src'):
            return img['src']
        return None

    def _scrape_mediafire(self, url: str) -> Optional[str]:
        """Scrape mediafire.com for direct download link."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        m = re.search(r'aria-label="Download file"[^>]+href="([^"]+)"', html)
        if m:
            return html_mod.unescape(m.group(1))
        # Alternative: download button
        m = re.search(r'id="downloadButton"[^>]+href="([^"]+)"', html)
        if m:
            return html_mod.unescape(m.group(1))
        return None

    def _scrape_generic_video(self, url: str) -> Optional[str]:
        """Scrape a generic video page looking for video sources."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Try common video source patterns
        patterns = [
            r'<source[^>]+src="([^"]+)"',
            r'<video[^>]+src="([^"]+)"',
            r'"src"\s*:\s*"([^"]+\.(mp4|webm|m3u8)[^"]*)"',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.I)
            if m:
                return html_mod.unescape(m.group(1)).replace('\\\\/', '/')
        return None

    def _scrape_wetransfer(self, url: str) -> Optional[str]:
        """WeTransfer URLs typically expire — log and skip."""
        _log(f'WeTransfer URL (likely expired): {url[:60]}', 'WARN')
        return None

    # ── Browser-based resolution (Playwright) ─────────────────────

    async def resolve_with_browser(self, url: str, timeout: int = 30) -> Optional[str]:
        """Resolve a URL that needs a browser (turbo.cr, bunkr fallback, etc.)."""
        await self._ensure_browser()
        if self._context is None:
            _log(f'Browser not available, cannot resolve: {url[:60]}', 'WARN')
            return None
        page = await self._context.new_page()
        resolved_url = None

        try:
            host = self._get_netloc(url)

            # selti-delivery → transformed to cuckcapital first
            direct = self.resolve_direct(url)
            if direct:
                url = direct  # use the transformed URL

            if 'turbo.cr' in host:
                resolved_url = await self._resolve_turbo(page, url, timeout)
            elif 'bunkr' in host:
                resolved_url = await self._resolve_bunkr_browser(page, url, timeout)
            elif 'cyberdrop' in host or 'cyberfile' in host:
                resolved_url = await self._resolve_cyberdrop_browser(page, url, timeout)
            elif 'erome' in host:
                resolved_url = await self._resolve_erome_browser(page, url, timeout)
            elif 'coomer' in host:
                resolved_url = await self._resolve_coomer_browser(page, url, timeout)
        except Exception as e:
            _log(f'Browser resolution failed for {url[:60]}: {e}', 'ERROR')
        finally:
            await page.close()

        self._resolve_count += 1
        if self._resolve_count >= 10:
            # Recycle browser context to avoid memory leaks, keep session alive
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
                self._context = None
                self._playwright = None
                self._playwright_ctx = None
            self._resolve_count = 0

        return resolved_url

    async def _resolve_jpg_variant(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve jpg*.su/church → actual image URL (cuckcapital or lazy-loaded)."""
        actual_url = None

        def handle_response(response):
            nonlocal actual_url
            ct = response.headers.get('content-type', '')
            if actual_url is None and 'image' in ct:
                url_lower = response.url.lower()
                # Skip site chrome (logo, favicon, icons, loading SVG)
                if any(skip in url_lower for skip in ['logo', 'favicon', 'icon', 'loading.svg', 'avatar']):
                    return
                # Only accept cuckcapital or similar CDN URLs
                if 'cuckcapital' in url_lower or 'images' in url_lower:
                    actual_url = response.url

        page.on('response', handle_response)

        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            pass

        for _ in range(12):
            if actual_url:
                return actual_url
            try:
                # Check for cuckcapital images via currentSrc
                imgs = await page.evaluate('''() => {
                    return Array.from(document.querySelectorAll('img'))
                        .filter(i => i.currentSrc && i.currentSrc.startsWith('http') && !i.currentSrc.includes('loading.svg') && !i.currentSrc.includes('logo'))
                        .map(i => i.currentSrc);
                }''')
                if imgs and imgs[0]:
                    return imgs[0]
            except Exception:
                pass

            # Scroll to bottom to trigger lazy loads
            try:
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            except Exception:
                pass
            await asyncio.sleep(1.5)

        # Scroll image into view and trigger lazy load via vanilla-lazyload
        try:
            await page.evaluate('''() => {
                const img = document.querySelector('img.lazy');
                if (img) {
                    img.scrollIntoView({behavior: 'instant', block: 'center'});
                    // Trigger vanilla-lazyload manually if available
                    if (window.LazyLoad && window.LazyLoad.load) {
                        window.LazyLoad.load(img, {use_P: true});
                    }
                }
            }''')
        except Exception:
            pass
        await asyncio.sleep(2)

        if actual_url:
            return actual_url

        # Check for loaded image (currentSrc is the real URL after lazy-load)
        for _ in range(8):
            if actual_url:
                return actual_url
            try:
                src = await page.evaluate('''() => {
                    const img = document.querySelector('img.lazy');
                    if (img) {
                        if (img.currentSrc && img.currentSrc.startsWith('http') && !img.currentSrc.includes('loading.svg') && !img.currentSrc.includes('logo')) {
                            return img.currentSrc;
                        }
                    }
                    // Fallback: check all images
                    for (const i of document.querySelectorAll('img')) {
                        if (i.currentSrc && i.currentSrc.startsWith('http') && !i.currentSrc.includes('loading.svg') && !i.currentSrc.includes('logo') && !i.currentSrc.includes('favicon')) {
                            return i.currentSrc;
                        }
                    }
                    // If still loading, trigger click on the image
                    if (img && !img.complete) {
                        img.dispatchEvent(new Event('scroll'));
                    }
                    return '';
                }''')
                if src:
                    return src
            except Exception:
                pass
            await asyncio.sleep(1)

        return None

    async def _resolve_turbo(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve turbo.cr embed → actual video URL."""
        actual_url = None

        def handle_response(response):
            nonlocal actual_url
            ct = response.headers.get('content-type', '')
            if actual_url is None and ('video' in ct or 'application/octet-stream' in ct) and 'turbo.cr' in response.url:
                actual_url = response.url

        page.on('response', handle_response)
        await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')

        for _ in range(15):
            if actual_url:
                return actual_url
            # Check for <video><source> or video[src]
            v = await page.query_selector('video source')
            if v:
                src = await v.get_attribute('src')
                if src:
                    return html_mod.unescape(src)
            # Check video element's src attribute directly
            src = await page.evaluate('''() => {
                const v = document.getElementById('main-video');
                if (v && v.src && v.src.startsWith('http')) return v.src;
                const v2 = document.querySelector('video');
                if (v2 && v2.src && v2.src.startsWith('http')) return v2.src;
                return '';
            }''')
            if src:
                return src
            await asyncio.sleep(1)
        return None

    async def _resolve_bunkr_browser(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve bunkr media page via browser (JS-rendered video/image)."""
        actual_url = None

        def handle_response(response):
            nonlocal actual_url
            ct = response.headers.get('content-type', '')
            if actual_url is None and ('video' in ct or 'image' in ct):
                actual_url = response.url

        page.on('response', handle_response)
        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            pass

        for _ in range(15):
            if actual_url:
                return actual_url
            # Check for video element
            try:
                video = await page.query_selector('video source')
                if video:
                    src = await video.get_attribute('src')
                    if src:
                        return src
                # Check for source attribute on video
                sources = await page.evaluate('''() => {
                    const v = document.querySelector('video');
                    return v ? (v.src || (v.querySelector('source')?.src || '')) : '';
                }''')
                if sources:
                    return sources
            except Exception:
                pass
            await asyncio.sleep(1)
        return None

    async def _resolve_cyberdrop_browser(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve cyberdrop media page via browser."""
        actual_url = None

        def handle_response(response):
            nonlocal actual_url
            ct = response.headers.get('content-type', '')
            if actual_url is None and ('video' in ct or 'image' in ct):
                actual_url = response.url

        page.on('response', handle_response)
        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            pass

        for _ in range(15):
            if actual_url:
                return actual_url
            try:
                video = await page.query_selector('video source')
                if video:
                    src = await video.get_attribute('src')
                    if src:
                        return src
                imgs = await page.evaluate('''() => {
                    const img = document.querySelector('img[src*=\"cyberdrop\"]');
                    return img ? img.src : '';
                }''')
                if imgs:
                    return imgs
            except Exception:
                pass
            await asyncio.sleep(1)
        return None

    async def _resolve_erome_browser(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve erome album page via browser to find video URLs."""
        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            pass

        await asyncio.sleep(3)  # Erome lazy-loads content

        for _ in range(15):
            try:
                # Erome stores video URLs in data-url or src attributes
                urls = await page.evaluate('''() => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    const sources = videos.map(v => v.src || (v.querySelector('source')?.src || '')).filter(s => s);
                    if (sources.length) return sources[0];
                    const divs = Array.from(document.querySelectorAll('[data-url]'));
                    const durls = divs.map(d => d.getAttribute('data-url')).filter(u => u && u.includes('.mp4'));
                    if (durls.length) return durls[0];
                    return '';
                }''')
                if urls:
                    return urls
            except Exception:
                pass
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await asyncio.sleep(2)
        return None

    async def _resolve_coomer_browser(self, page, url: str, timeout: int) -> Optional[str]:
        """Resolve coomer.party media page via browser."""
        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            pass

        for _ in range(10):
            try:
                # Look for attachment links or images
                urls = await page.evaluate('''() => {
                    const a = document.querySelector('a.post__attachment-link');
                    if (a && a.href) return a.href;
                    const img = document.querySelector('img.post__attachment');
                    if (img && img.src) return img.src;
                    return '';
                }''')
                if urls:
                    return urls
            except Exception:
                pass
            await asyncio.sleep(1)
        return None

    # ── Gallery / album expansion ─────────────────────────────

    def _try_expand_gallery(self, url: str) -> list[str]:
        """If url is a media gallery/album, expand into individual media URLs.
        Returns list of expanded URLs, or empty list if not a gallery.
        """
        host = self._get_netloc(url)
        result: list[str] = []

        # ── Bunkr gallery (/a/...) with pagination ──
        if 'bunkr' in host and '/a/' in url:
            try:
                seen: set[str] = set()
                base_url = url.split('?')[0]
                visited_pages: set[int] = set()
                to_visit: set[int] = {1}

                while to_visit:
                    p = to_visit.pop()
                    if p in visited_pages:
                        continue
                    visited_pages.add(p)
                    page_url = base_url if p == 1 else f'{base_url}?page={p}'
                    resp = self._session.get(page_url, timeout=20)
                    if resp.status_code != 200:
                        continue
                    html = resp.text

                    # Collect /f/ links
                    if BS4_AVAILABLE:
                        soup = BeautifulSoup(html, 'html.parser')
                        for a in soup.find_all('a', href=True):
                            href = a['href']
                            if '/f/' in href:
                                full = urljoin(url, href)
                                if full not in seen:
                                    seen.add(full)
                                    result.append(full)
                    else:
                        for m in re.finditer(r'href=\"([^\"]*)/f/([^\"]+)\"',
                                             html):
                            full = urljoin(url, m.group(0).split('"')[1])
                            if full not in seen:
                                seen.add(full)
                                result.append(full)

                    # Discover page numbers to visit
                    for m in re.finditer(r'href=\"([^\"]*\\?page=(\d+))\"',
                                         html):
                        np = int(m.group(2))
                        if np not in visited_pages:
                            to_visit.add(np)

                if result:
                    _log(f'Expanded bunkr gallery: {url[:60]} → '
                         f'{len(result)} file(s) across '
                         f'{len(visited_pages)} page(s)')
            except Exception as e:
                _log(f'Failed to expand bunkr gallery {url[:60]}: {e}',
                     'WARN')

        # ── Gofile folder (/d/...) ──
        elif 'gofile' in host and '/d/' in url:
            try:
                path = urlparse(url).path.strip('/')
                content_id = path.split('/')[-1] if path else ''
                if not content_id:
                    return []
                token = None
                try:
                    acct = self._session.post(
                        'https://api.gofile.io/accounts', json={}, timeout=10
                    )
                    if acct.status_code == 200:
                        d = acct.json()
                        if d.get('status') == 'ok':
                            token = d['data']['token']
                except Exception:
                    pass
                api_url = f'https://api.gofile.io/contents/{content_id}'
                params: dict[str, str] = {}
                if token:
                    params['token'] = token
                    params['wt'] = '4fd6sg89d7s6'
                resp = self._session.get(api_url, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('status') == 'ok':
                        contents = data.get('data', {}).get('contents', {})
                        for cid, cdata in contents.items():
                            link = cdata.get('link', '')
                            if link and cdata.get('type') in ('file',):
                                result.append(link)
                        if result:
                            _log(f'Expanded gofile folder: {url[:60]} → {len(result)} file(s)')
            except Exception as e:
                _log(f'Failed to expand gofile {url[:60]}: {e}', 'WARN')

        # ── RedGifs user page (/users/{username}) ──
        elif 'redgifs' in host and '/users/' in url:
            username = url.rstrip('/').split('/')[-1]
            if not username or username in ('redgifs.com', 'www.redgifs.com'):
                return result
            try:
                # Get anonymous token
                tok = getattr(self, '_redgifs_token', None)
                if not tok:
                    tr = self._session.get('https://api.redgifs.com/v2/auth/temporary', timeout=15)
                    if tr.status_code == 200:
                        self._redgifs_token = tr.json().get('token', '')
                        tok = self._redgifs_token
                if not tok:
                    return result
                headers = {'Authorization': f'Bearer {tok}'}
                page = 1
                seen_ids = set()
                while True:
                    api_url = f'https://api.redgifs.com/v2/users/{username}/search?order=new&count=40&type=g&page={page}'
                    resp = self._session.get(api_url, headers=headers, timeout=15)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    gifs = data.get('gifs', [])
                    if not gifs:
                        break
                    for g in gifs:
                        gid = g.get('id', '')
                        if gid and gid not in seen_ids:
                            seen_ids.add(gid)
                            result.append(f'https://www.redgifs.com/watch/{gid}')
                    total_pages = data.get('pages', 1)
                    if page >= total_pages:
                        break
                    page += 1
                if result:
                    _log(f'Expanded RedGifs user page: {username} → {len(result)} videos')
            except Exception as e:
                _log(f'Failed to expand RedGifs user {username}: {e}', 'WARN')

        return result

    # ── Batch resolution ───────────────────────────────────────────

    async def resolve_batch(self, urls: list[str],
                            cancel_event=None) -> dict[str, Optional[str]]:
        """Resolve a batch of URLs using the appropriate strategy for each."""
        result: dict[str, Optional[str]] = {}
        browser_urls: list[str] = []
        oembed_urls: list[str] = []

        strategy_counts: dict[str, int] = {}

        for url in urls:
            strategy = self.classify_host(url)
            strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

            if strategy == 'direct':
                result[url] = url

            elif strategy == 'referer':
                info = self.resolve_referer(url)
                result[url] = info['url']

            elif strategy == 'api':
                resolved = self.resolve_api(url)
                result[url] = resolved or url

            elif strategy == 'scrape':
                resolved = self.resolve_scrape(url)
                if resolved:
                    result[url] = resolved
                else:
                    # Scrape failed — try browser fallback for sites that
                    # are known to need JS rendering
                    host = self._get_netloc(url)
                    if any(p in host for p in ['bunkr', 'cyberdrop', 'cyberfile', 'erome', 'coomer']):
                        browser_urls.append(url)
                    else:
                        result[url] = None

            elif strategy == 'oembed':
                oembed_urls.append(url)

            elif strategy == 'browser':
                direct = self.resolve_direct(url)
                if direct:
                    result[url] = direct
                else:
                    browser_urls.append(url)

            elif strategy == 'mega':
                # Return URL as-is — download_url handles mega.nz via mega.py
                result[url] = url

            else:
                result[url] = url

        _log(f'URL breakdown: {strategy_counts}')

        # Resolve oEmbed URLs in parallel via thread pool (REST API calls)
        if oembed_urls:
            import concurrent.futures
            import time as _time
            _log(f'Resolving {len(oembed_urls)} URLs via Chevereto oEmbed API...')
            oembed_results: dict[str, Optional[str]] = {}
            def _resolve_oe(url):
                return url, self.resolve_oembed(url)
            resolved_count = 0
            total_oe = len(oembed_urls)
            _start_oe = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(_resolve_oe, url): url for url in oembed_urls}
                for f in concurrent.futures.as_completed(futures):
                    # Check cancel every 50 resolved URLs
                    if cancel_event and cancel_event.is_set():
                        _log('oEmbed resolution cancelled by user', 'WARN')
                        # Mark remaining as None
                        for remaining_url in futures.values():
                            if remaining_url not in result:
                                result[remaining_url] = None
                        break
                    url, rurl = f.result()
                    result[url] = rurl
                    resolved_count += 1
                    if resolved_count % 50 == 0 or resolved_count == total_oe:
                        elapsed = time.time() - _start_oe
                        _log(f'oEmbed: {resolved_count}/{total_oe} resolved ({elapsed:.0f}s elapsed)')
                        # Check if we're falling behind — log total OK/FAIL
                        ok_sofar = sum(1 for u in oembed_urls[:resolved_count] if result.get(u))
                        fail_sofar = resolved_count - ok_sofar
                        _log(f'  → {ok_sofar} OK, {fail_sofar} FAIL so far')
            ok = sum(1 for u in oembed_urls if result.get(u))
            fail = sum(1 for u in oembed_urls if not result.get(u))
            _log(f'oEmbed resolution complete: {ok} OK, {fail} FAIL in {time.time()-_start_oe:.0f}s')

        if browser_urls:
            _log(f'Resolving {len(browser_urls)} URLs via browser (Playwright)...')
            # Limit concurrent browser pages to avoid triggering anti-bot on Chevereto-based sites
            sem = asyncio.Semaphore(2)

            async def _browser_resolve(url):
                async with sem:
                    # Small delay per URL to avoid rate-limiting
                    await asyncio.sleep(0.5)
                    return await self.resolve_with_browser(url)

            tasks = [_browser_resolve(url) for url in browser_urls]
            resolved = await asyncio.gather(*tasks, return_exceptions=True)
            for url, rurl in zip(browser_urls, resolved):
                if isinstance(rurl, Exception):
                    _log(f'Browser resolve failed for {url[:60]}: {rurl}', 'WARN')
                    result[url] = None
                else:
                    result[url] = rurl

        return result


# ── Download Stats ──────────────────────────────────────────────────

class DownloadStats:
    def __init__(self):
        self.total_posts = 0
        self.total_urls = 0
        self.completed = 0
        self.failed = 0
        self.skipped = 0
        self.total_bytes = 0
        self.errors: list[str] = []
        self.failed_urls: list[tuple[str, str, str, int, int, str]] = []  # (orig_url, model, mode, page, post, reason)
        self._lock = threading.Lock()

    def _inc_completed(self):
        with self._lock:
            self.completed += 1

    def _inc_failed(self):
        with self._lock:
            self.failed += 1

    def _inc_skipped(self):
        with self._lock:
            self.skipped += 1

    def _add_bytes(self, n: int):
        with self._lock:
            self.total_bytes += n


# ── File extension detection ────────────────────────────────────────

# Known magic byte signatures for common media formats
_MAGIC_SIGS: list[tuple[bytes, str]] = [
    (b'\xff\xd8\xff', '.jpg'),
    (b'\x89PNG\r\n\x1a\n', '.png'),
    (b'GIF87a', '.gif'),
    (b'GIF89a', '.gif'),
    (b'RIFF', '.webp'),          # WEBP starts with RIFF, check bytes 8-11
    (b'\x00\x00\x00\x18ftypmp4', '.mp4'),
    (b'\x00\x00\x00\x20ftypmp4', '.mp4'),
    (b'\x00\x00\x00\x1cftyp', '.mp4'),
    (b'\x00\x00\x00\x14ftyp', '.mov'),
    (b'\x1a\x45\xdf\xa3', '.webm'),
    (b'\x00\x00\x00\x0cftyp', '.mp4'),
    (b'ftypmp4', '.mp4'),
]


def _detect_ext_from_magic(data: bytes) -> str | None:
    """Detect file extension from magic bytes (reads first 16 bytes)."""
    if len(data) < 4:
        return None
    # WEBP-specific: RIFF....WEBP
    if data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
        return '.webp'
    for sig, ext in _MAGIC_SIGS:
        if data.startswith(sig):
            return ext
    return None


def _fix_extension(filepath: str) -> str:
    """Check a file's magic bytes and rename with correct extension if needed.
    Returns the (possibly new) filepath.
    """
    if not os.path.isfile(filepath):
        return filepath
    try:
        with open(filepath, 'rb') as f:
            header = f.read(16)
        detected = _detect_ext_from_magic(header)
        if not detected:
            return filepath  # unknown type, keep as-is
        base, cur_ext = os.path.splitext(filepath)
        if cur_ext.lower() == detected:
            return filepath  # already correct
        new_path = base + detected
        # Avoid overwriting an existing file
        if os.path.exists(new_path):
            n = 1
            while os.path.exists(f'{base}_{n}{detected}'):
                n += 1
            new_path = f'{base}_{n}{detected}'
        os.rename(filepath, new_path)
        return new_path
    except Exception:
        return filepath


# ── Download Manager ────────────────────────────────────────────────

class DownloadManager:
    """Orchestrates downloading media files with proper folder structure."""

    # Map hosts to their required Referer
    REFERER_MAP = {
        'pixhost.to': 'https://pixhost.to/',
        'www.imagepond.net': 'https://www.imagepond.net/',
        'media.imagepond.net': 'https://www.imagepond.net/',
        'pixeldrain.com': 'https://pixeldrain.com/',
        'www.imagebam.com': 'https://www.imagebam.com/',
        'thumbs2.imagebam.com': 'https://www.imagebam.com/',
        'imgbox.com': 'https://imgbox.com/',
    }

    def __init__(self, output_base: str = 'output', max_speed_bps: int = 1_000_000,
                 max_concurrent: int = 3, pixeldrain_api_key: str = "",
                 pixeldrain_cookies_json: str = ""):
        self.output_base = output_base
        self.max_speed_bps = max_speed_bps
        self.max_concurrent = max_concurrent
        self.resolver = URLResolver(pixeldrain_api_key=pixeldrain_api_key,
                                    pixeldrain_cookies_json=pixeldrain_cookies_json)

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        })
        if pixeldrain_api_key:
            self.session.headers.update({'X-API-Key': pixeldrain_api_key})
        if pixeldrain_cookies_json:
            _set_pixeldrain_cookies(self.session, pixeldrain_cookies_json)
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    async def close(self):
        await self.resolver.close()
        self.session.close()

    def _get_filename(self, url: str, resp=None) -> str:
        """Extract filename from a download URL or HTTP response headers."""
        # Try Content-Disposition header first
        if resp and 'Content-Disposition' in resp.headers:
            cd = resp.headers['Content-Disposition']
            m = re.search(r'filename[^;=\n]*=[\s"\']*([^"\';,\n]*)', cd, re.IGNORECASE)
            if m:
                fname = m.group(1).strip()
                if fname and '.' in fname:
                    return fname
        # Try URL path
        clean = url.split('?')[0]
        fname = clean.rstrip('/').split('/')[-1]
        if fname and '.' in fname:
            return fname
        # Try fn query parameter
        match = re.search(r'[?&]fn=([^&]+)', url)
        if match:
            return requests.utils.unquote(match.group(1))
        # Fallback: use Content-Type extension if available
        if resp and 'Content-Type' in resp.headers:
            ct = resp.headers['Content-Type'].split(';')[0].strip()
            ct_map = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                      'image/gif': '.gif', 'video/mp4': '.mp4', 'video/quicktime': '.mov',
                      'video/x-msvideo': '.avi'}
            # Do NOT map application/octet-stream to .bin — leaves room for
            # magic-byte detection (_fix_extension) to set the correct real extension
            ext = ct_map.get(ct, '')
            if ext:
                return f'download_{hash(url) & 0xFFFF}{ext}'
        return f'download_{hash(url) & 0xFFFF}'

    def _get_referer(self, url: str) -> Optional[str]:
        """Get the referer header for a host if needed."""
        host = URLResolver._get_netloc(url)
        # Direct check
        if host in self.REFERER_MAP:
            return self.REFERER_MAP[host]
        # With www prefix
        if host.startswith('www.') and host[4:] in self.REFERER_MAP:
            return self.REFERER_MAP[host[4:]]
        if f'www.{host}' in self.REFERER_MAP:
            return self.REFERER_MAP[f'www.{host}']
        # Bunkr CDN: any *.cdn.cr or static.scdn.st needs Referer
        if '.cdn.cr' in host or host.endswith('.scdn.st'):
            return 'https://bunkr.cr/'
        # Common pattern: same host
        if any(x in host for x in ['pixhost', 'pixl', 'imagepond', 'imagebam', 'imgbox']):
            return f'https://{host}/'
        return None


    def _handle_pixeldrain_list_zip(self, list_id, model_name, output_dir,
                                     progress_callback=None, log_callback=None,
                                     cancel_event=None, start_count=0, total_files=0):
        """Stream a pixeldrain list ZIP and extract all files to output_dir.

        Parses ZIP local file headers incrementally from a streaming HTTP response,
        handles data descriptors (compressed_size=0), and decompresses deflated
        entries on the fly.
        """
        url = f'https://pixeldrain.com/api/list/{list_id}/zip'
        _log(f'PixelDrain list ZIP: streaming list {list_id}')
        if progress_callback:
            progress_callback('status', message=f'Streaming PixelDrain list ZIP for {list_id}...')

        try:
            resp = self.session.get(url, stream=True, timeout=60)
            if resp.status_code != 200:
                _log(f'PixelDrain list ZIP returned HTTP {resp.status_code}', 'ERROR')
                return 0

            it = resp.iter_content(chunk_size=65536)
            buffer = bytearray()
            files_processed = 0

            while True:
                # Need at least 30 bytes for the local file header fixed part
                while len(buffer) < 30:
                    try:
                        chunk = next(it)
                        buffer.extend(chunk)
                    except StopIteration:
                        _log(f'PixelDrain list ZIP: done ({files_processed} files extracted)')
                        return files_processed

                sig = int.from_bytes(buffer[0:4], 'little')

                if sig == 0x04034b50:
                    # ── Local file header ──────────────────────────
                    bit_flag = int.from_bytes(buffer[6:8], 'little')
                    compression = int.from_bytes(buffer[8:10], 'little')
                    compressed_size = int.from_bytes(buffer[18:22], 'little')
                    uncompressed_size = int.from_bytes(buffer[22:26], 'little')
                    filename_len = int.from_bytes(buffer[26:28], 'little')
                    extra_len = int.from_bytes(buffer[28:30], 'little')
                    has_data_desc = bool(bit_flag & 0x08)

                    header_total = 30 + filename_len + extra_len
                    while len(buffer) < header_total:
                        try:
                            chunk = next(it)
                            buffer.extend(chunk)
                        except StopIteration:
                            return files_processed

                    # Decode filename
                    raw_name = bytes(buffer[30:30 + filename_len])
                    filename = raw_name.decode('utf-8', errors='replace')
                    # Sanitize path
                    if '..' in filename or filename.startswith('/') or filename.startswith('\\'):
                        filename = os.path.basename(filename)
                    if not filename:
                        filename = f'file_{files_processed}'

                    target_path = os.path.join(output_dir, filename)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)

                    # Consume header from buffer
                    buffer = buffer[header_total:]

                    if cancel_event and cancel_event.is_set():
                        _log(f'PixelDrain list ZIP cancelled during: {filename}', 'WARN')
                        return files_processed

                    # ── Read file data ─────────────────────────────
                    raw_data = b''

                    if compressed_size > 0:
                        # Known compressed size — simplest case
                        raw_data = bytes(buffer)
                        buffer = bytearray()
                        while len(raw_data) < compressed_size:
                            try:
                                chunk = next(it)
                            except StopIteration:
                                break
                            raw_data += chunk
                        if len(raw_data) > compressed_size:
                            buffer = bytearray(raw_data[compressed_size:])
                            raw_data = raw_data[:compressed_size]

                    elif has_data_desc and compression == 8:
                        # Deflated with data descriptor — decompress until stream ends
                        raw_data = bytes(buffer)
                        buffer = bytearray()
                        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                        decompressed = bytearray()
                        leftover = b''
                        try:
                            while True:
                                try:
                                    chunk = decompressor.decompress(raw_data)
                                    decompressed.extend(chunk)
                                    if decompressor.eof:
                                        leftover = decompressor.unconsumed_tail
                                        break
                                    raw_data = next(it)
                                except StopIteration:
                                    break
                        except zlib.error:
                            pass

                        # Skip data descriptor (optional 4-byte sig + 12 bytes data)
                        dd_buf = leftover
                        while len(dd_buf) < 12:
                            try:
                                chunk = next(it)
                            except StopIteration:
                                break
                            dd_buf += chunk
                        if len(dd_buf) >= 4 and dd_buf[:4] == b'PK\x07\x08':
                            dd_buf = dd_buf[16:]  # sig(4) + crc32(4) + csize(4) + usize(4)
                        elif len(dd_buf) >= 12:
                            dd_buf = dd_buf[12:]  # no sig, just 12 bytes
                        buffer = bytearray(dd_buf)

                        raw_data = bytes(decompressed)
                        # Already decompressed, so compression is now 0 (stored)
                        compression = 0

                    else:
                        # Stored with data descriptor or unknown — find next PK sig
                        raw_data = bytes(buffer)
                        buffer = bytearray()
                        search = raw_data
                        found = False
                        while True:
                            for marker in (b'PK\x03\x04', b'PK\x01\x02', b'PK\x05\x06'):
                                idx = search.find(marker)
                                if idx >= 0:
                                    raw_data = search[:idx]
                                    buffer = bytearray(search[idx:])
                                    found = True
                                    break
                            if found:
                                break
                            try:
                                chunk = next(it)
                                search += chunk
                            except StopIteration:
                                raw_data = search
                                break
                        # Truncate to uncompressed_size if available
                        if uncompressed_size > 0 and len(raw_data) > uncompressed_size:
                            raw_data = raw_data[:uncompressed_size]

                    # ── Decompress if needed ───────────────────────
                    if compression == 8:
                        try:
                            raw_data = zlib.decompress(raw_data, -zlib.MAX_WBITS)
                        except zlib.error as e:
                            _log(f'PixelDrain ZIP: decompress error for {filename}: {e}', 'WARN')
                            files_processed += 1
                            continue

                    if not raw_data:
                        _log(f'SKIP (empty): {filename}')
                        files_processed += 1
                        continue

                    # Write to disk
                    with open(target_path, 'wb') as f:
                        f.write(raw_data)

                    files_processed += 1
                    _log(f'OK {len(raw_data) / 1024 / 1024:.1f}MB: {filename}')
                    if progress_callback:
                        progress_callback('file',
                                          filename=filename,
                                          size=len(raw_data),
                                          host='pixeldrain.com',
                                          ok=True,
                                          overall_total=total_files,
                                          overall_completed=start_count + files_processed,
                                          overall_eta=0)

                elif sig == 0x02014b50:
                    # Central directory header — end of file entries
                    _log(f'PixelDrain list ZIP: complete ({files_processed} files)')
                    return files_processed
                elif sig == 0x06054b50:
                    # End of central directory record
                    _log(f'PixelDrain list ZIP: complete ({files_processed} files)')
                    return files_processed
                else:
                    # Unexpected signature — skip one byte and continue
                    buffer = buffer[1:]

        except Exception as e:
            _log(f'PixelDrain list ZIP failed for {list_id}: {e}', 'ERROR')
            return 0

        return files_processed


    async def download_url(
        self,
        url: str,
        resolved_url: str,
        filepath: str,
        stats: DownloadStats,
        eta: int = 0,
        cancel_event=None,
        page: int = 0,
        post: int = 0,
        model_name: str = "",
        mode: str = "",
        file_progress_callback=None,
    ):
        """Download a single resolved URL to a file.

        The actual HTTP download runs in a thread to avoid blocking the event loop,
        allowing truly parallel downloads across hosts.
        """
        if cancel_event and cancel_event.is_set():
            return (False, 0)

        # ── Pre-check: skip if file already exists from URL basename ──
        _url_to_check = resolved_url or url
        if _url_to_check:
            _pre_name = os.path.basename(urlparse(_url_to_check).path)
            if _pre_name:
                _pre_path = os.path.join(filepath, _pre_name)
                if os.path.exists(_pre_path) and os.path.getsize(_pre_path) > 0:
                    stats.skipped += 1
                    _log(f'SKIP (exists): {_pre_name}')
                    return (True, 0)
        # ── Handle mega.nz via mega.py (fast enough to stay async) ──
        if MEGA_AVAILABLE and ('mega.nz' in url or 'mega.co.nz' in url):
            try:
                mega_api = Mega()
                mega_api.login_anonymous()
                fn = mega_api.download_url(url, dest_path=filepath)
                stats.completed += 1
                fsize = os.path.getsize(fn) if fn and os.path.isfile(fn) else 0
                stats.total_bytes += fsize
                fname = os.path.basename(fn) if fn else url.split('/')[-1][:40]
                _log(f'OK (mega) {fsize/1024/1024:.1f}MB: {fname}')
                return (True, fsize)
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f'Mega download failed: {url[:50]} — {e}')
                _log(f'FAIL (mega): {url[:70]} — {e}', 'ERROR')
                return (False, 0)

        # ── Blocking HTTP work runs in a thread to free the event loop ──
        def _do_download() -> tuple:
            """Synchronous download body. Returns a result tuple."""
            headers = {}
            referer = self._get_referer(resolved_url or url)
            if referer:
                headers['Referer'] = referer

            # Gofile CDN links need the guest token as Cookie
            _dl_url = resolved_url or url
            if _dl_url and 'gofile.io' in _dl_url.lower():
                global _gofile_token
                if _gofile_token:
                    headers['Cookie'] = f'accountToken={_gofile_token}'
                    headers['AccountToken'] = _gofile_token

            # Retry on flaky connections (bunkr etc.)
            max_attempts = 3
            resp = None
            for attempt in range(max_attempts):
                try:
                    resp = self.session.get(
                        resolved_url or url, stream=True, timeout=120, headers=headers
                    )
                    break
                except (BrokenPipeError, ConnectionResetError, ConnectionError) as e:
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s

            if resp is None:
                return ('connection_error', 'Max retries exceeded', url[:60])

            if resp.status_code != 200:
                return ('http_error', resp.status_code, url[:60])

            filename = self._get_filename(resp.url, resp)
            full_path = os.path.join(filepath, filename)

            if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                return ('skip', filename)

            downloaded = 0
            content_length = int(resp.headers.get('Content-Length', 0))
            start_time = time.time()
            cancel_occurred = False
            stream_err = None

            try:
                with open(full_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if cancel_event and cancel_event.is_set():
                            cancel_occurred = True
                            break
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if file_progress_callback and (
                                downloaded % (256 * 1024) < 65536
                                or downloaded == content_length
                            ):
                                elapsed = time.time() - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                file_progress_callback(
                                    url, filename, downloaded,
                                    content_length, speed,
                                )
                            if self.max_speed_bps > 0:
                                elapsed = time.time() - start_time
                                expected = downloaded / self.max_speed_bps
                                if elapsed < expected:
                                    time.sleep(expected - elapsed)
            except (BrokenPipeError, ConnectionResetError) as e:
                stream_err = str(e)
                # Partial file — clean up
                if os.path.exists(full_path):
                    try:
                        os.remove(full_path)
                    except OSError:
                        pass

            if stream_err:
                return ('stream_error', stream_err, filename, downloaded)
            if cancel_occurred:
                if os.path.exists(full_path):
                    try:
                        os.remove(full_path)
                    except OSError:
                        pass
                return ('cancelled', filename)

            # Fix extension from magic bytes (catches .bin / no-ext / wrong-ext)
            try:
                new_path = _fix_extension(full_path)
                if new_path != full_path:
                    filename = os.path.basename(new_path)
            except Exception:
                pass

            return ('ok', filename, downloaded)

        # Dispatch blocking work to the default thread pool
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, _do_download)
        except Exception as e:
            stats.failed += 1
            stats.errors.append(f'Download: {url[:50]} — {e}')
            stats.failed_urls.append((url, model_name, mode, page, post, str(e)))
            _log(f'FAIL: {url[:70]} — {e}', 'ERROR')
            return (False, 0)

        action = result[0]
        if action == 'http_error':
            status_code = result[1]
            stats.failed += 1
            stats.errors.append(f'HTTP {status_code}: {url[:60]}')
            stats.failed_urls.append((url, model_name, mode, page, post, f'HTTP {status_code}'))
            _log(f'FAIL HTTP {status_code}: {url[:70]}', 'ERROR')
            return (False, 0)
        elif action == 'connection_error':
            err_msg = result[1]
            stats.failed += 1
            stats.errors.append(f'Connection: {url[:50]} — {err_msg}')
            stats.failed_urls.append((url, model_name, mode, page, post, err_msg))
            _log(f'FAIL (connection): {url[:70]} — {err_msg}', 'ERROR')
            return (False, 0)
        elif action == 'skip':
            stats.skipped += 1
            _log(f'SKIP (exists): {result[1]}')
            return (True, 0)
        elif action == 'cancelled':
            _log(f'Cancelled: {result[1]}', 'WARN')
            return (False, 0)
        elif action == 'stream_error':
            err_msg, fname = result[1], result[2]
            stats.failed += 1
            stats.errors.append(f'Stream error: {url[:50]} — {err_msg}')
            stats.failed_urls.append((url, model_name, mode, page, post, err_msg))
            _log(f'FAIL (stream): {url[:70]} — {err_msg}', 'ERROR')
            return (False, 0)
        elif action == 'ok':
            fname = result[1]
            dloaded = result[2]
            stats.completed += 1
            stats.total_bytes += dloaded
            _log(f'OK {dloaded/1024/1024:.1f}MB: {fname}')
            # Auto-extract ZIP/RAR archives if they contain media
            full_archive_path = os.path.join(filepath, fname)
            try:
                if try_extract_archive(full_archive_path, filepath):
                    _log(f'Extracted archive: {fname}')
            except Exception:
                pass
            return (True, dloaded)

        return (False, 0)

    async def download_posts_json(
        self,
        posts_json_path: str,
        model_name: str,
        progress_callback=None,
        cancel_event=None,
        file_progress_callback=None,
        streaming_resolve: bool = True,
        models_data_dir: str | None = None,
    ) -> DownloadStats:
        """Run full download from a posts.json file.

        Resolves URLs in batches and starts downloading as results come in.
        Tracks ETA, total bytes, and generates failed_downloads.txt on completion.
        """
        stats = DownloadStats()

        with open(posts_json_path) as f:
            data = json.load(f)
        mode = data.get('mode', 'normal')
        posts = data.get('posts', [])
        stats.total_posts = len(posts)
        stats.total_urls = sum(len(p.get('urls', [])) for p in posts)

        # Overall progress tracking (entire process, not per-batch)
        _overall_start = time.time()
        _overall_total = stats.total_urls
        _overall_completed = 0  # URLs fully processed (resolved + download attempted)

        def _calc_overall_eta(elapsed, done, total):
            if done <= 0 or elapsed <= 0:
                return 0
            rate = done / elapsed
            remaining = total - done
            return int(remaining / rate) if rate > 0 else 0

        if progress_callback:
            progress_callback('start', total_posts=stats.total_posts, total_urls=stats.total_urls)

        _log(f'Downloading {model_name}: {stats.total_posts} posts, {stats.total_urls} URLs')

        # Build a flat URL→(page,post_index) lookup
        url_to_post: dict[str, tuple[int, int]] = {}
        all_urls = []
        for p in posts:
            pn = p.get('page', 0)
            pi = p.get('post_index', 0)
            for u in p.get('urls', []):
                url_to_post[u] = (pn, pi)
                all_urls.append(u)

        # ── Expand gallery/album URLs (bunkr /a/, gofile /d/) ──
        _gallery_count = 0
        _expanded_all_urls: list[str] = []
        _gallery_url_to_parent: dict[str, tuple[int, int]] = {}
        for _g_idx, url in enumerate(all_urls):
            # Check cancel every 20 URLs during gallery expansion (which makes HTTP calls)
            if cancel_event and cancel_event.is_set():
                _log(f'Download cancelled during gallery expansion ({_g_idx}/{len(all_urls)})', 'WARN')
                break
            expanded = self.resolver._try_expand_gallery(url)
            if expanded:
                _gallery_count += 1
                parent_page, parent_post = url_to_post.get(url, (0, 0))
                for nu in expanded:
                    if nu not in _gallery_url_to_parent:
                        _gallery_url_to_parent[nu] = (parent_page, parent_post)
                        _expanded_all_urls.append(nu)
            else:
                _expanded_all_urls.append(url)
        if _gallery_count:
            _log(f'Expanded {_gallery_count} gallery URL(s): '
                 f'{len(all_urls)} → {len(_expanded_all_urls)} total URLs')
            all_urls = _expanded_all_urls
            url_to_post.update(_gallery_url_to_parent)
        _overall_total = len(all_urls)  # update after gallery expansion

        _log(f'Resolving {len(all_urls)} URLs...')
        if progress_callback:
            progress_callback('resolving', total=len(all_urls))

        resolved_so_far = 0
        total_for_resolve = len(all_urls)
        resolution_map: dict[str, Optional[str]] = {}
        _resolve_start = time.time()

        # Pipeline: resolve in batches, pre-start next batch while downloading current
        BATCH_SIZE = 100
        all_batches = [all_urls[i:i+BATCH_SIZE] for i in range(0, len(all_urls), BATCH_SIZE)]
        next_resolve_task = None

        for batch_idx, batch in enumerate(all_batches):
            # Check for cancel
            if cancel_event and cancel_event.is_set():
                _log(f'Download cancelled for {model_name}', 'WARN')
                break

            _log(f'Resolving batch {batch_idx+1}/{len(all_batches)} ({len(batch)} URLs)...')

            # Await this batch's resolution (already started if streaming)
            if next_resolve_task:
                batch_result = await next_resolve_task
                next_resolve_task = None
            else:
                batch_result = await self.resolver.resolve_batch(batch, cancel_event=cancel_event)
            resolution_map.update(batch_result)

            # Pre-start next batch resolution (if streaming enabled)
            if streaming_resolve and batch_idx + 1 < len(all_batches):
                next_batch = all_batches[batch_idx + 1]
                _log(f'Pre-resolving batch {batch_idx+2}/{len(all_batches)} ({len(next_batch)} URLs)...')
                next_resolve_task = asyncio.create_task(
                    self.resolver.resolve_batch(next_batch, cancel_event=cancel_event)
                )

            resolved_so_far += len(batch)
            resolve_elapsed = time.time() - _resolve_start
            resolve_rate = resolved_so_far / resolve_elapsed if resolve_elapsed > 0 else 0
            remaining_resolve = total_for_resolve - resolved_so_far
            resolve_eta = remaining_resolve / resolve_rate if resolve_rate > 0 else 0

            batch_ok = sum(1 for u in batch if batch_result.get(u))
            batch_fail = sum(1 for u in batch if not batch_result.get(u))
            eta_str = f' (ETA: {resolve_eta:.0f}s)' if resolve_eta > 0 else ''
            _log(f'Batch {batch_idx+1}: {batch_ok} OK, {batch_fail} FAIL — '
                 f'{resolved_so_far}/{total_for_resolve} resolved{eta_str}')

            _overall_elapsed_resolve = time.time() - _overall_start
            _overall_eta_resolve = _calc_overall_eta(
                _overall_elapsed_resolve, _overall_completed, _overall_total
            )
            if progress_callback:
                progress_callback('resolve_progress',
                                  resolved=resolved_so_far, total=total_for_resolve,
                                  eta=int(resolve_eta),
                                  overall_total=_overall_total,
                                  overall_completed=_overall_completed,
                                  overall_eta=_overall_eta_resolve,
                                  batch_info=f'{batch_ok} OK, {batch_fail} FAIL')

            # Download resolved URLs from this batch concurrently
            # (up to max_concurrent total, max 2 per host at a time)
            if batch_idx == 0:
                # Define once for all batches + retry pass
                _dl_sem = asyncio.Semaphore(self.max_concurrent)
                _dl_host_locks: dict[str, asyncio.Semaphore] = {}

            async def _dl_one(src_url, res_url, dl_folder, dl_stats, dl_eta_val,
                              dl_page=0, dl_post=0,
                              dl_filename='', dl_strategy='',
                              dl_mode='',
                              dl_progress_callback=None,
                              dl_file_progress_callback=None):
                # Check cancel before starting each download
                if cancel_event and cancel_event.is_set():
                    return False
                host = urlparse(res_url or src_url).netloc
                if host not in _dl_host_locks:
                    _dl_host_locks[host] = asyncio.Semaphore(4)
                # Capture stats BEFORE download to compute per-file delta + detect skips
                _skip_before = dl_stats.skipped
                async with _dl_host_locks[host]:
                    async with _dl_sem:
                        ok, dl_actual_size = await self.download_url(src_url, res_url, dl_folder, dl_stats, eta=dl_eta_val, cancel_event=cancel_event, page=dl_page, post=dl_post, model_name=model_name, mode=dl_mode, file_progress_callback=dl_file_progress_callback)
                # Use actual downloaded size from the download (not stats delta — stats is shared across concurrent downloads)
                file_size = dl_actual_size
                # Detect if this was a skip (stats.skipped increased, stats.completed didn't)
                _was_skipped = dl_stats.skipped > _skip_before
                # Fire progress callback AFTER download completes so stats are accurate
                if dl_progress_callback:
                    _overall_completed_now = dl_stats.completed + dl_stats.failed + dl_stats.skipped
                    _overall_elapsed_now = time.time() - _overall_start
                    _overall_eta_now = _calc_overall_eta(
                        _overall_elapsed_now, _overall_completed_now, _overall_total
                    )
                    dl_progress_callback('file',
                        url=src_url,
                        filename=dl_filename, strategy=dl_strategy,
                        ok=ok, size=file_size, host=host,
                        skipped=_was_skipped,
                        overall_total=_overall_total,
                        overall_completed=_overall_completed_now,
                        overall_eta=_overall_eta_now)
                return ok

            # ── Process resolved URLs in host-diverse (round-robin) order ──
            from collections import defaultdict
            # Separate unresolved (decrement total) from resolved
            resolved_for_batch = []
            for url in batch:
                if not batch_result.get(url):
                    _overall_total -= 1  # won't produce any files
                else:
                    resolved_for_batch.append(url)

            # Group resolved URLs by host for round-robin interleaving
            host_groups = defaultdict(list)
            for url in resolved_for_batch:
                rurl = batch_result[url]
                host = urlparse(rurl or url).netloc
                host_groups[host].append(url)

            # Build round-robin order: one URL from each host per round
            round_robin_order = []
            while any(host_groups.values()):
                for h in list(host_groups.keys()):
                    if host_groups[h]:
                        round_robin_order.append(host_groups[h].pop(0))
                    if not host_groups[h]:
                        del host_groups[h]

            dl_tasks = []
            round_robin_tasks = []
            for url in round_robin_order:
                if cancel_event and cancel_event.is_set():
                    break
                resolved_url = batch_result[url]
                # Get page/post index from url_to_post map
                _u_page, _u_post = url_to_post.get(url, (0, 0))
                # Skip PixelDrain URLs blocked by hotlinking detection
                if resolved_url and resolved_url.startswith('__PIXELDRAIN_BLOCKED__'):
                    file_id = resolved_url.split(':', 1)[1] if ':' in resolved_url else ''
                    stats.failed += 1
                    stats.errors.append(f'PixelDrain blocked (needs subscription): {file_id}')
                    stats.failed_urls.append((url, model_name, mode, _u_page, _u_post,
                                              'PIXELDRAIN_BLOCKED'))
                    _log(f'SKIP PixelDrain {file_id}: requires captcha/subscription — '
                         f'open in browser: https://pixeldrain.com/u/{file_id}', 'WARN')
                    continue
                # Skip bunkr thumbnails (i-maple, are images are not downloadable)
                if 'i-maple.bunkr' in (resolved_url or url):
                    _log(f'SKIP bunkr thumbnail: {url[:60]}', 'WARN')
                    stats.failed_urls.append((url, model_name, mode, _u_page, _u_post, 'BUNKR_THUMBNAIL'))
                    stats.failed += 1
                    continue
                # Find which post this URL belongs to (via url_to_post — covers gallery-expanded URLs)
                folder = os.path.join(self.output_base, model_name,
                                      f'{model_name}-page{_u_page}-post{_u_post}')
                os.makedirs(folder, exist_ok=True)
                strategy = self.resolver.classify_host(url)
                # Try to get filename from content after download
                filename = os.path.basename(urlparse(resolved_url).path) if resolved_url else url.split('/')[-1][:40]
                # Calculate download ETA
                done_so_far = stats.completed + stats.skipped
                resolved_sofar = sum(1 for v in resolution_map.values() if v)
                remaining_dl = resolved_sofar - done_so_far
                dl_elapsed = time.time() - _resolve_start
                dl_rate = done_so_far / dl_elapsed if dl_elapsed > 0 else 0
                dl_eta_val = remaining_dl / dl_rate if dl_rate > 0 else 0
                # ── Expand album/list URLs into individual file tasks ──
                _album_info = _is_album_url(resolved_url or url)
                if _album_info:
                    _host_type, _album_page = _album_info
                    _album_files = _scrape_album_files(_host_type, _album_page, self.resolver._session)
                    if _album_files:
                        for _af in _album_files:
                            # Handle PixelDrain list ZIP (streaming extraction)
                            if _af['url'].startswith('__PIXELDRAIN_LIST_ZIP__:'):
                                parts = _af['url'].split(':')
                                list_id = parts[1]
                                file_count = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
                                # Adjust totals: 1 ZIP URL → N files
                                if file_count > 1:
                                    _overall_total += file_count - 1
                                # Process the ZIP extraction
                                processed_count = self._handle_pixeldrain_list_zip(
                                    list_id, model_name, folder,
                                    start_count=_overall_completed,
                                    total_files=_overall_total,
                                    progress_callback=progress_callback,
                                    log_callback=_log_callback,
                                    cancel_event=cancel_event,
                                )
                                # Mark extracted files as completed
                                stats.completed += processed_count if processed_count > 0 else 1
                                _overall_completed += processed_count if processed_count > 0 else 1
                                _log(f'OK PixelDrain list ZIP: {list_id} ({processed_count} files)')
                            else:
                                dl_tasks.append(_dl_one(url, _af['url'], folder, stats, 0,
                                                         dl_page=_u_page,
                                                         dl_post=_u_post,
                                                         dl_filename=_af['filename'],
                                                         dl_strategy=strategy,
                                                         dl_mode=mode,
                                                         dl_progress_callback=progress_callback,
                                                         dl_file_progress_callback=file_progress_callback))
                        _overall_total += len(_album_files) - 1  # 1 URL → N files
                        break  # skip the single-file _dl_one below
                else:
                    # Single file — add to round-robin group for this host
                    round_robin_tasks.append((url, resolved_url, folder, stats,
                                              int(dl_eta_val), _u_page, _u_post,
                                              filename, strategy))

            # Gather album sub-tasks (pixeldrain album files, etc.)
            if dl_tasks:
                await asyncio.gather(*dl_tasks, return_exceptions=True)

            # Process single-file downloads — submit all at once, semaphores handle concurrency
            if round_robin_tasks:
                all_dl_tasks = []
                for item in round_robin_tasks:
                    u, rurl, f, s, eta, pp, po, fn, strat = item
                    all_dl_tasks.append(_dl_one(u, rurl, f, s, eta,
                                                dl_page=pp, dl_post=po,
                                                dl_filename=fn, dl_strategy=strat,
                                                dl_mode=mode,
                                                dl_progress_callback=progress_callback,
                                                dl_file_progress_callback=file_progress_callback))
                await asyncio.gather(*all_dl_tasks, return_exceptions=True)

            # ── Cancel check after batch ──
            if cancel_event and cancel_event.is_set():
                _log(f'Download cancelled — cleaning up output for {model_name}', 'WARN')
                # Remove output directory to avoid stale files
                model_output_dir = os.path.join(self.output_base, model_name)
                if os.path.isdir(model_output_dir):
                    try:
                        shutil.rmtree(model_output_dir, ignore_errors=True)
                        _log(f'Cleaned up output for {model_name} after cancel')
                    except Exception:
                        pass
                break

        # ── Retry unresolved URLs ─────────────────────────────────────────
        unresolved_urls = [url for url, rurl in resolution_map.items() if rurl is None]
        if unresolved_urls:
            _log(f'Retrying {len(unresolved_urls)} unresolved URLs...')
            _retry_start = time.time()
            retry_batches = [unresolved_urls[i:i+50] for i in range(0, len(unresolved_urls), 50)]
            for retry_idx, retry_batch in enumerate(retry_batches):
                if cancel_event and cancel_event.is_set():
                    _log(f'Retry cancelled for {model_name}', 'WARN')
                    break
                await asyncio.sleep(2)  # Brief cooldown before retry
                _log(f'Retry batch {retry_idx+1}/{len(retry_batches)} ({len(retry_batch)} URLs)...')
                retry_result = await self.resolver.resolve_batch(retry_batch, cancel_event=cancel_event)
                retry_tasks = []
                for url, rurl in retry_result.items():
                    if rurl:
                        resolution_map[url] = rurl  # Update with successful resolution
                        # Skip PixelDrain blocked URLs in retry too
                        if rurl.startswith('__PIXELDRAIN_BLOCKED__'):
                            file_id = rurl.split(':', 1)[1] if ':' in rurl else ''
                            stats.failed += 1
                            stats.errors.append(f'PixelDrain blocked: {file_id}')
                            _log(f'SKIP PixelDrain retry {file_id}: blocked', 'WARN')
                            continue
                        _u_page_r, _u_post_r = url_to_post.get(url, (0, 0))
                        folder = os.path.join(self.output_base, model_name,
                                              f'{model_name}-page{_u_page_r}-post{_u_post_r}')
                        os.makedirs(folder, exist_ok=True)
                        retry_tasks.append(_dl_one(url, rurl, folder, stats, 0, dl_page=_u_page_r, dl_post=_u_post_r,
                                                 dl_mode=mode,
                                                 dl_file_progress_callback=file_progress_callback))
                if retry_tasks:
                    await asyncio.gather(*retry_tasks, return_exceptions=True)
                    # Cancel check after retry batch gather
                    if cancel_event and cancel_event.is_set():
                        _log('Retry cancelled mid-batch', 'WARN')
                        break
            _retry_elapsed = time.time() - _retry_start
            retry_ok = sum(1 for u in unresolved_urls if resolution_map.get(u))
            _log(f'Retry complete: {retry_ok} newly resolved ({_retry_elapsed:.0f}s)')

        # Capture still-unresolved URLs as failed entries
        for url, resolved_url in resolution_map.items():
            if resolved_url is None:
                page, post_idx = url_to_post.get(url, (0, 0))
                stats.failed_urls.append((url, model_name, mode, page, post_idx, 'UNRESOLVED'))

        # Log resolution summary
        resolved_count = sum(1 for v in resolution_map.values() if v)
        failed_resolve = sum(1 for v in resolution_map.values() if not v)
        resolve_total_elapsed = time.time() - _resolve_start
        _log(f'Resolved: {resolved_count}/{len(all_urls)} ({failed_resolve} failed) in {resolve_total_elapsed:.0f}s')

        # Build host category breakdown
        from collections import Counter
        cat_counts: dict[str, int] = Counter()
        for url, resolved in resolution_map.items():
            strategy = self.resolver.classify_host(url)
            status = 'ok' if resolved else 'fail'
            cat_counts[f'{strategy}.{status}'] += 1
        cat_breakdown = {}
        for key, count in sorted(cat_counts.items()):
            strategy, status = key.split('.')
            if strategy not in cat_breakdown:
                cat_breakdown[strategy] = {'ok': 0, 'fail': 0}
            cat_breakdown[strategy][status] = count

        if progress_callback:
            progress_callback('resolved', ok=resolved_count, failed=failed_resolve,
                              categories=cat_breakdown)

        _log(f'Complete: {stats.completed} OK, {stats.failed} failed, {stats.skipped} skipped')
        _log(f'Total size: {stats.total_bytes / 1024 / 1024:.1f} MB')
        if progress_callback:
            _overall_completed_final = stats.completed + stats.failed + stats.skipped
            progress_callback('complete', completed=stats.completed, failed=stats.failed,
                              skipped=stats.skipped, total_bytes=stats.total_bytes,
                              overall_total=_overall_total,
                              overall_completed=_overall_completed_final,
                              errors=stats.errors[:10])

        if stats.errors:
            _log(f'Errors ({len(stats.errors)}):', 'WARN')
            for e in stats.errors[:10]:
                _log(f'  • {e}', 'WARN')

        # Write failed_downloads_<date>.txt to the models_data folder
        if stats.failed_urls:
            if models_data_dir:
                fail_dir = os.path.join(models_data_dir, model_name)
            else:
                # Compute from output_base's parent (typically project root)
                fail_dir = os.path.join(os.path.dirname(os.path.abspath(self.output_base)), 'models_data', model_name)
            os.makedirs(fail_dir, exist_ok=True)
            date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            fail_path = os.path.join(fail_dir, f'failed_downloads_{model_name}_{date_str}.txt')
            with open(fail_path, 'w') as f:
                for orig_url, _model, _mode, page, post, reason in stats.failed_urls:
                    f.write(f'{orig_url}\tPage{page}\tPost{post}\tMode{_mode}\t{reason}\n')
            _log(f'Written failed list: {fail_path} ({len(stats.failed_urls)} entries)')

        return stats


class _CancelChecker:
    """Wraps a threading.Event + optional cancel file path.
    Responds to .is_set() so it's a drop-in replacement for raw threading.Event
    anywhere in the download pipeline. The file path survives page refreshes
    while a Python thread-local Event does not.
    """
    def __init__(self, event, file_path=None):
        self._event = event
        self._file_path = file_path

    def is_set(self):
        if self._event and self._event.is_set():
            return True
        if self._file_path and os.path.exists(self._file_path):
            return True
        return False


# ── Convenience entry point ────────────────────────────────────────

def run_download(posts_json_path: str, model_name: str, output_base: str = 'output',
                 max_speed_bps: int = 1_000_000, progress_callback=None, log_callback=None,
                 max_concurrent: int = 3, cancel_event=None,
                 file_progress_callback=None,
                 cancel_file_path: str | None = None,
                 pixeldrain_api_key: str = "",
                 pixeldrain_cookies_json: str = "",
                 streaming_resolve: bool = True,
                 models_data_dir: str | None = None) -> dict:
    """Synchronous entry point for calling from app.py."""
    async def _run():
        global _log_callback
        _log_callback = log_callback
        # Wrap cancel_event with file-path checker for page-refresh resilience
        _ce = _CancelChecker(cancel_event, cancel_file_path)
        mgr = DownloadManager(output_base=output_base, max_speed_bps=max_speed_bps,
                              max_concurrent=max_concurrent,
                              pixeldrain_api_key=pixeldrain_api_key,
                              pixeldrain_cookies_json=pixeldrain_cookies_json)
        try:
            return await mgr.download_posts_json(posts_json_path, model_name, progress_callback=progress_callback,
                                                  cancel_event=_ce,
                                                  file_progress_callback=file_progress_callback,
                                                  streaming_resolve=streaming_resolve,
                                                  models_data_dir=models_data_dir)
        finally:
            await mgr.close()
    return asyncio.run(_run()).__dict__


# ── Retry failed URLs ──────────────────────────────────────────────

def retry_failed_urls(
    failed_entries: list[tuple[str, int, int]],
    model_name: str,
    output_base: str = 'output',
    max_concurrent: int = 3,
    log_callback=None,
    cancel_event=None,
    file_progress_callback=None,
) -> dict:
    """Retry downloading specific failed URLs.

    failed_entries: list of (original_url, page, post_index)
    Returns dict with keys: retried, succeeded, failed, results
    """
    async def _run():
        global _log_callback
        _log_callback = log_callback

        mgr = DownloadManager(output_base=output_base, max_concurrent=max_concurrent)
        resolver = mgr.resolver

        urls = [e[0] for e in failed_entries]
        url_info = {e[0]: (e[1], e[2]) for e in failed_entries}

        dl_sem = asyncio.Semaphore(max_concurrent)
        dl_host_locks: dict[str, asyncio.Lock] = {}
        succeeded = 0
        failed = 0
        results = []

        _log(f'Retry: {len(urls)} failed URL(s) for {model_name}')

        async def _dl_one(src_url, res_url, dl_folder, dl_page, dl_post):
            if cancel_event and cancel_event.is_set():
                return None
            host = urlparse(res_url).netloc
            if host not in dl_host_locks:
                dl_host_locks[host] = asyncio.Lock()
            async with dl_sem:
                async with dl_host_locks[host]:
                    dummy = DownloadStats()
                    ok = await mgr.download_url(
                        src_url, res_url, dl_folder, dummy,
                        cancel_event=cancel_event,
                        page=dl_page, post=dl_post,
                        file_progress_callback=file_progress_callback,
                    )
                    return (src_url, ok)

        BATCH_SIZE = 100
        all_batches = [urls[i:i + BATCH_SIZE] for i in range(0, len(urls), BATCH_SIZE)]

        for batch_idx, batch in enumerate(all_batches):
            if cancel_event and cancel_event.is_set():
                _log('Retry cancelled', 'WARN')
                break

            _log(f'Retry: resolve batch {batch_idx + 1}/{len(all_batches)} ({len(batch)} URLs)...')
            batch_result = await resolver.resolve_batch(batch, cancel_event=cancel_event)

            dl_tasks = []
            for url in batch:
                if cancel_event and cancel_event.is_set():
                    break
                resolved = batch_result.get(url)
                page, post_idx = url_info.get(url, (0, 0))

                if not resolved:
                    failed += 1
                    results.append({'url': url, 'success': False, 'reason': 'UNRESOLVED'})
                    _log(f'Retry: unresolvable — {url[:70]}', 'WARN')
                    continue

                # Skip PixelDrain blocked URLs in retry
                if resolved.startswith('__PIXELDRAIN_BLOCKED__'):
                    failed += 1
                    results.append({'url': url, 'success': False, 'reason': 'PIXELDRAIN_BLOCKED'})
                    _log(f'Retry: PixelDrain blocked — {url[:70]}', 'WARN')
                    continue

                folder = os.path.join(
                    output_base, model_name,
                    f'{model_name}-page{page}-post{post_idx}',
                )
                os.makedirs(folder, exist_ok=True)
                dl_tasks.append(_dl_one(url, resolved, folder, page, post_idx))

            if dl_tasks:
                outcomes = await asyncio.gather(*dl_tasks, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        failed += 1
                    elif outcome is None:
                        pass  # cancelled
                    else:
                        src_url, ok = outcome
                        if ok:
                            succeeded += 1
                            results.append({'url': src_url, 'success': True})
                        else:
                            failed += 1
                            results.append({'url': src_url, 'success': False, 'reason': 'Download failed'})

        await mgr.close()
        _log(f'Retry complete: {succeeded} OK, {failed} FAIL')
        return {
            'retried': len(urls),
            'succeeded': succeeded,
            'failed': failed,
            'results': results,
        }

    return asyncio.run(_run())
