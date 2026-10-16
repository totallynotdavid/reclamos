import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    queue_schema: str = "task_queue"

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            return cls(database_url=database_url)

        host = os.environ.get("DB_HOST", "127.0.0.1")
        port = os.environ.get("DB_PORT", "5432")
        name = os.environ.get("DB_NAME", "reclamos")
        user = os.environ.get("DB_USER", "postgres")
        password = os.environ.get("DB_PASSWORD", "")
        credentials = f"{user}:{password}" if password else user
        return cls(database_url=f"postgresql://{credentials}@{host}:{port}/{name}")
