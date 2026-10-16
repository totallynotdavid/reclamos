# Corpus

The corpus is the set of OSIPTEL TRASU resolutions (the Tribunal Administrativo
de Solución de Reclamos de Usuarios) that answers are drawn from. The code is in
`apps/api/src/reclamos_api/corpus/` and the command line is `reclamos`.

## Source

OSIPTEL publishes the resolutions in the "Jurisprudencia" collection on gob.pe:
<https://www.gob.pe/institucion/osiptel/colecciones/52616-jurisprudencia>. Each
entry is titled `Expediente N.° NNNNNNN-YYYY/TRASU/ST-RA` (an appeal) or
`ST-RQJ` (a complaint about the procedure), and links to a page that links the
PDF on `cdn.www.gob.pe`.

The crawler follows the robots rules of each host. gob.pe disallows `?sheet=`
pagination, so the crawler never pages. It asks the collection for one
publication-date window at a time with `filter[start_date]`, `filter[end_date]`
and `filter[per_page]=100`. When a window reports more entries than it shows, it
is split in two until it fits. A single day with more entries than one page
cannot be split further, so it is recorded in `listing_failures` with the reason
`listing_truncated`, and `corpus stats` lists it.

Requests are one per second per process, and each says who asks:

```text
reclamos-corpus/0.1 (+https://github.com/totallynotdavid/reclamos)
```

Redirects are followed by hand, up to 5. Each hop is checked against the
robots.txt of its own host and waits its own second. A chain longer than 5 fails
with `too_many_redirects`. Each host's robots.txt is cached for an hour, or for
five minutes when the host could not answer (5xx, 429 or unreachable), so a
transient failure is retried and fails the request as `robots_unreachable`. When
an Allow and a Disallow rule match equally, Allow wins. A `Crawl-delay` that is
not a plain number is ignored.

## Pipeline

Each step is an `rqueue` job, and each job queues the next:

1. `corpus.discover` lists a window and records each entry.
2. `corpus.download` reads the resolution page, then stores the PDF under
   `resolutions/<id>.pdf` with its SHA-256 and size.
3. `corpus.extract` reads the text layer of each page and runs OCR on pages that
   have none. Resolutions published as scans are the common case.
4. `corpus.parse` reads the number, date, operator, claim type and outcome from
   the text and the page summary.

The `resolutions` row in `schema.sql` records which steps are finished. A job
checks its step first, so a re-run does nothing for finished work.

A step that cannot finish writes `failure_stage`, `failure_reason` and
`failure_detail` on the row, and the row stays in the table. A re-run tries
failed rows again, and parsing again needs no network, so a better parser can be
applied to stored text by running `reclamos corpus crawl`.

The jobs run in whichever process claims them: the worker
(`python -m reclamos_api.worker`) or `reclamos corpus crawl`. Only a crawl
starts the retry of a failure. [Architecture](architecture.md#corpus-state)
lists the states of a resolution, of a listing window and of a job, who changes
each, and who clears a failure.

### What parsing reads

| Field        | Values                                                                                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `number`     | `NNNNNNN-YYYY-TRASU/OSIPTEL`                                                                                                                                                    |
| `operator`   | One of the known operators in `parse.py`, else the name as printed                                                                                                              |
| `claim_type` | `unrequested_contract`, `porting`, `service_cut`, `service_quality`, `billing`, `equipment`, `contract_terms`, `procedural` (complaints, and resolutions that only void a step) |
| `outcome`    | `fundado`, `infundado`, `improcedente`, `fundado_en_parte`, `mixto`, `sin_efecto`                                                                                               |

Telefónica del Perú S.A.A. was renamed Integratel Perú S.A.A. in 2025. They are
kept as separate operators because the resolutions print each name.

The scan can damage any label, so two fields have a second source. The
`Empresa:` line on the resolution page, when present, names the operator. The
page summary gives the outcome when the scan lost the outcome label. A
resolution that rules differently on several claims is `mixto` even when the
summary names one ruling. An operator name that is neither a known operator nor
ends in a legal form (`S.A.`, `S.A.C.`, ...) is a misread box, so it is not
stored.

When a field is missing, the row fails at `parse` with the reason
`no_<first missing field>` and the full list in `failure_detail`.

## Commands

Run these from `apps/api` with PostgreSQL available (see
[operations](operations.md)):

```console
uv run reclamos corpus crawl --since 2020-01-01
uv run reclamos corpus stats
```

`crawl` runs the jobs in this process until the queue is empty, then prints the
same statistics as `stats`. `stats` prints the resolutions found, downloaded,
extracted, parsed and failed, the failures by stage and reason, and the counts
of claim types and outcomes.

## Configuration

| Variable          | Default           | Meaning                               |
| ----------------- | ----------------- | ------------------------------------- |
| `DATABASE_URL`    | built from `DB_*` | PostgreSQL connection string          |
| `PDF_STORE`       | `local`           | `local` or `s3`                       |
| `PDF_STORE_PATH`  | `.data/pdfs`      | Directory for the `local` store       |
| `PDF_BUCKET`      |                   | Bucket for the `s3` store             |
| `S3_ENDPOINT_URL` |                   | Endpoint for an S3-compatible service |

The `s3` store uses the standard AWS credential chain. The worker started by
`python -m reclamos_api.worker` runs the same jobs, so a deployed worker can
continue a crawl that `reclamos corpus crawl` began. The one-request-per-second
limit is per process: do not run both at once.

## OCR

OCR needs `pdftoppm` from poppler-utils and `tesseract` with the Spanish
language data. On Debian:

```console
sudo apt-get install poppler-utils tesseract-ocr tesseract-ocr-spa
```

The Docker image installs them. Extraction fails a scan with the reason
`tool_missing` when they are absent, and the tests that need them skip.

## Tests

The tests replay recorded gob.pe pages and PDFs from `apps/api/tests/fixtures`
and make no network request. The corpus tests need PostgreSQL: set
`TEST_DATABASE_URL` to a server, or run Docker so the tests start `postgres:18`.
Each test creates and drops its own database.
