"""
FastAPI router para a API de notificacoes.
Todos os envios sao nao-bloqueantes (BackgroundTasks).

Seguranca:
  - verify_api_key: dependency aplicada no router
  - @limiter.limit: rate limiting por IP em cada endpoint de escrita.
    Leitura tambem e limitada para evitar enumeracao de IDs.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.schemas import (
    AlertRequest,
    NotificationAdminOut,
    NotificationCreate,
    NotificationListOut,
    NotificationOut,
    PaymentConfirmationRequest,
    PasswordResetRequest,
    WelcomeEmailRequest,
)
from app.core.auth import verify_api_key
from app.core.database import get_db
from app.core.rate_limit import limiter, rate_limit_string
from app.models import Notification, NotificationStatus, NotificationType
from app.services.notification_service import (
    create_notification,
    dispatch_notification,
    get_or_create_recipient,
)

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
    # Apply authentication to every route on this router.
    # Any route added in the future is protected automatically.
    dependencies=[Depends(verify_api_key)],
)


# ── Internal helper ───────────────────────────────────────────────────────────


def _enqueue(
    bg: BackgroundTasks,
    db: Session,
    *,
    email: str,
    name: str,
    notification_type: NotificationType,
    subject: str,
    context: dict,
    scheduled_at: datetime | None = None,
) -> Notification:
    recipient = get_or_create_recipient(db, email=email, name=name)
    notif = create_notification(
        db,
        recipient_id=recipient.id,
        notification_type=notification_type,
        subject=subject,
        template_context=context,
        scheduled_at=scheduled_at,
    )
    if not scheduled_at or scheduled_at <= datetime.now(timezone.utc):
        bg.add_task(dispatch_notification, notif.id)
    return notif


# ── Generic endpoint ──────────────────────────────────────────────────────────


@router.post("/", response_model=NotificationOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_string())
def send_notification(
    request: Request,
    payload: NotificationCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Cria e despacha assincronamente uma notificacao."""
    return _enqueue(
        background_tasks,
        db,
        email=payload.recipient_email,
        name=payload.recipient_name,
        notification_type=payload.notification_type,
        subject=payload.subject,
        context=payload.template_context,
        scheduled_at=payload.scheduled_at,
    )


# ── Typed shortcuts ───────────────────────────────────────────────────────────


@router.post("/welcome", response_model=NotificationOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_string())
def send_welcome(
    request: Request,
    payload: WelcomeEmailRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _enqueue(
        background_tasks,
        db,
        email=payload.recipient_email,
        name=payload.recipient_name,
        notification_type=NotificationType.WELCOME,
        subject=f"Bem-vindo ao {payload.app_name}",
        context={
            "app_name": payload.app_name,
            # HttpUrl -> str so Jinja2 templates receive a plain string
            "login_url": str(payload.login_url),
        },
    )


@router.post("/password-reset", response_model=NotificationOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_string())
def send_password_reset(
    request: Request,
    payload: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _enqueue(
        background_tasks,
        db,
        email=payload.recipient_email,
        name=payload.recipient_name,
        notification_type=NotificationType.PASSWORD_RESET,
        subject="Redefinicao de senha solicitada",
        context={
            "reset_url": str(payload.reset_url),
            "expires_in_minutes": payload.expires_in_minutes,
        },
    )


@router.post("/payment-confirmation", response_model=NotificationOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_string())
def send_payment_confirmation(
    request: Request,
    payload: PaymentConfirmationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _enqueue(
        background_tasks,
        db,
        email=payload.recipient_email,
        name=payload.recipient_name,
        notification_type=NotificationType.PAYMENT_CONFIRMATION,
        subject=f"Pagamento confirmado - Pedido #{payload.order_id}",
        context={
            "order_id": payload.order_id,
            "amount": payload.amount,
            "currency": payload.currency,
            "items": payload.items,
        },
    )


@router.post("/alert", response_model=NotificationOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_string())
def send_alert(
    request: Request,
    payload: AlertRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _enqueue(
        background_tasks,
        db,
        email=payload.recipient_email,
        name=payload.recipient_name,
        notification_type=NotificationType.ALERT,
        subject=f"[{payload.severity.upper()}] {payload.alert_title}",
        context={
            "alert_title": payload.alert_title,
            "alert_message": payload.alert_message,
            "severity": payload.severity,
            "action_url": str(payload.action_url) if payload.action_url else None,
            "action_label": payload.action_label,
        },
    )


# ── Query endpoints ───────────────────────────────────────────────────────────


@router.get("/", response_model=NotificationListOut)
@limiter.limit(rate_limit_string())
def list_notifications(
    request: Request,
    status: NotificationStatus | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(Notification)
    if status:
        q = q.filter(Notification.status == status)
    total = q.count()
    items = q.order_by(Notification.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": items}


@router.get("/{notification_id}", response_model=NotificationAdminOut)
@limiter.limit(rate_limit_string())
def get_notification(
    request: Request,
    notification_id: int,
    db: Session = Depends(get_db),
):
    """
    Retorna detalhe completo incluindo last_error.
    Protegido por API key — o chamador ja e autenticado.
    """
    notif = db.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notif


@router.post(
    "/{notification_id}/retry",
    response_model=NotificationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(rate_limit_string())
def retry_notification(
    request: Request,
    notification_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Reenvia manualmente uma notificacao com status FAILED."""
    notif = db.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notif.status != NotificationStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Apenas notificacoes FAILED podem ser reenviadas. Status atual: {notif.status}",
        )
    notif.status = NotificationStatus.PENDING
    notif.retry_count = 0
    notif.last_error = None
    db.commit()
    background_tasks.add_task(dispatch_notification, notif.id)
    db.refresh(notif)
    return notif

