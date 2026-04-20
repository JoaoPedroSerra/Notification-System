from functools import lru_cache
from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

_KNOWN_WEAK_SECRETS = {"dev-secret-key", "", "secret", "changeme", "password", "test"}


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    # Default is intentionally localhost-only with no password embedded here.
    # In any real environment supply DATABASE_URL via the environment / .env file.
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/notifications_db"

    # ── Email ─────────────────────────────────────────────────────────────────
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = ""
    EMAIL_FROM_NAME: str = "Notification System"

    # ── Auth ──────────────────────────────────────────────────────────────────
    # Shared secret sent in the X-API-Key request header.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    API_KEY: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    LOG_LEVEL: str = "INFO"

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Separate multiple origins with commas:
    # ALLOWED_ORIGINS=https://app.com,https://admin.app.com
    ALLOWED_ORIGINS: str = "*"

    # ── Rate limiting (requests per minute per IP) ─────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = 30

    # ── Scheduler ─────────────────────────────────────────────────────────────
    SCHEDULER_RETRY_INTERVAL_SECONDS: int = 60
    SCHEDULER_MAX_RETRIES: int = 3
    SCHEDULER_STALE_SENDING_SECONDS: int = 300

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL deve ser um de: {allowed}")
        return upper

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """
        Hard-fail on startup when insecure defaults reach a production process.
        This is a last-resort guard; secrets should be injected via a secrets
        manager (Vault, AWS Secrets Manager, etc.) before this point.
        """
        if self.APP_ENV != "production":
            return self

        errors: list[str] = []

        if self.SECRET_KEY.lower() in _KNOWN_WEAK_SECRETS or len(self.SECRET_KEY) < 32:
            errors.append(
                "SECRET_KEY insegura ou curta demais (minimo 32 chars)."
            )

        if not self.API_KEY or len(self.API_KEY) < 32:
            errors.append(
                "API_KEY ausente ou curta demais (minimo 32 chars). "
                "Gere com: python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        if not self.SMTP_USER or not self.SMTP_PASSWORD:
            errors.append("SMTP_USER e SMTP_PASSWORD sao obrigatorios em producao.")

        if not self.EMAIL_FROM:
            errors.append("EMAIL_FROM e obrigatorio em producao.")

        if self.ALLOWED_ORIGINS.strip() == "*":
            errors.append(
                "ALLOWED_ORIGINS=* e proibido em APP_ENV=production. "
                "Liste as origens permitidas separadas por virgula."
            )

        if "postgres:postgres" in self.DATABASE_URL or "password" in self.DATABASE_URL.lower():
            errors.append(
                "DATABASE_URL parece conter credenciais fracas/padrao em producao."
            )

        if errors:
            raise ValueError(
                "Configuracao de producao invalida:\n" + "\n".join(f"  - {e}" for e in errors)
            )

        return self

    def get_allowed_origins(self) -> List[str]:
        if self.ALLOWED_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
