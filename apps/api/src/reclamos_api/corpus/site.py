"""Access to the gob.pe pages that publish OSIPTEL's TRASU resolutions."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from reclamos_api.corpus.parse import MONTHS
from selectolax.lexbor import LexborHTMLParser

USER_AGENT = "reclamos-corpus/0.1 (+https://github.com/totallynotdavid/reclamos)"
SITE = "https://www.gob.pe"
COLLECTION_URL = f"{SITE}/institucion/osiptel/colecciones/52616-jurisprudencia"
PAGE_SIZE = 100
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# A readable robots.txt stays valid for an hour. An unreadable one is retried
# after five minutes. This keeps transient failures from blocking a host or
# causing repeated requests.
ROBOTS_TTL = 3600.0
ROBOTS_RETRY_TTL = 300.0


class FetchError(Exception):
    """A request failed. `permanent` says whether asking again can help."""

    def __init__(self, reason: str, *, permanent: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


class RobotsPolicy:
    """The Allow and Disallow rules of one host for any crawler."""

    def __init__(self, text: str | None, *, unreachable: bool = False) -> None:
        self.unreachable = unreachable
        self.rules: list[tuple[int, bool, re.Pattern[str]]] = []
        self.crawl_delay = 0.0
        if text is not None:
            self._parse(text)

    def _parse(self, text: str) -> None:
        applies = False
        reading_agents = False
        for line in text.splitlines():
            field, _, value = line.split("#", 1)[0].partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if not reading_agents:
                    applies = False
                reading_agents = True
                applies = applies or value == "*"
                continue
            reading_agents = False
            if not applies:
                continue
            if field in {"allow", "disallow"} and value:
                pattern = re.escape(value).replace(r"\*", ".*")
                pattern = pattern[:-2] + "$" if pattern.endswith(r"\$") else pattern
                self.rules.append((len(value), field == "allow", re.compile(pattern)))
            elif field == "crawl-delay" and re.fullmatch(r"\d+(?:\.\d+)?", value):
                self.crawl_delay = float(value)

    def allows(self, url: str) -> bool:
        if self.unreachable:
            return False
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        # RFC 9309: the longest matching rule wins, and Allow wins a tie.
        matching = [
            (length, allow)
            for length, allow, pattern in self.rules
            if pattern.match(target)
        ]
        return max(matching)[1] if matching else True


class Fetcher:
    """Serializes requests, spaces them out, and obeys each host's robots.txt."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last = float("-inf")
        self._robots: dict[str, tuple[RobotsPolicy, float]] = {}

    @classmethod
    def create(
        cls,
        *,
        min_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "Fetcher":
        # Redirects are followed by hand: each destination needs its own
        # robots.txt check and its own place in the one-request-per-second queue.
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
            transport=transport,
        )
        return cls(client, min_interval=min_interval)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, xhr: bool = False
    ) -> httpx.Response:
        async with self._lock:
            headers = (
                {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "text/javascript, application/javascript, */*; q=0.01",
                }
                if xhr
                else {}
            )
            return await self._follow(
                f"{url}?{urlencode(params)}" if params else url, headers, obey=True
            )

    async def _follow(
        self, url: str, headers: dict[str, str], *, obey: bool
    ) -> httpx.Response:
        """Request `url` and follow up to MAX_REDIRECTS redirects.

        With `obey`, every hop is checked against its own host's robots.txt
        before it is requested. `headers` belong to the host that was asked, so
        a redirect to another host drops them.
        """
        for _ in range(MAX_REDIRECTS + 1):
            delay = 0.0
            if obey:
                policy = await self._policy(url)
                if policy.unreachable:
                    raise FetchError("robots_unreachable", permanent=False)
                if not policy.allows(url):
                    raise FetchError("robots_disallowed", permanent=True)
                delay = policy.crawl_delay
            response = await self._send(url, headers, delay)
            if response.status_code not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            target = urljoin(url, location) if location else ""
            if urlsplit(target).scheme not in {"http", "https"}:
                raise FetchError("bad_redirect", permanent=True)
            if urlsplit(target).netloc != urlsplit(url).netloc:
                headers = {}
            url = target
        raise FetchError("too_many_redirects", permanent=True)

    async def _policy(self, url: str) -> RobotsPolicy:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached and self._clock() < cached[1]:
            return cached[0]
        try:
            response = await self._follow(f"{origin}/robots.txt", {}, obey=False)
            policy, ttl = RobotsPolicy(response.text), ROBOTS_TTL
        except FetchError as error:
            # RFC 9309: a missing robots.txt allows everything, a failing server
            # allows nothing.
            policy = RobotsPolicy(None, unreachable=not error.permanent)
            ttl = ROBOTS_RETRY_TTL if policy.unreachable else ROBOTS_TTL
        self._robots[origin] = (policy, self._clock() + ttl)
        return policy

    async def _send(
        self, url: str, headers: dict[str, str] | None = None, delay: float = 0.0
    ) -> httpx.Response:
        wait = max(self.min_interval, delay) - (self._clock() - self._last)
        if wait > 0:
            await self._sleep(wait)
        try:
            response = await self.client.get(
                url, headers=headers, follow_redirects=False
            )
        except httpx.HTTPError as error:
            raise FetchError(
                f"network_{type(error).__name__}", permanent=False
            ) from error
        finally:
            self._last = self._clock()
        if response.status_code >= 400:
            transient = (
                response.status_code in {408, 425, 429} or response.status_code >= 500
            )
            raise FetchError(f"http_{response.status_code}", permanent=not transient)
        return response


@dataclass(frozen=True, slots=True)
class ListedResolution:
    id: int
    page_url: str
    expediente: str
    published_on: date


@dataclass(frozen=True, slots=True)
class Listing:
    total: int
    items: list[ListedResolution]


@dataclass(frozen=True, slots=True)
class ResolutionPage:
    summary: str
    pdf_url: str


def parse_spanish_date(text: str) -> date:
    match = re.fullmatch(r"\s*(\d{1,2}) de (\w+) de (\d{4})\s*", text)
    if not match or match.group(2).lower() not in MONTHS:
        raise ValueError(f"not a Spanish date: {text!r}")
    return date(
        int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
    )


async def fetch_listing(fetcher: Fetcher, start: date, end: date) -> Listing:
    """List the compendium entries published from `start` to `end` inclusive.

    The form that filters by date answers an XHR with JavaScript that replaces
    the count and the result cards. robots.txt disallows `sheet=` pagination,
    so a window with more than PAGE_SIZE entries has to be split by the caller.
    """
    response = await fetcher.get(
        COLLECTION_URL,
        params={
            "filter[order]": "publication_asc",
            "filter[per_page]": str(PAGE_SIZE),
            "filter[start_date]": start.isoformat(),
            "filter[end_date]": end.isoformat(),
        },
        xhr=True,
    )
    return parse_listing(response.text)


def parse_listing(javascript: str) -> Listing:
    # A JavaScript string may hold a raw tab, which strict JSON forbids. The
    # paginator markup of a long listing does.
    fragments = [
        json.loads(f'"{literal}"', strict=False)
        for literal in re.findall(r'\.html\("((?:[^"\\]|\\.)*)"\)', javascript)
    ]
    count = next(
        (m for f in fragments if (m := re.fullmatch(r"(\d+) resultados?", f))), None
    )
    if count is None:
        raise ValueError("listing response has no result count")
    items: list[ListedResolution] = []
    for fragment in fragments:
        for card in LexborHTMLParser(fragment).css("div.shadow-campaign-card"):
            link = card.css_first("a[href*='/normas-legales/']")
            heading = card.css_first("div.border-b")
            if link is None or heading is None:
                raise ValueError("listing card without link or date")
            href = link.attributes["href"] or ""
            id_match = re.search(r"/normas-legales/(\d+)-", href)
            if id_match is None:
                raise ValueError(f"listing link without a document id: {href!r}")
            items.append(
                ListedResolution(
                    id=int(id_match.group(1)),
                    page_url=urljoin(SITE, href),
                    expediente=link.text(strip=True),
                    published_on=parse_spanish_date(heading.text(strip=True)),
                )
            )
    return Listing(total=int(count.group(1)), items=items)


def parse_resolution_page(html: str, *, base_url: str) -> ResolutionPage:
    tree = LexborHTMLParser(html)
    description = tree.css_first("div.rule-content")
    link = tree.css_first("div.institution-document__files a[href*='.pdf']")
    if link is None:
        raise ValueError("no_pdf_link")
    return ResolutionPage(
        summary=description.text(separator="\n", strip=True) if description else "",
        pdf_url=urljoin(base_url, link.attributes["href"] or ""),
    )


def split_window(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]]:
    middle = start + (end - start) // 2
    return (start, middle), (middle + timedelta(days=1), end)
