import asyncio
from collections.abc import Callable
from datetime import date

import httpx
import pytest
from gobpe import FIXTURES, LISTED_IDS, PUBLISHED_ON, RecordedSite
from reclamos_api.corpus.site import (
    COLLECTION_URL,
    USER_AGENT,
    Fetcher,
    FetchError,
    RobotsPolicy,
    fetch_listing,
    parse_listing,
    parse_resolution_page,
    split_window,
)


class FakeClock:
    """Time that moves only when the fetcher sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def fetcher_for(site: RecordedSite, clock: FakeClock | None = None) -> Fetcher:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(site), headers={"User-Agent": USER_AGENT}
    )
    if clock is None:
        return Fetcher(client, min_interval=0)
    return Fetcher(client, min_interval=1.0, clock=clock, sleep=clock.sleep)


def test_the_listing_names_every_resolution() -> None:
    listing = parse_listing((FIXTURES / "listing-2022.js").read_text())

    assert listing.total == 8
    assert sorted(item.id for item in listing.items) == sorted(LISTED_IDS)
    assert {item.published_on for item in listing.items} == {PUBLISHED_ON}
    queja = next(item for item in listing.items if item.id == 6134941)
    assert queja.expediente == "Expediente N.° 0038402-2022/TRASU/ST-RQJ"
    assert queja.page_url == (
        "https://www.gob.pe/institucion/osiptel/normas-legales/"
        "6134941-0038402-2022-trasu-st-rqj"
    )


def test_a_page_links_its_pdf() -> None:
    page = parse_resolution_page(
        (FIXTURES / "6134941.html").read_text(),
        base_url="https://www.gob.pe/institucion/osiptel/normas-legales/6134941-x",
    )

    assert page.pdf_url.startswith("https://cdn.www.gob.pe/uploads/document/file/")
    assert "6134941" in page.pdf_url
    assert "IMPROCEDENTE" in page.summary


def test_a_page_without_a_pdf_says_so() -> None:
    with pytest.raises(ValueError, match="no_pdf_link"):
        parse_resolution_page("<html><body>nada</body></html>", base_url="https://x/")


def test_a_window_splits_into_two_that_cover_it() -> None:
    first, second = split_window(date(2022, 1, 1), date(2022, 1, 10))

    assert first == (date(2022, 1, 1), date(2022, 1, 5))
    assert second == (date(2022, 1, 6), date(2022, 1, 10))


def test_robots_rules_of_gob_pe() -> None:
    policy = RobotsPolicy((FIXTURES / "robots.txt").read_text())

    assert policy.allows(COLLECTION_URL)
    assert not policy.allows(f"{COLLECTION_URL}?sheet=2")
    assert not policy.allows(f"{COLLECTION_URL}?filter[per_page]=100&sheet=2")
    assert not policy.allows("https://www.gob.pe/admin/users")


def test_the_longest_matching_robots_rule_wins() -> None:
    policy = RobotsPolicy(
        "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nCrawl-delay: 3\n"
    )

    assert not policy.allows("https://h/a/c")
    assert policy.allows("https://h/a/b/c")
    assert policy.crawl_delay == 3


def test_requests_are_a_second_apart_and_say_who_asks() -> None:
    site = RecordedSite()
    clock = FakeClock()

    async def scenario() -> None:
        fetcher = fetcher_for(site, clock)
        try:
            await fetcher_listing(fetcher)
            for listed in (6134934, 6134935):
                await fetcher.get(
                    f"https://www.gob.pe/institucion/osiptel/normas-legales/{listed}-x"
                )
        finally:
            await fetcher.aclose()

    async def fetcher_listing(fetcher: Fetcher) -> None:
        await fetch_listing(fetcher, date(2022, 1, 1), date(2022, 12, 31))

    asyncio.run(scenario())

    paths = [request.url.path for request in site.requests]
    assert paths[0] == "/robots.txt"
    assert len(paths) == 4
    # The first request has nothing before it to wait for.
    assert clock.sleeps[1:] == [1.0] * (len(paths) - 2)
    for request in site.requests:
        assert request.headers["user-agent"] == USER_AGENT
    assert "github.com/totallynotdavid/reclamos" in USER_AGENT
    assert site.requests[1].headers["x-requested-with"] == "XMLHttpRequest"


def test_the_fetcher_never_requests_what_robots_disallows() -> None:
    site = RecordedSite()

    async def scenario() -> None:
        fetcher = fetcher_for(site)
        try:
            with pytest.raises(FetchError) as refused:
                await fetcher.get(COLLECTION_URL, params={"sheet": "2"})
        finally:
            await fetcher.aclose()
        assert refused.value.reason == "robots_disallowed"

    asyncio.run(scenario())

    assert [request.url.path for request in site.requests] == ["/robots.txt"]


def test_an_unreadable_robots_file_on_the_cdn_does_not_block_downloads() -> None:
    site = RecordedSite()

    async def scenario() -> bytes:
        fetcher = fetcher_for(site)
        try:
            page = parse_resolution_page(
                (FIXTURES / "6134934.html").read_text(),
                base_url="https://www.gob.pe/",
            )
            return (await fetcher.get(page.pdf_url)).content
        finally:
            await fetcher.aclose()

    assert asyncio.run(scenario()) == (FIXTURES / "6134934.pdf").read_bytes()
    assert [request.url.host for request in site.requests] == [
        "cdn.www.gob.pe",
        "cdn.www.gob.pe",
    ]


@pytest.mark.parametrize(
    ("status", "permanent"), [(404, True), (403, True), (429, False), (503, False)]
)
def test_failed_responses_say_whether_to_try_again(
    status: int, permanent: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status)

    async def scenario() -> FetchError:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = Fetcher(client, min_interval=0)
        try:
            with pytest.raises(FetchError) as failure:
                await fetcher.get("https://www.gob.pe/page")
        finally:
            await fetcher.aclose()
        return failure.value

    failure = asyncio.run(scenario())
    assert failure.reason == f"http_{status}"
    assert failure.permanent is permanent


def test_a_robots_503_blocks_the_host_until_the_retry_time_then_it_recovers() -> None:
    robots_answers = [503, 200]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            status = (
                robots_answers[0] if len(robots_answers) == 1 else robots_answers.pop(0)
            )
            return httpx.Response(status, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="page")

    clock = FakeClock()

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = Fetcher(client, min_interval=1.0, clock=clock, sleep=clock.sleep)
        try:
            with pytest.raises(FetchError) as blocked:
                await fetcher.get("https://www.gob.pe/page")
            # The failure is transient, so a job that hit it is retried.
            assert (blocked.value.reason, blocked.value.permanent) == (
                "robots_unreachable",
                False,
            )
            clock.now += 200
            with pytest.raises(FetchError, match="robots_unreachable"):
                await fetcher.get("https://www.gob.pe/page")
            assert requested == ["/robots.txt"]  # still within five minutes

            clock.now += 101
            assert (await fetcher.get("https://www.gob.pe/page")).text == "page"
            with pytest.raises(FetchError, match="robots_disallowed"):
                await fetcher.get("https://www.gob.pe/private")

            clock.now += 3000  # inside the hour a good robots.txt is kept
            await fetcher.get("https://www.gob.pe/page")
            assert requested.count("/robots.txt") == 2
            clock.now += 700
            await fetcher.get("https://www.gob.pe/page")
            assert requested.count("/robots.txt") == 3
        finally:
            await fetcher.aclose()

    asyncio.run(scenario())


def redirecting_site(
    redirects: dict[str, str], robots: dict[str, str]
) -> tuple[Callable[[httpx.Request], httpx.Response], list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots.get(request.url.host, ""))
        if str(request.url) in redirects:
            return httpx.Response(
                302, headers={"location": redirects[str(request.url)]}
            )
        return httpx.Response(200, text="page")

    return handler, requested


def test_a_redirect_to_a_disallowed_url_is_never_requested() -> None:
    handler, requested = redirecting_site(
        {"https://www.gob.pe/old": "https://other.example/files/a"},
        {"other.example": "User-agent: *\nDisallow: /files/\n"},
    )
    clock = FakeClock()

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = Fetcher(client, min_interval=1.0, clock=clock, sleep=clock.sleep)
        try:
            with pytest.raises(FetchError) as refused:
                await fetcher.get("https://www.gob.pe/old")
        finally:
            await fetcher.aclose()
        assert (refused.value.reason, refused.value.permanent) == (
            "robots_disallowed",
            True,
        )

    asyncio.run(scenario())

    assert requested == [
        "https://www.gob.pe/robots.txt",
        "https://www.gob.pe/old",
        "https://other.example/robots.txt",
    ]
    assert clock.sleeps == [1.0, 1.0]  # every request after the first waits a second


def test_an_allowed_redirect_is_followed_and_paced_like_any_request() -> None:
    handler, requested = redirecting_site(
        {
            "https://www.gob.pe/a": "/b",
            "https://www.gob.pe/b": "https://other.example/c",
        },
        {},
    )
    clock = FakeClock()

    async def scenario() -> str:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = Fetcher(client, min_interval=1.0, clock=clock, sleep=clock.sleep)
        try:
            return (await fetcher.get("https://www.gob.pe/a")).text
        finally:
            await fetcher.aclose()

    assert asyncio.run(scenario()) == "page"
    assert requested == [
        "https://www.gob.pe/robots.txt",
        "https://www.gob.pe/a",
        "https://www.gob.pe/b",
        "https://other.example/robots.txt",
        "https://other.example/c",
    ]
    assert clock.sleeps == [1.0] * 4  # every request after the first waits a second


def test_request_headers_stay_with_the_host_that_was_asked() -> None:
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen[str(request.url)] = request.headers
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if request.url.path == "/b":
            return httpx.Response(302, headers={"location": "https://other.example/c"})
        return httpx.Response(200, text="page")

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}
        )
        fetcher = Fetcher(client, min_interval=0)
        try:
            await fetcher.get("https://www.gob.pe/a", xhr=True)
        finally:
            await fetcher.aclose()

    asyncio.run(scenario())

    # A redirect inside the host keeps them, and one to another host drops them.
    assert seen["https://www.gob.pe/b"]["x-requested-with"] == "XMLHttpRequest"
    other = seen["https://other.example/c"]
    assert "x-requested-with" not in other
    assert "javascript" not in other["accept"]
    assert other["user-agent"] == USER_AGENT


def test_a_chain_of_more_than_five_redirects_is_refused() -> None:
    redirects = {f"https://www.gob.pe/{n}": f"/{n + 1}" for n in range(10)}
    handler, requested = redirecting_site(redirects, {})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = Fetcher(client, min_interval=0)
        try:
            with pytest.raises(FetchError) as refused:
                await fetcher.get("https://www.gob.pe/0")
        finally:
            await fetcher.aclose()
        assert (refused.value.reason, refused.value.permanent) == (
            "too_many_redirects",
            True,
        )

    asyncio.run(scenario())

    assert requested[-1] == "https://www.gob.pe/5"


def test_a_tie_between_allow_and_disallow_goes_to_allow() -> None:
    policy = RobotsPolicy("User-agent: *\nDisallow: /page\nAllow: /page\n")

    assert policy.allows("https://h/page")


@pytest.mark.parametrize("value", ["soon", "-5", "nan", "inf", "1e9", ""])
def test_a_malformed_crawl_delay_is_ignored(value: str) -> None:
    policy = RobotsPolicy(f"User-agent: *\nCrawl-delay: {value}\nDisallow: /x\n")

    assert policy.crawl_delay == 0
    assert not policy.allows("https://h/x")  # the rest of the file still applies
