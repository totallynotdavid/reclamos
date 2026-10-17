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
scratch database on that server, loads
`packages/eval/data/gate/subset.jsonl.gz` into it, searches the gate's
questions with the stored query vectors, drops the database and compares the
result with `packages/eval/data/gate/baseline.json`. `mise run eval:gate` runs
it, and the `gate` job of the API workflow runs that.

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

## Results

Run on 2026-10-17 (UTC) at commit `00acc6f` plus the evaluation code of this
change, with `intfloat/multilingual-e5-large` on a 4-core CPU. The database held
290 resolutions, of which 289 have text and are indexed, in 4419 chunks. The
questions are 116 answerable and 12 that the corpus cannot answer.

```text
128 questions: 116 answerable (103 tune, 25 held_out), 12 unanswerable (not scored)

                                mode     recall@5 recall@10    MRR  nDCG@10
all (116)                       vector      0.647     0.855  0.560    0.604
                                text        0.569     0.695  0.497    0.510
                                hybrid      0.727     0.855  0.628    0.661
split tune (93)                 vector      0.624     0.873  0.548    0.600
                                text        0.538     0.679  0.489    0.496
                                hybrid      0.735     0.857  0.619    0.653
split held_out (23)             vector      0.739     0.783  0.610    0.618
                                text        0.696     0.761  0.527    0.570
                                hybrid      0.696     0.848  0.666    0.694
claim billing (63)              vector      0.656     0.831  0.552    0.596
                                text        0.579     0.656  0.447    0.468
                                hybrid      0.735     0.862  0.611    0.657
claim contract_terms (6)        vector      0.806     0.861  0.833    0.747
                                text        0.778     1.000  0.778    0.786
                                hybrid      0.806     0.889  0.833    0.760
claim equipment (2)             vector      0.500     1.000  0.562    0.658
                                text        0.000     0.000  0.073    0.000
                                hybrid      1.000     1.000  0.500    0.631
claim porting (2)               vector      0.583     1.000  0.600    0.677
                                text        0.333     0.333  0.516    0.336
                                hybrid      0.333     0.583  0.562    0.449
claim procedural (15)           vector      0.656     0.822  0.574    0.609
                                text        0.589     0.722  0.613    0.592
                                hybrid      0.656     0.878  0.602    0.648
claim service_cut (15)          vector      0.589     0.911  0.464    0.548
                                text        0.567     0.911  0.537    0.603
                                hybrid      0.911     0.911  0.750    0.765
claim service_quality (9)       vector      0.611     0.963  0.548    0.628
                                text        0.648     0.741  0.583    0.583
                                hybrid      0.667     0.889  0.633    0.684
claim unrequested_contract (4)  vector      0.625     0.750  0.599    0.588
                                text        0.250     0.375  0.278    0.296
                                hybrid      0.250     0.375  0.315    0.298

search latency ms               mode          p50       p95
                                vector      318.9     679.1
                                text         73.9     129.0
                                hybrid      395.1     796.6
```

Fused search beats both single rankers on all answerable questions at recall@5
(0.727 against 0.647 for vector and 0.569 for text), MRR and nDCG@10. At
recall@10 it ties vector (0.855) and trails it on the tune split (0.857 against
0.873).

**Fused does not beat both single rankers on the held-out split.** At recall@5
it ties text (0.696) and trails vector (0.739). It leads on recall@10 (0.848
against 0.783 and 0.761), MRR (0.666 against 0.610 and 0.527) and nDCG@10 (0.694
against 0.618 and 0.570). The split has 23 questions, so the recall@5 gap is one
question. Where fused loses at recall@5:

- `q054` asks how a company proves a subscriber accepted premium-number calls.
  Vector ranks the right resolution 4th and text does not return it. Fused ranks
  it 9th, behind four resolutions that both rankers return and four that only
  one does.
- `q104` asks about a Speedy rent charged during outages. Vector ranks the right
  resolution 4th and text 2nd. Fused ranks it 6th, behind three resolutions that
  both rankers return and two that vector ranks 1st and 2nd.
- `q062` has two right resolutions. Text returns both in its first five and
  fused returns one.

Fused wins `q016`, where vector ranks the right resolution 11th and text 2nd.
Reciprocal rank fusion favors resolutions that both rankers return, so a
resolution that one ranker puts near the top can fall below several that both
return at lower ranks.

By claim type, fused trails vector at recall@5 on `porting` (2 questions) and
`unrequested_contract` (4 questions), where text finds little; both groups are
too small to read more into. Nothing was changed in response to these numbers.

Latency is the median and 95th percentile of one search on a CPU, where
embedding the question takes most of the time; `text` needs no embedding.
