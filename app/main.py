"""
FastAPI application entry point.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.api import notifications_router
from app.core.config import get_settings
from app.core.database import engine
from app.core.logging import get_logger, setup_logging
from app.core.rate_limit import limiter
from app.models.notification import Base
from app.services.scheduler import start_scheduler, stop_scheduler

setup_logging()
logger = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Creating database tables")
    Base.metadata.create_all(bind=engine)

    logger.info("Starting background scheduler")
    start_scheduler()

    yield

    logger.info("Stopping scheduler")
    stop_scheduler()


# Disable interactive docs in production — they expose the full API surface
# and accepted payloads to anyone who finds the URL.
_docs_url = None if settings.APP_ENV == "production" else "/docs"
_redoc_url = None if settings.APP_ENV == "production" else "/redoc"
_openapi_url = None if settings.APP_ENV == "production" else "/openapi.json"

app = FastAPI(
    title="Notification System",
    description="Servico de notificacoes por email com Background Tasks, APScheduler e Jinja2.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded. {exc.detail}"},
        headers={"Retry-After": "60"},
    )


# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    # Only the methods this API actually uses
    allow_methods=["GET", "POST"],
    # Only the headers callers need to send
    allow_headers=["Content-Type", "X-API-Key"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(notifications_router, prefix="/api/v1")


# ── Health check (unauthenticated — used by load balancers) ───────────────────
@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok"}

