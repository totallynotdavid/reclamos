# Working in reclamos

Use the pinned tools from `mise.toml`. Run `mise run check` before handing off
changes. Product text belongs in Spanish; repository documentation is English.

The API owns database access and queue startup. The web app talks to the API
through its configured origin. The evaluation package contains metrics that are
independent of the API and web runtimes. See
[docs/architecture.md](docs/architecture.md) for the code map.
