"""Data models for the Simp URL Fetcher application."""

from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime
import json
import os


@dataclass
class Post:
    """A single forum post with its URLs."""
    post_id: str
    page: int
    post_index: int  # position on the page (1-based)
    author: str = ""
    urls: list[str] = field(default_factory=list)
    source_file: str = ""


@dataclass
class ScanResult:
    """Result of scanning a set of HTML files."""
    model_name: str
    forum_source: str
    created: str = ""
    total_posts: int = 0
    total_urls: int = 0
    posts: list[Post] = field(default_factory=list)

    def __post_init__(self):
        if not self.created:
            self.created = datetime.now().isoformat()

    def to_dict(self) -> dict:
        # Always compute totals from actual posts
        self.total_posts = len(self.posts)
        self.total_urls = sum(len(p.urls) for p in self.posts)
        return {
            "model_name": self.model_name,
            "forum_source": self.forum_source,
            "created": self.created,
            "total_posts": self.total_posts,
            "total_urls": self.total_urls,
            "posts": [asdict(p) for p in self.posts],
        }

    def to_json(self, indent=2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "ScanResult":
        posts = [Post(**p) for p in data.get("posts", [])]
        sr = cls(
            model_name=data["model_name"],
            forum_source=data.get("forum_source", ""),
            created=data.get("created", ""),
            total_posts=len(posts),
            total_urls=data.get("total_urls", 0),
            posts=posts,
        )
        # Fix counts
        sr.total_urls = sum(len(p.urls) for p in posts)
        return sr


@dataclass
class Settings:
    """Application settings."""
    input_dir: str = "input"
    models_data_dir: str = "models_data"
    output_dir: str = "output"
    sort_dir: str = os.path.expanduser("~/Downloads")
    extraction_mode: str = "normal"  # normal, no_filter, reverse
    max_concurrent_downloads: int = 3  # max simultaneous downloads
    max_speed_mbps: int = 0  # 0 = unlimited, in MB/s
    # Crawler settings
    cookies_json: str = ""  # EditThisCookie JSON array
    request_delay: float = 3.0  # seconds between requests
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    # Display
    site_title: str = "Simp URL Fetcher"
    update_branch: str = "main"  # git branch for updates
    pixeldrain_api_key: str = ""  # API key for pixeldrain.com (free-tier works)
    pixeldrain_cookies_json: str = ""  # pd_auth_key cookie JSON for pixeldrain
    streaming_resolve: bool = True  # pre-resolve next batch while downloading current one

    SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

    def save(self):
        """Save settings to JSON file."""
        with open(self.SETTINGS_PATH, 'w') as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from JSON file, or return defaults.
        Falls back to PIXELDRAIN_APIKEY env var if not set in JSON."""
        if os.path.exists(cls.SETTINGS_PATH):
            try:
                with open(cls.SETTINGS_PATH) as f:
                    data = json.load(f)
                obj = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
                # Env var overrides empty JSON value (for .env / Docker / CI use)
                if not obj.pixeldrain_api_key:
                    obj.pixeldrain_api_key = os.environ.get("PIXELDRAIN_APIKEY", "")
                if not obj.pixeldrain_cookies_json:
                    obj.pixeldrain_cookies_json = os.environ.get("PIXELDRAIN_COOKIES", "")
                return obj
            except (json.JSONDecodeError, TypeError):
                pass
        obj = cls()
        if not obj.pixeldrain_api_key:
            obj.pixeldrain_api_key = os.environ.get("PIXELDRAIN_APIKEY", "")
        return obj
