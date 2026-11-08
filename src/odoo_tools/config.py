"""Configuration without committed credentials."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "/app/data/bom_tools.sqlite3"
    cookie_secure: bool = False
    session_ttl_hours: int = 8

    @classmethod
    def from_env(cls) -> "Settings":
        ttl = int(os.getenv("BOM_SESSION_TTL_HOURS", "8"))
        if not 1 <= ttl <= 168:
            raise ValueError("BOM_SESSION_TTL_HOURS must be 1..168")
        return cls(
            db_path=os.getenv("BOM_DB_PATH", "/app/data/bom_tools.sqlite3"),
            cookie_secure=os.getenv("BOM_COOKIE_SECURE", "false").lower() == "true",
            session_ttl_hours=ttl,
        )
