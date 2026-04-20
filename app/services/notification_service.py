"""
Core notification service.
Handles creation, dispatch, and retry logic for notifications.
"""
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import get_logger
from app.models import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    Recipient,
)
from app.services.email_sender import render_template, send_email

logger = get_logger(__name__)
settings = get_settings()

# Tamanho maximo da mensagem de erro armazenada no banco.
# Evita expor stack traces completos na API e limita crescimento da coluna.
_MAX_ERROR_LENGTH = 500

# Whitelist de templates permitidos. Impede que um valor corrompido no banco
# tente carregar arquivos arbitrarios via jinja_env.get_template().
ALLOWED_TEMPLATES: frozenset[str] = frozenset({
    "welcome.html",
    "password_reset.html",
    "payment_confirmation.html",
    "alert.html",
    "generic.html",
})

TEMPLATE_MAP: dict[NotificationType, str] = {
    NotificationType.WELCOME: "welcome.html",
    NotificationType.PASSWORD_RESET: "password_reset.html",
    NotificationType.PAYMENT_CONFIRMATION: "payment_confirmation.html",
    NotificationType.ALERT: "alert.html",
    NotificationType.GENERIC: "generic.html",
}


# ── Recipient helpers ─────────────────────────────────────────────────────────


def get_or_create_recipient(db: Session, *, name: str, email: str) -> Recipient:
    """
    Retorna o destinatario existente ou cria um novo.

    Usa try/except em vez de check-then-insert para evitar race condition
    quando duas requisicoes chegam simultaneamente com o mesmo e-mail
    (o SELECT passaria nas duas, ambas tentariam inserir, e uma receberia
    IntegrityError por violacao do UNIQUE constraint).
    """
    recipient = db.query(Recipient).filter_by(email=email).first()
    if recipient:
        return recipient

    try:
        recipient = Recipient(name=name, email=email)
        db.add(recipient)
        db.commit()
        db.refresh(recipient)
        logger.info("Recipient created: %s", email)
        return recipient
    except IntegrityError:
        db.rollback()
        # Outra thread/processo inseriu entre o SELECT e o INSERT; releia.
        recipient = db.query(Recipient).filter_by(email=email).first()
        if not recipient:
            raise  # Erro genuino, nao uma race condition
        return recipient


# ── Notification CRUD ─────────────────────────────────────────────────────────


def create_notification(
    db: Session,
    *,
    recipient_id: int,
    notification_type: NotificationType,
    subject: str,
    template_context: dict[str, Any] | None = None,
    body_text: str | None = None,
    scheduled_at: datetime | None = None,
) -> Notification:
    """
    Persiste um registro de notificacao.
    Se scheduled_at for fornecido o scheduler a buscara depois;
    caso contrario ela e despachada imediatamente via Background Task.
    """
    notif = Notification(
        recipient_id=recipient_id,
        channel=NotificationChannel.EMAIL,
        notification_type=notification_type,
        subject=subject,
        template_name=TEMPLATE_MAP.get(notification_type, "generic.html"),
        template_context=json.dumps(template_context or {}),
        body_text=body_text,
        status=NotificationStatus.PENDING,
        scheduled_at=scheduled_at,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    logger.info("Notification #%d created (type=%s)", notif.id, notification_type)
    return notif


# ── Dispatch ──────────────────────────────────────────────────────────────────


def dispatch_notification(notification_id: int) -> None:
    """
    Tenta enviar uma unica notificacao.

    IMPORTANTE: esta funcao cria sua propria sessao de banco de dados.
    Ela e chamada pelo BackgroundTasks do FastAPI e pelo APScheduler,
    ambos rodando fora do escopo de request — portanto nao pode receber
    uma sessao de fora, pois ela ja pode estar encerrada.
    """
    db = SessionLocal()
    try:
        _dispatch_with_session(db, notification_id)
    finally:
        db.close()


def _dispatch_with_session(db: Session, notification_id: int) -> None:
    notif = db.get(Notification, notification_id)
    if not notif:
        logger.warning("Notification #%d not found, skipping", notification_id)
        return

    if notif.status == NotificationStatus.SENT:
        logger.info("Notification #%d already sent, skipping", notification_id)
        return

    notif.status = NotificationStatus.SENDING
    db.commit()

    try:
        _send(db, notif)
        notif.status = NotificationStatus.SENT
        notif.sent_at = datetime.now(timezone.utc)
        notif.last_error = None
        db.commit()
        logger.info("Notification #%d sent successfully", notification_id)

    except Exception as exc:
        error_msg = str(exc)[:_MAX_ERROR_LENGTH]
        logger.error("Notification #%d failed: %s", notification_id, error_msg)

        notif.retry_count += 1
        notif.last_error = error_msg

        if notif.retry_count >= settings.SCHEDULER_MAX_RETRIES:
            notif.status = NotificationStatus.FAILED
            logger.error(
                "Notification #%d exhausted %d retries, marked FAILED",
                notification_id,
                settings.SCHEDULER_MAX_RETRIES,
            )
        else:
            notif.status = NotificationStatus.RETRYING
            logger.warning(
                "Notification #%d will be retried (attempt %d/%d)",
                notification_id,
                notif.retry_count,
                settings.SCHEDULER_MAX_RETRIES,
            )

        db.commit()


def retry_pending_notifications(db: Session) -> int:
    """
    Chamado pelo job do APScheduler.
    Re-despacha notificacoes PENDING e RETRYING que estao vencidas.
    Retorna o numero de notificacoes processadas.
    """
    now = datetime.now(timezone.utc)

    candidates = (
        db.query(Notification)
        .filter(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.RETRYING]),
            (Notification.scheduled_at.is_(None)) | (Notification.scheduled_at <= now),
        )
        .all()
    )

    logger.info("Retry job: found %d candidate(s)", len(candidates))
    for notif in candidates:
        dispatch_notification(notif.id)

    return len(candidates)


def reset_stale_sending(db: Session) -> int:
    """
    Reseta notificacoes presas em SENDING por mais de SCHEDULER_STALE_SENDING_SECONDS.

    Isso acontece quando o processo cai enquanto uma notificacao esta sendo enviada.
    Sem esse reset, o scheduler nunca as reprocessaria pois filtra apenas PENDING/RETRYING.
    """
    from datetime import timedelta
    from sqlalchemy import update

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.SCHEDULER_STALE_SENDING_SECONDS
    )

    result = db.execute(
        update(Notification)
        .where(
            Notification.status == NotificationStatus.SENDING,
            Notification.updated_at <= cutoff,
        )
        .values(status=NotificationStatus.RETRYING)
    )
    db.commit()

    count = result.rowcount
    if count:
        logger.warning("Reset %d stale SENDING notification(s) to RETRYING", count)
    return count


# ── Internal send ─────────────────────────────────────────────────────────────


def _send(db: Session, notif: Notification) -> None:
    recipient = db.get(Recipient, notif.recipient_id)
    if not recipient:
        raise ValueError(f"Recipient #{notif.recipient_id} not found")

    template_name = notif.template_name or "generic.html"
    if template_name not in ALLOWED_TEMPLATES:
        raise ValueError(
            f"Template '{template_name}' nao esta na lista de templates permitidos."
        )

    context: dict[str, Any] = json.loads(notif.template_context or "{}")
    context.setdefault("recipient_name", recipient.name)
    context.setdefault("subject", notif.subject)

    html_body = render_template(template_name, context)

    send_email(
        to_email=recipient.email,
        to_name=recipient.name,
        subject=notif.subject,
        html_body=html_body,
        text_body=notif.body_text,
    )
