CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS resolutions (
    id text PRIMARY KEY,
    title text NOT NULL,
    body text NOT NULL,
    embedding vector(1536),
    created_at timestamptz NOT NULL DEFAULT now()
);
