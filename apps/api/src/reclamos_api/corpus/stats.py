from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CorpusStats:
    found: int
    downloaded: int
    extracted: int
    extraction_methods: dict[str, int]
    parsed: int
    failures: dict[tuple[str, str], int]
    claim_types: dict[str, int]
    outcomes: dict[str, int]
    failed_jobs: dict[str, int]
    listing_failures: dict[str, int]

    @property
    def failed(self) -> int:
        return sum(self.failures.values())


async def collect_stats(pool: Any, *, queue_schema: str) -> CorpusStats:
    async with pool.acquire() as connection:
        totals = await connection.fetchrow(
            """
            SELECT count(*) AS found,
                   count(downloaded_at) AS downloaded,
                   count(extracted_at) AS extracted,
                   count(parsed_at) AS parsed
            FROM resolutions
            """
        )
        methods = await connection.fetch(
            """
            SELECT text_method, count(*) FROM resolutions
            WHERE text_method IS NOT NULL GROUP BY 1
            """
        )
        failures = await connection.fetch(
            """
            SELECT failure_stage, failure_reason, count(*) FROM resolutions
            WHERE failure_reason IS NOT NULL GROUP BY 1, 2
            """
        )
        claim_types = await connection.fetch(
            "SELECT claim_type, count(*) FROM resolutions WHERE parsed_at IS NOT NULL GROUP BY 1"
        )
        outcomes = await connection.fetch(
            "SELECT outcome, count(*) FROM resolutions WHERE parsed_at IS NOT NULL GROUP BY 1"
        )
        # The queue schema name is validated by rqueue before it is installed.
        failed_jobs = await connection.fetch(
            f"SELECT task, count(*) FROM {queue_schema}.jobs "
            "WHERE state = 'failed' AND task LIKE 'corpus.%' GROUP BY 1"
        )
        listing_failures = await connection.fetch(
            "SELECT reason, count(*) FROM listing_failures GROUP BY 1"
        )
    return CorpusStats(
        found=totals["found"],
        downloaded=totals["downloaded"],
        extracted=totals["extracted"],
        extraction_methods={row[0]: row[1] for row in methods},
        parsed=totals["parsed"],
        failures={(row[0], row[1]): row[2] for row in failures},
        claim_types={row[0]: row[1] for row in claim_types},
        outcomes={row[0]: row[1] for row in outcomes},
        failed_jobs={row[0]: row[1] for row in failed_jobs},
        listing_failures={row[0]: row[1] for row in listing_failures},
    )


def format_stats(stats: CorpusStats) -> str:
    lines = [
        f"resolutions found       {stats.found}",
        f"downloaded              {stats.downloaded}",
        f"extracted               {stats.extracted}"
        + _breakdown(stats.extraction_methods),
        f"parsed                  {stats.parsed}",
        f"failed                  {stats.failed}",
    ]
    for (stage, reason), count in sorted(stats.failures.items()):
        lines.append(f"  {stage}: {reason}".ljust(26) + str(count))
    lines.append("claim types")
    lines.extend(_counts(stats.claim_types))
    lines.append("outcomes")
    lines.extend(_counts(stats.outcomes))
    if stats.listing_failures:
        lines.append("listing windows that could not be read")
        lines.extend(_counts(stats.listing_failures))
    if stats.failed_jobs:
        lines.append("queue jobs that exhausted their attempts")
        lines.extend(_counts(stats.failed_jobs))
    return "\n".join(lines)


def _breakdown(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    return (
        "  ("
        + ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        + ")"
    )


def _counts(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["  none"]
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"  {name}".ljust(26) + str(count) for name, count in ordered]
