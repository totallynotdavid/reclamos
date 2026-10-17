# Evaluation

Retrieval is judged on questions a subscriber might ask, each with the
resolutions that answer it. `reclamos eval` searches every question against the
database and prints how often the right resolutions come back. A smaller check,
the gate, runs in CI on every change. The code is in
`apps/api/src/reclamos_api/evaluation/` and `packages/eval/`.

## Questions

`packages/eval/data/queries.jsonl` holds one JSON object per line:

```json
{
  "id": "q001",
  "query": "...",
  "split": "tune",
  "answerable": true,
  "relevant": [6134951, 6134992],
  "evidence": [
    { "resolution_id": 6134951, "quote": "..." },
    { "resolution_id": 6134992, "quote": "..." }
  ]
}
```

- `query` is Spanish, in a subscriber's plain words, with no resolution number.
  Some carry typos or lack accents, as a phone keyboard or an OCR copy would.
- `relevant` lists every resolution that answers the question. The first one
  sets the question's claim type.
- `evidence` holds one verbatim quote from each relevant resolution. A quote
  proves the resolution says it, and it picks the chunk the gate keeps.
  Questions name resolutions, not chunks, because chunk ids change whenever the
  text is chunked again.
- `split` is `tune` or `held_out`. The file records the split, not the line
  position. About a fifth of the questions are held out, spread over the claim
  types. Chunking, fusion weights and the model are tuned on `tune` only. Never
  move a question between splits after seeing a result.
- A question the corpus cannot answer has `"answerable": false`, no `relevant`
  and no `evidence`, and a `note` that says why. These are not scored; they are
  kept to set a refusal threshold later.

The set covers every claim type and outcome in proportion to how often the
corpus has them.

### Add a question

1. Read a resolution and write the question its subscriber would ask, without
   copying its words.
2. Copy a sentence that answers it into `evidence`, exactly as it appears in the
   text the database stores. Runs of whitespace may differ. Add the other
   resolutions that answer it, each with a quote.
3. Give it the next free `id` and the split `tune`.
4. Run `reclamos eval`. It reads the whole file first and stops with the line
   and id of a question that is malformed, names a resolution the database lacks
   or quotes text the resolution does not contain.

If the change adds a held-out question, take it from questions nobody has tuned
against, and say so in the pull request.

## Run the evaluation

The database must hold the crawled, chunked and embedded corpus (see
[corpus](corpus.md)) and `DATABASE_URL` must point at it. The embedding model
loads on the first search, so the first run downloads it (see
[retrieval](retrieval.md)).

```console
uv run reclamos eval
```

Options: `--questions PATH` for another question file and `--output PATH` for
where the JSON goes (default `.data/eval.json`).

The command prints one table and writes the same numbers as JSON. The JSON also
has the commit and whether files differed from it, the model name, the corpus
counts, and for each question the first ten resolutions each mode returned.

## Read the numbers

Each search runs in three modes: `vector`, `text` and `hybrid`. `hybrid` is the
reciprocal rank fusion of the other two, the mode `POST /search` uses by
default. The table gives each mode's scores for all answerable questions, per
split and per claim type, with the number of questions in parentheses.

A search returns chunks. They are folded into distinct resolutions in the order
of each resolution's best chunk, and a question counts a resolution as found
when it is relevant.

- `recall@5` and `recall@10` are the share of a question's relevant resolutions
  among the first 5 or 10 resolutions, averaged over questions.
- `MRR` is the mean of 1 / rank of the first relevant resolution, or 0 when none
  is returned.
- `nDCG@10` rewards relevant resolutions that rank higher, with every relevant
  resolution worth the same.

The last block gives the median (p50) and 95th percentile (p95) time of one
search in milliseconds, including the query embedding, after the model is
loaded. Scores of groups with few questions move by large steps: with 23
questions, one question is 4.3 points. Compare the held-out rows only for
differences larger than that.

## Gate

`reclamos gate check` fails when fused recall@5 on a fixed index falls more than
2 points below the committed baseline. It downloads no model and needs only a
PostgreSQL server with pgvector.

```console
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  uv run reclamos gate check
```

The role in the URL must be allowed to create databases. The command creates a
scratch database on that server, loads `packages/eval/data/gate/subset.jsonl.gz`
into it, searches the gate's questions with the stored query vectors, drops the
database and compares the result with `packages/eval/data/gate/baseline.json`.
`mise run test:gate` runs it, and the `integration` job of the CI workflow runs
that.

The fixed index holds the chunks that answer the gate's questions, the chunks
each single ranker prefers instead, and filler chunks picked by a hash, with
their vectors. The gate asks the `tune` questions only, so no change can pass
the gate by fitting the held-out split.

### Move the baseline

The gate never writes the baseline. After a change that moves fused recall@5 on
purpose, measure and commit the new value:

```console
uv run reclamos gate baseline
```

Review `git diff packages/eval/data/gate/baseline.json` and commit it in the
same pull request as the change that moved it. Do the same after editing the
`tune` questions or anything else that changes what the fixed index returns.

### Rebuild the fixed index

Questions added to the `tune` split are not in the fixed index until it is
rebuilt from the corpus database. This needs the model:

```console
uv run reclamos gate subset
uv run reclamos gate baseline
```

`--chunks N` sets how many chunks to keep (default 800). The answer chunks
always stay, so the index can be larger. Commit both files.

The subset is gzipped JSON Lines: one `meta` line with the model name and the
ids of the questions the gate asks, then `resolution`, `chunk` and `query`
lines. A `chunk` line holds the stored columns of a passage and its vector. A
`query` line holds the text of a question and its vector, so the gate searches
without loading the model. A vector is little-endian half-precision floats in
base64. The gzip header carries no timestamp, so the file does not change when
its content does not.
