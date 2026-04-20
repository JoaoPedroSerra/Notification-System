"""
APScheduler integration.
Registra jobs em background e expoe helpers start/stop
chamados pelo handler de lifespan do FastAPI.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import get_logger
from app.services.notification_service import reset_stale_sending, retry_pending_notifications

logger = get_logger(__name__)
settings = get_settings()

scheduler = BackgroundScheduler(timezone="UTC")


def _retry_job() -> None:
    """Callback do scheduler — cria sua propria sessao de DB."""
    db = SessionLocal()
    try:
        # Primeiro reseta registros travados em SENDING por crash anterior
        reset_stale_sending(db)
        count = retry_pending_notifications(db)
        if count:
            logger.info("Scheduler: processed %d notification(s)", count)
    except Exception as exc:
        logger.error("Scheduler retry job error: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    # Executa uma vez imediatamente no startup para limpar stale records
    db = SessionLocal()
    try:
        reset_stale_sending(db)
    finally:
        db.close()

    scheduler.add_job(
        _retry_job,
        trigger=IntervalTrigger(seconds=settings.SCHEDULER_RETRY_INTERVAL_SECONDS),
        id="retry_notifications",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Scheduler started (retry interval: %ds)",
        settings.SCHEDULER_RETRY_INTERVAL_SECONDS,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
