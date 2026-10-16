# reclamos

Reclamos answers Spanish telecom questions from public OSIPTEL TRASU
resolutions. It cites the quoted passage behind each answer and refuses when the
corpus does not support one. It is not legal advice.

Install the pinned tools with [mise](https://mise.jdx.dev/), then install the
workspace dependencies:

```console
mise install
mise run install
```

Start the complete local stack:

```console
docker compose up --build
```

Run `bun run --cwd apps/web dev` for the Spanish placeholder web page at
<http://localhost:5173>. The API health check is available at
<http://localhost:8000/health>.

The repository contains the FastAPI service in `apps/api`, the React web app in
`apps/web`, the evaluation package in `packages/eval`, and AWS Terraform in
`infra`. The measurement package exposes the shared exact-match accuracy metric.

Run the same checks as CI with:

```console
mise run check
```

The [manual](docs/readme.md) indexes the architecture and operations guides.
Agent rules live in [AGENTS.md](AGENTS.md).
