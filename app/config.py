"""A3 settings; credentials come only from the server's environment."""

from dataclasses import dataclass, field
import os
from pathlib import Path

from app.modules.models.bundle import DEFAULT_ARTIFACT_ROOT


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/runtime/platform.db"
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    admin_token: str = field(default="", repr=False)
    scorer_token: str = field(default="", repr=False)
    inference_timeout_ms: float = 2000.0

    @classmethod
    def from_env(cls):
        return cls(database_url=os.getenv("DATABASE_URL", cls.database_url),
                   artifact_root=Path(os.getenv("ARTIFACT_ROOT", str(DEFAULT_ARTIFACT_ROOT))),
                   admin_token=os.getenv("APP_ADMIN_TOKEN", ""),
                   scorer_token=os.getenv("APP_SCORER_TOKEN", ""),
                   inference_timeout_ms=float(os.getenv("INFERENCE_TIMEOUT_MS", "2000")))
