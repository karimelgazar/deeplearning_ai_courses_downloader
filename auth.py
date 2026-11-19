"""
Authentication module for DeepLearning.AI Course Downloader.

Supports Netscape/Mozilla cookie files (--cookies-file or DL_AI_COOKIES_FILE)
as well as direct browser cookie extraction using browser_cookie3.
"""

from __future__ import annotations

import logging
import os
from http.cookiejar import CookieJar, LoadError, MozillaCookieJar
from pathlib import Path
from typing import Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

ENV_COOKIES_FILE = "DL_AI_COOKIES_FILE"

# Default headers to use for requests
DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9,ar;q=0.8,fr;q=0.7,pt;q=0.6,ms;q=0.5",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=0, i",
    "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
}

# Supported browsers
SUPPORTED_BROWSERS = [
    "chrome",
    "chromium",
    "opera",
    "opera_gx",
    "brave",
    "edge",
    "vivaldi",
    "firefox",
    "librewolf",
    "safari",
]


class AuthenticationError(RuntimeError):
    """Raised when cookies cannot be loaded or validated."""


def resolve_cookies_file(
    cookies_file: Optional[Union[str, Path]] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Resolve cookie file path with precedence:
      1. Explicit CLI argument (`cookies_file`)
      2. Environment variable (`DL_AI_COOKIES_FILE`)
    Returns (path, source_label) or (None, None).
    """
    if cookies_file:
        return Path(cookies_file).expanduser().resolve(), "--cookies-file"

    env_val = os.environ.get(ENV_COOKIES_FILE, "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve(), ENV_COOKIES_FILE

    return None, None


def load_cookies_from_file(cookies_file: Union[str, Path]) -> MozillaCookieJar:
    """
    Load cookies from a Netscape/Mozilla format cookies.txt file.
    Never logs cookie values.
    """
    path = Path(cookies_file).expanduser().resolve()

    if not path.exists() or not path.is_file():
        raise AuthenticationError(f"Cookie file not found: {path}")

    if path.stat().st_size == 0:
        raise AuthenticationError(f"Cookie file is empty: {path}")

    cookie_jar = MozillaCookieJar(str(path))
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except (LoadError, UnicodeDecodeError, ValueError, OSError) as exc:
        raise AuthenticationError(
            f"Invalid Netscape/Mozilla cookie file format in '{path.name}': {exc}"
        ) from exc

    if len(cookie_jar) == 0:
        raise AuthenticationError(
            f"No valid cookies were loaded from '{path.name}'. "
            "Export cookies in Netscape format while logged into https://learn.deeplearning.ai."
        )

    logging.debug("Loaded %d cookies from cookie file '%s'",
                  len(cookie_jar), path.name)
    return cookie_jar


def get_cookies_from_browser(browser: str, domain_name: str = "deeplearning.ai") -> CookieJar:
    """
    Extract cookies from a browser for the specified domain.
    """
    if browser_cookie3 is None:
        raise ImportError(
            "browser_cookie3 is required. Install it with: pip install browser-cookie3")

    if browser not in SUPPORTED_BROWSERS:
        raise AuthenticationError(
            f"Unsupported browser: {browser}. Supported browsers: {', '.join(SUPPORTED_BROWSERS)}"
        )

    browser_map = {
        "chrome": browser_cookie3.chrome,
        "chromium": browser_cookie3.chromium,
        "opera": browser_cookie3.opera,
        "opera_gx": browser_cookie3.opera_gx,
        "brave": browser_cookie3.brave,
        "edge": browser_cookie3.edge,
        "vivaldi": browser_cookie3.vivaldi,
        "firefox": browser_cookie3.firefox,
        "librewolf": browser_cookie3.librewolf,
        "safari": browser_cookie3.safari,
    }

    try:
        cookie_jar = browser_map[browser](domain_name=domain_name)
    except Exception as exc:
        raise AuthenticationError(
            f"Failed to extract cookies from {browser}: {exc}. "
            "On Windows, Chrome encrypts cookies with App-Bound Encryption. "
            "Export your cookies to a Netscape cookies.txt file and pass "
            '--cookies-file "path\\to\\cookies.txt" (or set DL_AI_COOKIES_FILE).'
        ) from exc

    if not cookie_jar or len(cookie_jar) == 0:
        raise AuthenticationError(
            f"No cookies found for domain '{domain_name}' in {browser}. "
            "Make sure you are logged into DeepLearning.AI in your browser."
        )

    logging.debug("Extracted %d cookies from %s for %s",
                  len(cookie_jar), browser, domain_name)
    return cookie_jar


def _attach_retry_adapter(session: requests.Session, retry_count: int) -> None:
    """Attach HTTPAdapter with exponential backoff for transient network/server errors."""
    if retry_count <= 0:
        return

    retry_strategy = Retry(
        total=retry_count,
        connect=retry_count,
        read=retry_count,
        status=retry_count,
        backoff_factor=1.0,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def create_session(
    browser: str = "chrome",
    cookies_file: Optional[Union[str, Path]] = None,
    retry_count: int = 3,
) -> requests.Session:
    """
    Create a requests session with cookies, headers, and transient retry handling.

    Precedence for cookies:
      1. `cookies_file` parameter (--cookies-file)
      2. `DL_AI_COOKIES_FILE` environment variable
      3. Direct browser cookie extraction via `browser_cookie3`

    Returns:
        Configured requests.Session instance (with `.auth_source` metadata attribute).
    """
    session = requests.Session()
    resolved_path, source_label = resolve_cookies_file(cookies_file)

    if resolved_path is not None:
        cookie_jar = load_cookies_from_file(resolved_path)
        auth_source = f"{source_label} ({resolved_path.name})"
    else:
        cookie_jar = get_cookies_from_browser(browser)
        auth_source = f"browser ({browser})"

    session.cookies = cookie_jar  # type: ignore[assignment]
    session.headers.update(DEFAULT_HEADERS)
    _attach_retry_adapter(session, retry_count=retry_count)

    # Non-sensitive description of where cookies were loaded from
    setattr(session, "auth_source", auth_source)
    return session
