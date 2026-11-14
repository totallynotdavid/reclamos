CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Spanish stemming over text with the accents removed, because scans and users
-- drop them. Documents and queries both go through it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'spanish_unaccent') THEN
        CREATE TEXT SEARCH CONFIGURATION spanish_unaccent (COPY = spanish);
        ALTER TEXT SEARCH CONFIGURATION spanish_unaccent
            ALTER MAPPING FOR asciiword, word, numword, asciihword, hword, numhword,
                              hword_asciipart, hword_part, hword_numpart
            WITH unaccent, spanish_stem;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS resolutions (
    id bigint PRIMARY KEY,
    page_url text NOT NULL,
    expediente text NOT NULL,
    published_on date NOT NULL,
    discovered_at timestamptz NOT NULL DEFAULT now(),

    summary text,
    pdf_url text,
    pdf_key text,
    pdf_sha256 text,
    pdf_bytes integer,
    downloaded_at timestamptz,

    body text,
    text_method text CHECK (text_method IN ('text-layer', 'ocr', 'mixed')),
    page_count integer,
    ocr_pages integer,
    extracted_at timestamptz,

    number text,
    resolved_on date,
    operator text,
    subject text,
    claim_type text,
    appeal_kind text,
    outcome text,
    parsed_at timestamptz,

    chunked_at timestamptz,
    embedded_at timestamptz,

    failure_stage text CHECK (
        failure_stage IN ('download', 'extract', 'parse', 'chunk', 'embed')
    ),
    failure_reason text,
    failure_detail text,
    failed_at timestamptz,
    CHECK ((failure_stage IS NULL) = (failure_reason IS NULL))
);

-- A listing window that discovery cannot read in full: one day with more
-- entries than a page shows. A later complete listing of the window deletes it.
CREATE TABLE IF NOT EXISTS listing_failures (
    start_date date NOT NULL,
    end_date date NOT NULL,
    reason text NOT NULL,
    detail text NOT NULL,
    failed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (start_date, end_date)
);

-- A passage of a resolution's body: body[char_start:char_end], on pages
-- page_start to page_end (1-based). `search` indexes the text for full-text
-- search; `embedding` is null until the resolution is embedded.
-- `id` names one version of a passage: replacing a resolution's chunks gives
-- new ids, so a vector computed for the old ones finds nothing to attach to.
CREATE TABLE IF NOT EXISTS chunks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resolution_id bigint NOT NULL REFERENCES resolutions (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    section text NOT NULL,
    page_start integer NOT NULL,
    page_end integer NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    text text NOT NULL,
    search tsvector GENERATED ALWAYS AS (to_tsvector('spanish_unaccent', text)) STORED,
    embedding vector(1024),
    UNIQUE (resolution_id, ordinal),
    CHECK (char_start < char_end),
    CHECK (page_start <= page_end)
);

CREATE TABLE IF NOT EXISTS embedding_model (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    model text NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_search_idx ON chunks USING gin (search);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS resolutions_filter_idx
    ON resolutions (claim_type, outcome, resolved_on);

-- One call to a language model. `reserved_usd` is the most the call can cost,
-- written before the call; `usd` is what it was billed, written after. A call
-- with no `usd` is in flight and counts at `reserved_usd` (answers/spend.py).
CREATE TABLE IF NOT EXISTS llm_calls (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    purpose text NOT NULL CHECK (purpose IN ('answer', 'judge', 'card')),
    model text NOT NULL,
    reserved_usd numeric NOT NULL CHECK (reserved_usd >= 0),
    input_tokens integer,
    output_tokens integer,
    usd numeric CHECK (usd >= 0),
    error text
);

CREATE INDEX IF NOT EXISTS llm_calls_started_idx ON llm_calls (started_at);

-- What a language model read from one resolution's body, written offline
-- (cards/). `fields` and `readings` hold only what passed the quote check;
-- `rejected` holds the rest. A card is current while `prompt_version` and
-- `body_sha256` match the prompt and the body it was written from.
-- `unsupported` is null until the card is judged.
CREATE TABLE IF NOT EXISTS cards (
    resolution_id bigint PRIMARY KEY REFERENCES resolutions (id) ON DELETE CASCADE,
    prompt_version text NOT NULL,
    body_sha256 text NOT NULL,
    model text NOT NULL,
    fields jsonb NOT NULL,
    readings jsonb NOT NULL,
    rejected jsonb NOT NULL,
    usd numeric NOT NULL CHECK (usd >= 0),
    written_at timestamptz NOT NULL DEFAULT now(),
    judge_model text,
    unsupported jsonb,
    judged_at timestamptz
);
