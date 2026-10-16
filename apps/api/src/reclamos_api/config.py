import os
from dataclasses import dataclass

DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-large"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    queue_schema: str = "task_queue"
    pdf_store: str = "local"
    pdf_store_path: str = ".data/pdfs"
    pdf_bucket: str | None = None
    s3_endpoint_url: str | None = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_cache_path: str = ".data/models"

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            database_url=_database_url(),
            pdf_store=os.environ.get("PDF_STORE", "local"),
            pdf_store_path=os.environ.get("PDF_STORE_PATH", ".data/pdfs"),
            pdf_bucket=os.environ.get("PDF_BUCKET"),
            s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            embedding_model=os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_cache_path=os.environ.get("EMBEDDING_CACHE_PATH", ".data/models"),
        )


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "reclamos")
    user = os.environ.get("DB_USER", "postgres")
    password = os.environ.get("DB_PASSWORD", "")
    credentials = f"{user}:{password}" if password else user
    return f"postgresql://{credentials}@{host}:{port}/{name}"
