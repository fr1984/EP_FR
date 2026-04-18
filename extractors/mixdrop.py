import logging
import random
import re
import base64
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class MixdropExtractor:
    """Mixdrop URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.proxies = proxies or []

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = self._get_random_proxy()
            if proxy:
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=60, enable_cleanup_closed=True, force_close=False, use_dns_cache=True)

            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["user-agent"]})
        return self.session

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Mixdrop URL."""
        session = await self._get_session()
        
        # Handle wrappers like stayonline.pro
        if "stayonline.pro" in url:
            try:
                link_id = url.rstrip("/").split("/")[-1]
                async with session.post(
                    "https://stayonline.pro/ajax/linkView.php",
                    data={"id": link_id},
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": url,
                        "User-Agent": self.base_headers["user-agent"]
                    }
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "success":
                        new_url = data["data"]["value"]
                        logger.info(f"Resolved stayonline.pro wrapper: {url} -> {new_url}")
                        url = new_url
                    else:
                        logger.warning(f"Failed to resolve stayonline.pro wrapper: {data.get('message')}")
            except Exception as e:
                logger.error(f"Error resolving stayonline.pro wrapper: {e}")

        # Handle safego.cc wrapper
        if "safego.cc" in url:
            try:
                from bs4 import BeautifulSoup
                try:
                    import ddddocr
                except ImportError:
                    ddddocr = None
                
                logger.info(f"Step 1: Fetching safego.cc page: {url}")
                async with session.get(url) as resp:
                    text = await resp.text()
                
                soup = BeautifulSoup(text, "lxml")
                img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,'))
                
                if img_tag and ddddocr:
                    img_data_b64 = img_tag["src"].split(",")[1]
                    img_data = base64.b64decode(img_data_b64)
                    
                    ocr = ddddocr.DdddOcr(show_ad=False)
                    res = ocr.classification(img_data)
                    logger.info(f"Step 2: Solved safego.cc captcha: {res}")
                    
                    async with session.post(url, data={"captch5": res}, headers={"Referer": url}) as resp:
                        text = await resp.text()
                        soup = BeautifulSoup(text, "lxml")
                else:
                    if not ddddocr:
                        logger.warning("ddddocr not installed, skipping captcha solve for safego.cc")
                
                # Look for "Proceed to video" link
                proceed_link = soup.find("a", string=re.compile(r'Proceed to video', re.I))
                if not proceed_link:
                    # Try to find any link with Mixdrop in it
                    proceed_link = soup.find("a", href=re.compile(r'mixdrop', re.I))
                
                if proceed_link:
                    new_url = proceed_link["href"]
                    if new_url.startswith("/"):
                        from urllib.parse import urljoin
                        new_url = urljoin(url, new_url)
                    logger.info(f"Step 3: Resolved safego.cc wrapper: {url} -> {new_url}")
                    url = new_url
                    # normalization will happen after this block
                else:
                    logger.warning(f"Failed to find redirect link in safego.cc page.")
            except Exception as e:
                logger.error(f"Error resolving safego.cc wrapper: {e}")

        # Normalize URL and ensure it's an embed URL if possible
        # Mixdrop mirrors: .co, .to, .ps, .ch, .ag, .gl, .club, .net, .top, .nz
        if "/f/" in url:
            url = url.replace("/f/", "/e/")
        if "/emb/" in url:
            url = url.replace("/emb/", "/e/")
        
        # Keep original domain if it's a known mirror, otherwise default to .to
        # Note: mixdrop.vip and mixdrop.ps are removed from here to force normalization to .to
        # because they often have bot protection (meta refresh) that eval_solver doesn't handle.
        known_mirrors = ["mixdrop.co", "mixdrop.to", "mixdrop.ch", "mixdrop.ag", 
                         "mixdrop.gl", "mixdrop.club", "m1xdrop.net", "mixdrop.top", "mixdrop.nz",
                         "mixdrop.vc", "mixdrop.sx", "mixdrop.bz", "mdy48tn97.com"]
        
        mirror_found = False
        for mirror in known_mirrors:
            if mirror in url:
                mirror_found = True
                break
        
        if not mirror_found and "mixdrop" in url:
            # Try to force a known good mirror
            parts = url.split("/")
            if len(parts) > 2:
                parts[2] = "mixdrop.to"
                url = "/".join(parts)

        headers = {"accept-language": "en-US,en;q=0.5", "referer": url}
        
        # Multiple patterns to try in order of likelihood
        patterns = [
            r'MDCore.wurl ?= ?\"(.*?)\"',  # Primary pattern
            r'wurl ?= ?\"(.*?)\"',          # Simplified pattern
            r'src: ?\"(.*?)\"',             # Alternative pattern
            r'file: ?\"(.*?)\"',            # Another alternative
            r'https?://[^\"\']+\.mp4[^\"\']*'  # Direct MP4 URL pattern
        ]

        session = await self._get_session()
        
        try:
            final_url = await eval_solver(session, url, headers, patterns)
            
            # Validate extracted URL
            if not final_url or len(final_url) < 10:
                raise ExtractorError(f"Extracted URL appears invalid: {final_url}")
            
            logger.info(f"Successfully extracted Mixdrop URL: {final_url[:50]}...")
            
            self.base_headers["referer"] = url
            return {
                "destination_url": final_url,
                "request_headers": self.base_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }
        except Exception as e:
            error_message = str(e)
            # Per errori di video non trovato, non loggare il traceback perché sono errori attesi
            if "not found" in error_message.lower() or "unavailable" in error_message.lower():
                logger.warning(f"Mixdrop video not available at {url}: {error_message}")
            else:
                logger.error(f"Failed to extract Mixdrop URL from {url}: {error_message}")
            raise ExtractorError(f"Mixdrop extraction failed: {str(e)}") from e

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
