from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_ignore_empty=True, extra="ignore")

    app_env: str = "local"
    api_prefix: str = "/api"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "uom_cleansing_poc"
    local_storage_root: Path = Path("./data/uom-jobs")
    max_upload_bytes: int = 100 * 1024 * 1024
    frontend_origin: str = "http://localhost:5173"
    ai_provider: str = "mock"
    ai_max_concurrency: int = 5
    # Parallel calls for the reasoning layer and the sample judge; default to ai_max_concurrency.
    # The judge uses a pro model with tighter rate limits, so it has its own setting.
    reasoner_concurrency: int | None = None
    judge_concurrency: int | None = None
    ai_timeout_seconds: float = 60
    # The judge thinks at HIGH on a pro model; it needs longer than the worker.
    judge_timeout_seconds: float = 180
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    # A stronger model that judges samples of the worker's output; defaults to gemini_model.
    judge_model: str | None = None
    # The reasoning layer in the pipeline: off | shadow (record only) | gate (the gate acts).
    reasoner_mode: str = "off"
    # Defaults to gemini_model. Budget guard: at most this many triggered rows per run.
    reasoner_model: str | None = None
    reasoner_max_rows: int = 3000
    pack_size_inference_enabled: bool = True
    discrepancy_ai_fallback_enabled: bool = False
    require_discrepancy_ack_before_export: bool = True
    default_rounding_decimals: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
