"""
SQLAlchemy ORM models for the notification system.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Enumerations ──────────────────────────────────────────────────────────────


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"       # queued, not yet attempted
    SENDING = "sending"       # being processed right now
    SENT = "sent"             # delivered successfully
    FAILED = "failed"         # all retries exhausted
    RETRYING = "retrying"     # scheduled for another attempt


class NotificationChannel(str, enum.Enum):
    EMAIL = "email"
    # future channels can be added here (sms, push, slack…)


class NotificationType(str, enum.Enum):
    WELCOME = "welcome"
    PASSWORD_RESET = "password_reset"
    PAYMENT_CONFIRMATION = "payment_confirmation"
    ALERT = "alert"
    GENERIC = "generic"


# ── Models ────────────────────────────────────────────────────────────────────


class Recipient(Base):
    """A person who can receive notifications."""

    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="recipient", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Recipient id={self.id} email={self.email!r}>"


class Notification(Base):
    """
    A single notification request.
    Tracks status, retry count, and scheduling metadata.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    recipient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel), nullable=False, default=NotificationChannel.EMAIL
    )
    notification_type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType), nullable=False, default=NotificationType.GENERIC
    )
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON-serialised dict of template variables
    template_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.PENDING, index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now_utc
    )

    recipient: Mapped["Recipient"] = relationship("Recipient", back_populates="notifications")

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} type={self.notification_type} "
            f"status={self.status} recipient_id={self.recipient_id}>"
        )
