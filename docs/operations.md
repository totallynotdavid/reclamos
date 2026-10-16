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

The database is disposable. The API applies its one application schema file on
startup and owns rqueue schema setup. There are no project migrations. The
worker waits for that queue schema for up to 30 seconds before exiting if the
API has not made it ready.

## Checks

CI runs the same commands through `mise`: API lint, typecheck, and test; eval
lint, typecheck, and test; web lint, typecheck, test, and build; and Terraform
format, initialization without the remote backend, validation, and tflint.
Workflow path filters keep API, web, and infrastructure changes from running
unrelated jobs.

## Web deploy

The web app is configured for Vercel in `apps/web`. Link the project from that
directory with the already authenticated Vercel CLI:

```console
cd apps/web
vercel link
vercel deploy --prod
```

The linked project metadata is local and ignored by Git.
