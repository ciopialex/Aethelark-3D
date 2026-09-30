"""
MakerWorld Provider implementation for Aethelark-3D.
"""

import json
import threading
import time
import re
import urllib.parse
from pathlib import Path
from typing import Optional, List, Tuple, Callable, Dict, Any
from curl_cffi import requests
from bs4 import BeautifulSoup

from .base import BaseProvider, LoginRequired
from ..models import UniversalDesign, UniversalProfile, SearchResult
from ..config import config


class MakerWorldProvider(BaseProvider):
    BASE_URL = "https://makerworld.com"
    API_URL = "https://makerworld.com/api/v1/design-service"

    def __init__(self, cfg=None):
        self.config = cfg or config
        self.session = requests.Session(impersonate="chrome120")
        self._cookie_jar: str | None = None
        # get_provider() hands every caller the same instance (PROVIDERS holds
        # instances, not classes), and `browse` fans its downloads across a
        # thread pool. Without this, five threads all see an empty jar at the
        # same instant and each launches its own browser — five Chromiums for
        # the deck this was written to make cost one.
        self._seed_lock = threading.Lock()

    @property
    def platform_name(self) -> str:
        return "makerworld"

    def _get_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "X-BBL-Client-Type": "web",
            "X-BBL-Client-Version": "00.00.00.01",
            "X-BBL-App-Source": "makerworld",
            "X-BBL-Client-Name": "MakerWorld",
            "Origin": "https://makerworld.com",
            "Referer": referer or "https://makerworld.com/",
            "Accept": "application/json, text/plain, */*",
        }
        # A harvested jar and the configured cookie are mutually exclusive:
        # sending both measured HTTP 418 (bot-detect), which is how a working
        # request became a failing one. The jar is the fresher fact, so it wins.
        if self._cookie_jar:
            headers["Cookie"] = self._cookie_jar
        elif self.config.makerworld_cookie:
            headers["Cookie"] = self.config.makerworld_cookie
        if self.config.makerworld_token:
            headers["Authorization"] = f"Bearer {self.config.makerworld_token}"
        return headers

    #: Cloudflare issues this only once its challenge has been passed. `__cf_bm`
    #: arrives immediately and proves nothing, so a jar holding that alone is a
    #: jar that will be refused.
    CLEARANCE_COOKIE = "cf_clearance"
    CLEARANCE_WAIT_S = 20.0

    def _harvest_cookies(self) -> str:
        """One headless page load, purely to obtain a valid cookie jar.

        Waits for the CLEARANCE cookie, not for the DOM. `domcontentloaded`
        fires as soon as the markup parses, which on a challenged request is
        the interstitial rather than the model page — and the jar taken at that
        moment holds `__cf_bm` and no `cf_clearance`.

        That jar is then cached and, by design, amortised across a whole deck:
        one browser launch serves five candidates. So a single bad harvest
        fails all five identically. Measured in a real session:

            all 5 matches for 'BIC lighter sleeve' failed to download:
              ... (HTTP 418): {'error': 'We need to confirm that you are not a
              robot.', 'captchaId': '41267bfbb7be906c265cebfe01d77920' ...

        One captchaId across every candidate, which is the signature of one
        jar reused rather than five separate refusals.

        Raising when clearance never arrives is deliberate. The caller already
        treats an exception here as "no browser" and falls through to the
        headless resolver; that is a better answer than handing back a jar
        which cannot work and letting the deck burn against it.
        """
        from playwright.sync_api import sync_playwright
        ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=ua)
                page = ctx.new_page()
                page.goto(f"{self.BASE_URL}/en/models/124686",
                          wait_until="domcontentloaded", timeout=45000)
                deadline = time.monotonic() + self.CLEARANCE_WAIT_S
                cookies = ctx.cookies()
                while time.monotonic() < deadline:
                    if any(c["name"] == self.CLEARANCE_COOKIE for c in cookies):
                        break
                    page.wait_for_timeout(500)
                    cookies = ctx.cookies()
                else:
                    raise RuntimeError(
                        "MakerWorld is showing a bot challenge and it did not "
                        "clear within %.0fs" % self.CLEARANCE_WAIT_S)
                return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            finally:
                browser.close()

    def _seed_cookie_jar(self) -> str:
        """The jar, harvesting it the first time it is asked for.

        Amortised across a whole deck: five candidates cost one browser launch
        rather than five, which is the difference between ~47s and a few
        seconds for `a3d browse`.
        """
        if self._cookie_jar:
            return self._cookie_jar
        with self._seed_lock:
            # Re-checked inside the lock: the thread that waited here while
            # another was harvesting must use that result, not start again.
            if not self._cookie_jar:
                self._cookie_jar = self._harvest_cookies()
        return self._cookie_jar

    @staticmethod
    def parse_model_id(input_str: str) -> int:
        clean = str(input_str).strip()
        if clean.isdigit():
            return int(clean)
        match = re.search(r'/models/(\d+)', clean)
        if match:
            return int(match.group(1))
        match = re.search(r'^(\d+)', clean)
        if match:
            return int(match.group(1))
        raise ValueError(f"Could not extract a valid MakerWorld model ID from: {input_str}")

    def search(self, query: str, page: int = 1, limit: int = 20) -> SearchResult:
        encoded_query = urllib.parse.quote(query)
        search_url = f"{self.BASE_URL}/en/search/models?keyword={encoded_query}"
        
        resp = self.session.get(
            search_url,
            headers=self._get_headers(referer=self.BASE_URL),
            timeout=15
        )
        if resp.status_code != 200:
            raise RuntimeError(f"MakerWorld search failed with HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if not next_data_tag or not next_data_tag.string:
            raise RuntimeError("Failed to parse search page data from MakerWorld.")

        payload = json.loads(next_data_tag.string)
        page_props = payload.get("props", {}).get("pageProps", {})
        total = page_props.get("total", 0)
        raw_designs = page_props.get("designs", [])

        designs: List[UniversalDesign] = []
        for d in raw_designs:
            try:
                creator = d.get("designCreator", {}).get("name") or d.get("user", {}).get("name") or "Unknown"
                profiles: List[UniversalProfile] = []
                for inst in d.get("instances", []):
                    profiles.append(UniversalProfile(
                        id=inst.get("id"),
                        profile_id=inst.get("profileId"),
                        title=inst.get("title", "Standard Profile"),
                        weight_g=inst.get("weight"),
                        prediction_s=inst.get("prediction"),
                        rating_score=inst.get("score", 0.0),
                        download_count=inst.get("downloadCount", 0),
                        is_default=bool(inst.get("isDefault", False)),
                        source_platform="makerworld"
                    ))

                designs.append(UniversalDesign(
                    id=d.get("id"),
                    title=d.get("title", "Untitled"),
                    slug=d.get("slug", ""),
                    platform="makerworld",
                    url=f"{self.BASE_URL}/en/models/{d.get('id')}-{d.get('slug', '')}",
                    cover_url=d.get("cover") or d.get("coverLandscape") or d.get("coverPortrait"),
                    summary=d.get("summary", ""),
                    creator_name=creator,
                    download_count=d.get("downloadCount", 0),
                    like_count=d.get("likeCount", 0),
                    print_count=d.get("printCount", 0),
                    profiles=profiles,
                    tags=d.get("tags") or [],
                    license=d.get("license")
                ))
            except Exception:
                continue

        return SearchResult(
            query=query,
            total=total,
            platform="makerworld",
            designs=designs
        )

    def get_design(self, id_or_url: str) -> UniversalDesign:
        design_id = self.parse_model_id(id_or_url)
        detail_url = f"{self.API_URL}/design/{design_id}"

        resp = self.session.get(
            detail_url,
            headers=self._get_headers(referer=f"{self.BASE_URL}/en/models/{design_id}"),
            timeout=15
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch design #{design_id} (HTTP {resp.status_code})")

        d = resp.json()
        creator = d.get("designCreator", {}).get("name") or d.get("user", {}).get("name") or "Unknown"

        profiles: List[UniversalProfile] = []
        for inst in d.get("instances", []):
            profiles.append(UniversalProfile(
                id=inst.get("id"),
                profile_id=inst.get("profileId"),
                title=inst.get("title", "Standard Profile"),
                weight_g=inst.get("weight"),
                prediction_s=inst.get("prediction"),
                rating_score=inst.get("score", 0.0),
                download_count=inst.get("downloadCount", 0),
                is_default=bool(inst.get("isDefault", False)),
                source_platform="makerworld"
            ))

        return UniversalDesign(
            id=d.get("id"),
            title=d.get("title", "Untitled"),
            slug=d.get("slug", ""),
            platform="makerworld",
            url=f"{self.BASE_URL}/en/models/{d.get('id')}-{d.get('slug', '')}",
            cover_url=d.get("cover") or d.get("coverLandscape") or d.get("coverPortrait"),
            summary=d.get("summary", ""),
            creator_name=creator,
            download_count=d.get("downloadCount", 0),
            like_count=d.get("likeCount", 0),
            print_count=d.get("printCount", 0),
            profiles=profiles,
            tags=d.get("tags") or [],
            license=d.get("license")
        )

    def _resolve_url_via_headless(self, endpoint: str, referer_id: int) -> Tuple[str, str]:
        from playwright.sync_api import sync_playwright

        cookies = []
        for item in self.config.makerworld_cookie.split("; "):
            if "=" in item:
                k, v = item.split("=", 1)
                cookies.append({
                    "name": k,
                    "value": v,
                    "domain": ".makerworld.com",
                    "path": "/",
                })

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            # This page is loaded only to put the in-page fetch below on the
            # right origin with the right cookies; nothing is scraped from it.
            # "networkidle" waits for 500ms of silence that never comes —
            # MakerWorld is a Next.js app with continuous analytics traffic, so
            # measured here it times out at 45s while "domcontentloaded"
            # delivers the same document in 4.8s. Every download resolution
            # that reached this fallback failed on that wait alone.
            page.goto(f"{self.BASE_URL}/en/models/{referer_id}",
                      wait_until="domcontentloaded", timeout=45000)

            js_code = f"""async () => {{
                const resp = await fetch('{endpoint}', {{
                    headers: {{
                        'X-BBL-Client-Type': 'web',
                        'X-BBL-Client-Version': '00.00.00.01',
                        'X-BBL-App-Source': 'makerworld',
                        'X-BBL-Client-Name': 'MakerWorld'
                    }}
                }});
                return {{ status: resp.status, data: await resp.json() }};
            }}"""

            result = page.evaluate(js_code)
            browser.close()

            status = result.get("status")
            data = result.get("data", {})
            if status == 200 and data.get("url"):
                return data["url"], data.get("name") or "model_download"
            # Measured with no session, 2026-09-24: HTTP 403 and
            # {'code': 1, 'error': 'Please log in to download models.'}.
            if status in (401, 403) and "log in" in str(data).lower():
                raise LoginRequired(
                    "MakerWorld only lets signed-in accounts download models.",
                    "makerworld.com")
            raise RuntimeError(f"MakerWorld download resolution failed (HTTP {status}): {data}")

    def get_reviews(self, id_or_url: str, sample: int = 40, keep: int = 6) -> Dict[str, Any]:
        """What people who printed it thought: the rating, and a few comments
        worth reading, low ratings first because those are the ones to hear."""
        design_id = self.parse_model_id(id_or_url)
        referer = f"{self.BASE_URL}/en/models/{design_id}"
        d = self.session.get(f"{self.API_URL}/design/{design_id}",
                             headers=self._get_headers(referer=referer), timeout=15).json()
        total = sum(int(i.get("ratingScoreTotal") or 0) for i in d.get("instances") or [])
        count = sum(int(i.get("ratingCount") or 0) for i in d.get("instances") or [])
        r = self.session.get(
            f"{self.BASE_URL}/api/v1/comment-service/commentandrating",
            params={"designId": design_id, "offset": 0, "limit": sample, "type": 0, "sort": 0},
            headers=self._get_headers(referer=referer), timeout=15).json()
        low, good = [], []
        for hit in r.get("hits") or []:
            c = hit.get("comment") or {}
            ri = hit.get("ratingItem") or {}
            text = " ".join(str(c.get("content") or ri.get("content") or "").split())
            if len(text) < 25 or not any(ch.isalpha() for ch in text):
                continue
            score = ri.get("score")
            entry = {"text": text[:220], "stars": score, "likes": c.get("likeCount")}
            (low if (score is not None and score <= 3) else good).append(entry)
        good.sort(key=lambda e: (e.get("likes") or 0, len(e["text"])), reverse=True)
        return {
            "id": design_id, "title": d.get("title"),
            "url": f"{self.BASE_URL}/en/models/{design_id}-{d.get('slug', '')}",
            "rating": round(total / count, 2) if count else None,
            "rating_count": count, "print_count": d.get("printCount"),
            "comment_count": d.get("commentCount"), "like_count": d.get("likeCount"),
            "critical": low[:3], "comments": good[:keep],
        }

    def get_download_url(self, design_id: int, profile_id: Optional[int] = None, raw_stl: bool = False) -> Tuple[str, str]:
        if raw_stl or not profile_id:
            endpoint = f"{self.API_URL}/design/{design_id}/model?type=download"
        else:
            endpoint = f"{self.API_URL}/instance/{profile_id}/f3mf?type=download&fileType=3mf&devModelName=O1D"

        for attempt in (1, 2):
            if not self._cookie_jar:
                try:
                    self._seed_cookie_jar()
                except Exception:
                    break               # no browser: fall through to headless
            resp = self.session.get(endpoint, headers=self._get_headers(
                f"{self.BASE_URL}/en/models/{design_id}"), timeout=15)
            if resp.status_code == 200:
                d = resp.json()
                if d.get("url"):
                    return d["url"], d.get("name") or f"model_{design_id}.3mf"
            if resp.status_code in (401, 403, 418) and attempt == 1:
                self._cookie_jar = None     # stale; re-seed exactly once
                continue
            break

        # Fallback to headless session with auth cookies
        return self._resolve_url_via_headless(endpoint, design_id)

    def download_file(
        self,
        download_url: str,
        dest_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

        resp = self.session.get(download_url, stream=True, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Download failed from CDN (HTTP {resp.status_code})")

        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(temp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)

        temp_path.replace(dest_path)
        return dest_path
