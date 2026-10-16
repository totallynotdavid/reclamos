# Architecture

Reclamos has four deployable concerns:

```text
browser -> apps/web -> apps/api -> PostgreSQL + pgvector
                              \-> rqueue jobs -> apps/api worker
packages/eval -> offline answer metrics
infra -> AWS ECS, RDS, S3, ECR, IAM
```

`apps/api/src/reclamos_api/main.py` creates the FastAPI application and owns the
`/health` route. `db.py` owns the asyncpg startup boundary, and `health.py`
checks PostgreSQL and the rqueue jobs table separately.

`apps/api/src/reclamos_api/worker.py` registers the worker process. It shares
the API package for the same database and queue configuration. It uses rqueue's
PostgreSQL leases and at-least-once delivery model. Before registering, it polls
for the API-owned queue schema once per second for up to 30 attempts, then exits
with an error if the schema is still unavailable.

`apps/web/src/App.tsx` is the Spanish placeholder page. Vite builds it as a
static client. `apps/web/vercel.json` owns the Vercel change filter for this
workspace.

`packages/eval/src/reclamos_eval/metrics.py` owns offline exact-match metrics.
It has no runtime dependency on the API or web app, so evaluation can run in a
clean Python environment.

`infra/` defines the AWS deployment: an HTTPS application load balancer, ECS
Fargate task definitions and services for the API and worker, PostgreSQL RDS, an
S3 PDF bucket, ECR, and the IAM roles those services need. Terraform uses an S3
backend with native S3 locking; applying it is an operator action.
