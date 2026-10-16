# Architecture

Reclamos has four deployable concerns:

```text
browser -> apps/web -> apps/api -> PostgreSQL
                              \-> rqueue jobs -> apps/api worker -> gob.pe
                                                      \-> PDF store (disk or S3)
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

`apps/api/src/reclamos_api/corpus/` builds the resolution corpus: `site.py`
crawls gob.pe, `store.py` holds PDFs behind one interface, `extract.py` reads
text and runs OCR, `parse.py` reads each resolution, `jobs.py` runs those steps
as rqueue jobs, and `stats.py` summarizes the result. `cli.py` exposes it as the
`reclamos` command. [Corpus](corpus.md) describes the pipeline.

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

## Corpus state

Three tables of the database hold shared state: `resolutions` (one row per
resolution), `listing_failures` (one row per listing window that could not be
read) and the rqueue `jobs` table. The jobs run in whichever process claims
them, the worker (`python -m reclamos_api.worker`) or `reclamos corpus crawl`,
so a row can be written by either. Jobs are queued by two kinds of actor:

- The CLI starts work from the database. `crawl` queues a `corpus.discover` job
  for its date range and one job for the next stage of every unfinished
  resolution. Nothing else reads the database to decide what to queue, so the
  worker never starts a crawl by itself.
- A running job queues what follows it, in whichever process runs it, worker or
  CLI. `corpus.discover` queues the first missing stage of each resolution it
  lists, or two smaller `corpus.discover` windows when the window is too big.
  `corpus.download` and `corpus.extract` queue the next stage.

### Resolution

A resolution has one state, the last stage it finished, and may also carry a
failure. The columns that define each state are in `schema.sql`.

| State      | Holds when                                     | Entered from                                               | Actor that makes the transition                            |
| ---------- | ---------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- |
| discovered | the row exists and `downloaded_at` is null     | nothing: `discover` inserts                                | `corpus.discover`, run by the worker or the CLI            |
| downloaded | `downloaded_at` is set and `extracted_at` null | discovered                                                 | `corpus.download`, run by the worker or the CLI            |
| extracted  | `extracted_at` is set and `parsed_at` is null  | downloaded                                                 | `corpus.extract`, run by the worker or the CLI             |
| parsed     | `parsed_at` is set                             | extracted                                                  | `corpus.parse`, run by the worker or the CLI               |
| failed     | `failure_stage` is set                         | discovered, downloaded or extracted (any state but parsed) | the job of the failing stage, run by the worker or the CLI |

Rules for these transitions:

- A state is never undone. A stage that finishes sets its timestamp, and nothing
  clears a timestamp, so a resolution only moves forward.
- `failed` is a flag on the row, not a place after `parsed`. The row keeps the
  state of the last stage it finished, and `failure_stage` names the stage that
  could not finish: `download` on a discovered row, `extract` on a downloaded
  row, `parse` on an extracted row.
- A job records `failed` only when it cannot succeed: the error is permanent, or
  it is transient and the job is on its last attempt. A transient error with
  attempts left is retried by rqueue and leaves the row as it was.
- A `parse` that finds the text but not every field stores what it found, leaves
  `parsed_at` null and records `failed`.
- A failure is cleared by the success of the stage that failed, and only a
  `crawl` starts that retry: it queues the stage again, and the job that runs
  it, in the worker or the CLI, clears the failure columns when it finishes. No
  other code path clears them. A failed row is not queued again by itself.
- A job that finds its stage already finished does nothing except queue the next
  stage, so a re-run skips finished work.

### Listing failure

| State  | Holds when                    | Actor that makes the transition                                                                   |
| ------ | ----------------------------- | ------------------------------------------------------------------------------------------------- |
| absent | the window has no row         | the initial state. `corpus.discover` clears a row in a window it lists in full, in either process |
| failed | a row exists for a single day | `corpus.discover`, in the worker or the CLI, when that day has more entries than a page shows     |

The only reason is `listing_truncated`. The job ends normally after recording
it, because a retry would show the same page. A failed window is cleared by a
later `corpus.discover` that lists a window containing the day in full, in
either process. Only a `crawl` queues such a job for a date range, so only a
`crawl` starts the clearing.

### Job

rqueue owns the states of a job. Reclamos sets none of them itself: it queues
jobs and returns from handlers, and rqueue does the rest.

| State     | Holds when                                               | Entered from                                  | Actor that makes the transition                                                                                                                                  |
| --------- | -------------------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| pending   | the job waits to be claimed                              | nothing, leased, or a failed or cancelled job | the CLI or a running job queues it; rqueue returns a leased job to pending to retry it; an rqueue operator retries a finished job                                |
| leased    | one process holds the job                                | pending                                       | the worker or the CLI, whichever claims it                                                                                                                       |
| succeeded | the handler returned                                     | leased                                        | rqueue, in the process that ran it                                                                                                                               |
| failed    | the attempts are used up, or the error cannot be retried | leased                                        | rqueue, in the process that ran it. Causes: attempts used up, a handler error not retried, an unknown task name, an undecodable payload, an invalid retry policy |
| cancelled | an operator cancelled the job                            | pending, or leased                            | an rqueue operator (`cancel`); a leased job is cancelled by the process that holds it, or when its lease expires                                                 |

Reclamos code never cancels or retries a job, and nothing in it clears a
`failed` one. An rqueue operator can: `retry` moves a `failed` or `cancelled`
job back to `pending` with more attempts. Without that, the resolution that the
job served is retried by the next `crawl`, which queues a new job for it.
`corpus stats` counts the failed jobs.

The dedupe key of a job is `corpus.<stage>:<resolution id>` for a stage and
`corpus.discover:<start>:<end>` for a window. Queueing a key returns the job
that already holds it. Only a `pending` or `leased` job holds its key, so a
finished job, `succeeded` or `failed`, does not block a later `crawl` from
queueing the same stage or window again. That is how a crawl retries a failure
and re-lists a window, and a second `crawl` while the first still runs queues
nothing twice.
