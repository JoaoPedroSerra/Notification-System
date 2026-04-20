"""
Pydantic v2 request/response schemas for the notifications API.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator

from app.models import NotificationStatus, NotificationType

# ── Shared constraints ────────────────────────────────────────────────────────

_MAX_NAME_LEN = 255
_MAX_SUBJECT_LEN = 512
_MAX_SHORT_STR_LEN = 255
_MAX_MESSAGE_LEN = 4096
_MAX_ORDER_ID_LEN = 128
_MAX_ITEMS = 50
_MAX_SCHEDULE_DAYS = 365


def _safe_url(v: str | HttpUrl | None) -> str | None:
    """
    Normaliza e valida URLs fornecidas pelo chamador.
    Pydantic HttpUrl ja garante scheme http/https e estrutura valida.
    Aqui convertemos de volta para string para armazenamento.
    """
    if v is None:
        return None
    return str(v)


# ── Recipient ─────────────────────────────────────────────────────────────────


class RecipientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    email: EmailStr


class RecipientOut(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Notification ──────────────────────────────────────────────────────────────


class NotificationCreate(BaseModel):
    recipient_email: EmailStr
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    notification_type: NotificationType = NotificationType.GENERIC
    subject: str = Field(..., min_length=1, max_length=_MAX_SUBJECT_LEN)
    template_context: dict[str, Any] = Field(default_factory=dict)
    body_text: str | None = Field(default=None, max_length=_MAX_MESSAGE_LEN)
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def validate_scheduled_at(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        # Pydantic may pass a string before coercion in mode="before"
        if isinstance(v, str):
            from datetime import datetime as dt
            v = dt.fromisoformat(v.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if v.tzinfo is None:
            raise ValueError("scheduled_at deve incluir informacao de fuso horario.")
        if v <= now:
            raise ValueError("scheduled_at deve estar no futuro.")
        if v > now + timedelta(days=_MAX_SCHEDULE_DAYS):
            raise ValueError(
                f"scheduled_at nao pode ser mais de {_MAX_SCHEDULE_DAYS} dias no futuro."
            )
        return v

    @field_validator("template_context")
    @classmethod
    def validate_template_context(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 30:
            raise ValueError("template_context nao pode ter mais de 30 chaves.")
        for key, value in v.items():
            if isinstance(value, str) and len(value) > _MAX_MESSAGE_LEN:
                raise ValueError(
                    f"Valor da chave '{key}' no template_context excede {_MAX_MESSAGE_LEN} caracteres."
                )
        return v


class NotificationOut(BaseModel):
    """
    Resposta publica de notificacao.

    last_error e intencionalmente OMITIDO: mesmo truncado, esse campo
    pode vazar hostnames SMTP e detalhes de infraestrutura.
    Use NotificationAdminOut em rotas autenticadas quando necessario.
    """
    id: int
    recipient_id: int
    channel: str
    notification_type: str
    subject: str
    status: NotificationStatus
    retry_count: int
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationAdminOut(NotificationOut):
    """Resposta estendida com last_error — usar apenas em contextos autenticados."""
    last_error: str | None = None

    model_config = {"from_attributes": True}


class NotificationListOut(BaseModel):
    total: int
    items: list[NotificationOut]


# ── Typed request schemas ─────────────────────────────────────────────────────


class WelcomeEmailRequest(BaseModel):
    recipient_email: EmailStr
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    app_name: str = Field(default="MyApp", min_length=1, max_length=_MAX_SHORT_STR_LEN)
    login_url: HttpUrl = Field(default="https://app.example.com/login")


class PasswordResetRequest(BaseModel):
    recipient_email: EmailStr
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    reset_url: HttpUrl
    expires_in_minutes: int = Field(default=30, gt=0, le=1440)


class PaymentConfirmationRequest(BaseModel):
    recipient_email: EmailStr
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    order_id: str = Field(..., min_length=1, max_length=_MAX_ORDER_ID_LEN)
    amount: float = Field(..., gt=0, lt=1_000_000_000)
    currency: str = Field(default="BRL", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=_MAX_ITEMS)


class AlertRequest(BaseModel):
    recipient_email: EmailStr
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    alert_title: str = Field(..., min_length=1, max_length=_MAX_SHORT_STR_LEN)
    alert_message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_LEN)
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    action_url: HttpUrl | None = None
    action_label: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)

