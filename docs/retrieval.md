# Retrieval

`POST /search` ranks passages of the [corpus](corpus.md) against a question. The
code is in `apps/api/src/reclamos_api/retrieval/` and
`apps/api/src/reclamos_api/corpus/chunk.py`.

## Chunk model

A resolution's stored text (`resolutions.body`, pages joined by form feeds) is
split into passages by `chunk_text` in `chunk.py`. A resolution is a header
block, then sections introduced by a heading, each made of paragraphs, most of
them numbered. The splitter follows that structure:

- A heading (`VISTO`, `ANTECEDENTES`, `ANÁLISIS DEL CASO`, `FUNDAMENTOS`,
  `HA RESUELTO`, ...) starts a section, and a chunk never crosses one. Scans
  misread headings, so the match tolerates stray marks before the words and a
  lower-case `l` for a capital `L`. Text before the first heading is the
  `encabezado` section. Before 2020 `VISTO` leads straight into numbered
  paragraphs, which are filed under `fundamentos`.
- A blank line or a paragraph number (`1.`, `2)`) starts a paragraph. Chunks are
  packed with whole paragraphs up to 1200 characters.
- A paragraph over the cap is cut at sentence ends, then at the last space
  inside the cap.
- Each chunk after the first in a section opens at a sentence of the previous
  chunk, at most 200 characters before its end, so a sentence cut by the cap is
  whole in one of the two.

Each row of `chunks` in `schema.sql` holds:

| Column                     | Meaning                                                 |
| -------------------------- | ------------------------------------------------------- |
| `id`                       | Primary key; new chunks of a resolution get new ids     |
| `resolution_id`, `ordinal` | Unique; ordinals count from 0 in text order             |
| `section`                  | `encabezado`, `visto`, `antecedentes`, `analisis`, ...  |
| `page_start`, `page_end`   | 1-based pages the chunk touches                         |
| `char_start`, `char_end`   | Offsets into `resolutions.body`; `body[start:end]=text` |
| `text`                     | The passage                                             |
| `search`                   | Generated `tsvector` of `text`                          |
| `embedding`                | `vector(1024)`, null until the resolution is embedded   |

A caller quotes a passage by reading `char_start` and `char_end` from the stored
body, and checks it against `text`.

## Indexes

- Full text: `search` is `to_tsvector('spanish_unaccent', text)` with a GIN
  index. `spanish_unaccent` is the `spanish` configuration with `unaccent`
  placed before the stemmer, so `facturación` and `facturacion` give the same
  term. OCR and users drop accents.
- Vectors: `embedding` has an HNSW index on cosine distance
  (`vector_cosine_ops`). A filtered query sets `hnsw.iterative_scan` to
  `strict_order`, so a filter that removes most of the index still returns the
  candidates it should.
- `resolutions_filter_idx` covers `(claim_type, outcome, resolved_on)`.

## Embedding model

`intfloat/multilingual-e5-large` (1024 dimensions) runs in this process through
fastembed (ONNX). It needs no API key and no network call once downloaded. The
model is about 2.2 GB on disk and holds about 2.5 GB of memory while embedding.
The files are cached under `EMBEDDING_CACHE_PATH`, and the Docker image
downloads them at build time. E5 models were trained with a `query: ` marker on
questions and a `passage: ` marker on passages, which `embedder.py` adds.

The dimension is fixed in two places that change together: `DIMENSION` in
`embedder.py` and `vector(1024)` in `schema.sql`. Another model needs both
changed, an entry in `embedder.py` for its markers, and a new database, because
`schema.sql` does not alter an existing column.

| Variable               | Default                          | Meaning                    |
| ---------------------- | -------------------------------- | -------------------------- |
| `EMBEDDING_MODEL`      | `intfloat/multilingual-e5-large` | Model name                 |
| `EMBEDDING_CACHE_PATH` | `.data/models`                   | Where model files are kept |

## Search

`POST /search` takes a JSON body:

| Field                   | Rule                                                                              |
| ----------------------- | --------------------------------------------------------------------------------- |
| `query`                 | Required, 1 to 500 characters after trimming                                      |
| `k`                     | 1 to 50, default 10                                                               |
| `mode`                  | `hybrid` (default), `vector` or `text`                                            |
| `claim_type`, `outcome` | One of the values listed under [What parsing reads](corpus.md#what-parsing-reads) |
| `operator`              | Exact operator name as stored                                                     |
| `date_from`, `date_to`  | ISO dates, inclusive, on the resolution date; `from` not after `to`               |

A `vector` or `hybrid` search answers 503 when `EMBEDDING_MODEL` is not the
model that made the stored vectors; the reason names both models. `text` does
not embed and keeps answering. An unknown field or a value out of range
answers 422. A filter on a parsed field leaves out the resolutions whose parsing
failed, because their field is null.

The response is `{query, mode, results}`. Each result carries the resolution
(`resolution_id`, `expediente`, `number`, `date`, `operator`, `claim_type`,
`outcome`, `url`) and the passage (`ordinal`, `section`, `page_start`,
`page_end`, `char_start`, `char_end`, `text`), and three scores: `score`, the
fused score that orders the list, and `vector_score` and `text_score`, which are
null for a ranker that did not return the chunk.

## Fusion

Each ranker returns its best 100 chunks. Reciprocal rank fusion scores a chunk
by the sum of `1 / (60 + rank)` over the rankers that returned it, so only the
order of each list counts and the cosine similarity and `ts_rank` scales never
mix. Equal scores order by resolution id, then ordinal.

- The vector ranker orders by cosine distance to the embedded query.
  `vector_score` is the cosine similarity.
- The text ranker turns the question into a `tsquery` with `plainto_tsquery` and
  joins its terms with `|`, because a question is not a list of required words.
  `text_score` is `ts_rank`.

`mode=vector` and `mode=text` return one ranker alone, for comparison.

## Re-indexing

`reclamos corpus crawl` chunks and embeds every resolution that is not finished,
and skips each completed step. To redo work:

```console
uv run reclamos corpus reindex chunks
uv run reclamos corpus reindex embeddings
```

`chunks` deletes the chunks and runs the chunker and the embedder again, so use
it after a change to `chunk.py`. `embeddings` clears the vectors and runs the
embedder again, so use it after a change of model. Both read the stored text.
Like `crawl`, they also queue the next stage of every unfinished resolution, so
a resolution that was never downloaded gets a download job, and gob.pe is
requested for it. The deployed worker can keep running; the shared-state rules
are in [Architecture](architecture.md#chunks-and-vectors). Search returns fewer
results, or none, until the re-index has finished.

## Evaluation

[Evaluation](evaluation.md) scores the three modes on judged questions and gates
CI on fused recall@5.
