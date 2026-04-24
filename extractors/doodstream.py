import asyncio
import logging
import re
import time
from urllib.parse import urlparse, urljoin

import aiohttp
import cloudscraper
from config import BYPARR_URL, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_solver_proxy_url
from utils.cookie_cache import CookieCache

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class Settings:
    byparr_url = BYPARR_URL

settings = Settings()

_DOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

class DoodStreamExtractor:
    """
    DoodStream / PlayMogo extractor using Network Sniffing (Camoufox) 
    to capture dynamic pass_md5 links directly from traffic.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers["User-Agent"] = _DOOD_UA
        self.proxies = proxies or []
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.cache = CookieCache("dood")

    def _get_proxy(self, url: str) -> str | None:
        return get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)

    async def extract(self, url: str, **kwargs):
        """
        Main extraction entry point. 
        Uses ONLY cloudscraper as requested.
        """
        parsed = urlparse(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        if not video_id:
            raise ExtractorError("Invalid DoodStream URL: no video ID found")

        embed_url = url if "/e/" in url else f"https://{parsed.netloc}/e/{video_id}"
        
        # --- PHASE 1: cloudscraper (ONLY) ---
        try:
            logger.info(f"🚀 DoodStream: Trying cloudscraper extraction for {embed_url}")
            scraper = cloudscraper.create_scraper()
            # cloudscraper is synchronous, so we run it in a thread
            r = await asyncio.to_thread(scraper.get, embed_url, headers={"User-Agent": _DOOD_UA}, timeout=30)
            
            if r.status_code == 200:
                html = r.text
                pass_match = re.search(r"(/pass_md5/[^'\"<>\s]+)", html)
                token_match = re.search(r"token=([^&\s'\"]+)", html)
                
                if pass_match and token_match:
                    pass_path = pass_match.group(1)
                    token = token_match.group(1)
                    pass_url = urljoin(embed_url, pass_path)
                    
                    logger.info(f"🔗 Cloudscraper found pass_md5 path: {pass_path}")
                    
                    resp = await asyncio.to_thread(scraper.get, pass_url, headers={"Referer": embed_url}, timeout=30)
                    if resp.status_code == 200 and len(resp.text) > 10:
                        base_stream = resp.text.strip()
                        logger.info("✅ DoodStream: cloudscraper extraction successful!")
                        return self._finalize_extraction(base_stream, html, embed_url, _DOOD_UA)
            
                raise ExtractorError(f"DoodStream: cloudscraper failed with status {r.status_code}")
            else:
                raise ExtractorError(f"DoodStream: cloudscraper failed to fetch embed page (status {r.status_code})")
                
        except Exception as e:
            logger.error(f"❌ DoodStream: cloudscraper error: {e}")
            raise ExtractorError(f"DoodStream: cloudscraper extraction failed: {e}")

    def _finalize_extraction(self, base_stream: str, html: str, base_url: str, ua: str) -> dict:
        """Constructs the final URL from captured data."""
        if "RELOAD" in base_stream or len(base_stream) < 5:
            raise ExtractorError(f"DoodStream: Captured pass_md5 is invalid ({base_stream[:20]})")

        # Find token and expiry in the captured HTML
        token_match = re.search(r"token=([^&\s'\"]+)", html)
        if not token_match:
            token_match = re.search(r"['\"]?token['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
        if not token_match:
            token_match = re.search(r"window\.[a-z0-9_]+\s*=\s*['\"]([^'\"]{20,})['\"]", html)

        if not token_match:
             raise ExtractorError("DoodStream: token not found in HTML")
            
        token = token_match.group(1)
        expiry_match = re.search(r"expiry[:=]\s*['\"]?(\d+)['\"]?", html)
        expiry = expiry_match.group(1) if expiry_match else str(int(time.time()))
        
        import random
        import string
        rand_str = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        final_url = f"{base_stream}{rand_str}?token={token}&expiry={expiry}"

        logger.info(f"✅ DoodStream successful sniffed extraction: {final_url[:60]}...")

        return {
            "destination_url": final_url,
            "request_headers": {"User-Agent": ua, "Referer": f"{base_url}/", "Accept": "*/*"},
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        pass
