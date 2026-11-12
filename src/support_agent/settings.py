from functools import lru_cache
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT = PACKAGE_DIR.parents[1]
ROOT = CHECKOUT if (CHECKOUT / "pyproject.toml").is_file() else PACKAGE_DIR / "resources"


class AdminSettings(BaseSettings):
    """Explicit administrative credentials, independent of runtime/model configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_admin_url: str = Field(min_length=1, repr=False)
    business_timezone: str = "Europe/Moscow"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: Literal["local", "internal"] = "local"
    mode: Literal["demo", "internal"] = "demo"
    delivery_mode: Literal["shadow", "reviewed"] = "shadow"
    database_url: str = Field(
        default="postgresql+psycopg://agent_worker:local@127.0.0.1:55432/agent", repr=False
    )
    database_admin_url: str | None = Field(default=None, repr=False)
    business_timezone: str = "Europe/Moscow"
    service_products: list[str] = ["atlas", "boreal"]
    fixtures_dir: Path = ROOT / "fixtures/demo"
    storage_dir: Path = Path.cwd() / ".local/storage"
    token_file: Path = Path.cwd() / ".local/tokens.json"
    batch_size: int = Field(20, ge=1, le=200)
    poll_seconds: float = Field(600, ge=0.1)
    lease_seconds: int = Field(90, ge=2)
    max_attempts: int = Field(3, ge=1, le=3)
    max_files: int = Field(8, ge=1, le=32)
    max_file_bytes: int = Field(10_000_000, ge=1000, le=100_000_000)
    parse_memory_mb: int = Field(768, ge=256, le=2048)
    parse_timeout_seconds: int = Field(12, ge=1, le=30)
    max_pages: int = Field(30, ge=1, le=100)
    max_audio_seconds: int = Field(1200, ge=1, le=3600)
    max_segments: int = Field(80, ge=1, le=500)
    context_chars: int = Field(16000, ge=100, le=100000)
    max_model_calls: int = Field(12, ge=1, le=30)
    max_items: int = Field(12, ge=1, le=20)
    model_slots: int = Field(2, ge=1, le=16)
    timeout_seconds: float = Field(20, ge=0.1, le=60)
    max_gateway_json_bytes: int = Field(1_000_000, ge=1000, le=10_000_000)
    model_input_chars: int = Field(32000, ge=1000, le=200000)
    extraction_output_tokens: int = Field(3000, ge=100, le=10000)
    matching_output_tokens: int = Field(800, ge=100, le=3000)
    extraction_model: str = Field(default="Qwen 3.8", min_length=1, max_length=60)
    asr_model: str = Field(default="Qwen-ASR", min_length=1, max_length=100)
    vl_model: str = Field(default="Qwen-VL", min_length=1, max_length=100)
    candidate_limit: int = Field(20, ge=1, le=20)
    matching_limit: int = Field(5, ge=1, le=5)
    threshold: int = Field(2, ge=2)
    window_days: int = Field(30, ge=1, le=90)
    retention_days: int = Field(90, ge=1)
    gateway_url: str | None = None
    gateway_token: str | None = Field(default=None, repr=False)
    allowed_hosts: list[str] = []
    internal_ca: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_public_key: str | None = None
    backlog_idempotent: bool = False
    backlog_lookup: bool = False

    @model_validator(mode="after")
    def validate_internal(self):
        if not self.service_products or len(set(self.service_products)) != len(self.service_products):
            raise ValueError("service_products must be a nonempty list of unique product IDs")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", p) for p in self.service_products):
            raise ValueError("product IDs must contain only letters, digits, underscore or hyphen")
        try:
            ZoneInfo(self.business_timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("unknown business timezone") from None
        if self.mode == "internal" or self.app_env == "internal":
            required = [
                self.gateway_url,
                self.gateway_token,
                self.jwt_issuer,
                self.jwt_audience,
                self.jwt_public_key,
            ]
            if self.mode != "internal" or not all(required):
                raise ValueError(
                    "internal requires gateway credentials and verified corporate JWT configuration"
                )
            u = urlparse(self.gateway_url)
            if (
                u.scheme != "https"
                or u.hostname not in self.allowed_hosts
                or u.username
                or u.password
                or u.query
                or u.fragment
            ):
                raise ValueError("internal gateway must be HTTPS on the configured host allowlist")
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

                key = serialization.load_pem_public_key(Path(self.jwt_public_key).read_bytes())
                if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise ValueError(
                    "internal requires a readable RSA public key of at least 2048 bits"
                ) from None

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
