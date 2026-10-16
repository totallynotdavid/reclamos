-- Each row represents a TRASU resolution published on gob.pe. The columns follow the
-- pipeline: discovery, download, extraction, parse. A stage that cannot finish
-- records failure_stage, failure_reason and failure_detail instead of leaving
-- the row without an explanation. A later success clears them.
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

    failure_stage text CHECK (failure_stage IN ('download', 'extract', 'parse')),
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
