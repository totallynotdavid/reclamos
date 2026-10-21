# Operations

## Local development

Install the pinned toolchain and dependencies:

```console
mise install
mise run install
```

Run the quality gate:

```console
mise run check
```

Start PostgreSQL, the API, and the worker:

```console
docker compose up --build
```

The API listens on port 8000 and the web app runs separately with
`bun run --cwd apps/web dev` on port 5173. The health response reports
`database.ok` and `queue.ok`; both must be true for a healthy stack.

[`.env.example`](../.env.example) lists every environment variable the API, the
worker and the tests read.

The database is disposable. The API applies its one application schema file on
startup and owns rqueue schema setup. There are no project migrations: when
`schema.sql` changes, recreate the database with `docker compose down -v`. The
worker waits for that queue schema for up to 30 seconds before exiting if the
API has not made it ready. The worker keeps PDFs in the `pdfs` volume; the
deployed worker keeps them in the S3 bucket.

## Checks

`mise run check` never writes files. It runs these tasks, and CI runs it:

| Task        | Runs                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------- |
| `lint`      | Ruff, oxlint with `--deny-warnings`, an oxfmt check of every supported file, Terraform and tflint |
| `typecheck` | basedpyright in strict mode, `tsc -b`                                                             |
| `test`      | pytest without the `integration` marker, vitest                                                   |
| `build`     | the `reclamos-api` and `reclamos-eval` distributions, the web bundle                              |

The `check` job of the CI workflow runs `mise run check` on every pull request.
Each area has its own tasks, such as `lint:py`, `typecheck:web` and `build:py`.
`mise tasks` lists them.

`mise run fix` applies the safe Ruff and oxlint fixes and formats Python,
Terraform, Markdown, YAML, JSON, CSS, HTML and TypeScript.

The integration tests need a PostgreSQL server with the pgvector and unaccent
extensions. `mise run test:integration` starts `pgvector/pgvector:pg18` with
Docker, or uses the server in `TEST_DATABASE_URL`, and skips when neither
exists. Tests of OCR skip without `pdftoppm` and `tesseract` (see
[corpus](corpus.md)).

The `integration` job of the CI workflow runs, when Python code changes,
`mise run test:integration` and the retrieval gate (`mise run test:gate`)
against a `pgvector/pgvector:pg18` service. The gate fails when fused recall@5
on the fixed index drops more than 2 points below the committed baseline; see
[evaluation](evaluation.md#gate).

## Web deploy

The web app is configured for Vercel in `apps/web`. Link the project from that
directory with the already authenticated Vercel CLI:

```console
cd apps/web
vercel link
vercel deploy --prod
```

The linked project metadata is local and ignored by Git.
