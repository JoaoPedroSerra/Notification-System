# Notification System

API REST para envio assincrono de emails transacionais. O sistema persiste cada notificacao no banco, despacha o envio em background sem bloquear a resposta HTTP e reprocessa automaticamente falhas via scheduler. Desenvolvido como demonstracao de boas praticas em Python/FastAPI para cenarios reais de fintech, e-commerce e SaaS.

## Stack

- **FastAPI** com BackgroundTasks para processamento nao-bloqueante
- **SQLAlchemy 2 + PostgreSQL** com Alembic para migrations versionadas
- **smtplib + email.mime** (biblioteca padrao) para envio via SMTP/STARTTLS
- **Jinja2** para templates HTML de email
- **APScheduler** para retry automatico sem dependencia de Celery/Redis
- **slowapi** para rate limiting por IP
- **Pydantic v2** para validacao de entrada e configuracao

## Estrutura

```
app/
  api/            rotas e schemas Pydantic
  core/           configuracao, autenticacao, rate limiting, banco e logging
  models/         ORM: Recipient e Notification
  services/       email_sender, notification_service, scheduler
  templates/email templates HTML (base + 5 tipos)
alembic/          migrations versionadas
tests/            testes unitarios, de integracao e de seguranca (SQLite em memoria)
```

## Como rodar

### Pre-requisitos

- Python 3.11+ e PostgreSQL local, ou Docker e Docker Compose

### Localmente (sem Docker)

```bash
# 1. Crie e ative o ambiente virtual
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

# 2. Instale as dependencias
pip install -r requirements.txt

# 3. Configure o ambiente
cp .env.example .env
```

Edite o `.env` gerado. Para rodar localmente, o `DATABASE_URL` deve apontar para `localhost`, nao para `db`:

```env
DATABASE_URL=postgresql://notifications_user:sua_senha@localhost:5432/notifications_db
```

Crie o banco no PostgreSQL local:

```bash
createuser notifications_user
createdb -O notifications_user notifications_db
psql -c "ALTER USER notifications_user WITH PASSWORD 'sua_senha';"
```

```bash
# 4. Aplique as migrations
alembic upgrade head

# 5. Suba o servidor
uvicorn app.main:app --reload
```

A API estara disponivel em `http://localhost:8000`.
Documentacao interativa (apenas em desenvolvimento): `http://localhost:8000/docs`

### Com Docker Compose

```bash
cp .env.example .env
# Edite .env com suas credenciais SMTP
docker compose up --build
```

### Testes

```bash
pytest tests/ -v
```

Os testes usam SQLite em memoria com `StaticPool` (todas as conexoes compartilham o mesmo banco em memoria) e nunca fazem chamadas SMTP ou PostgreSQL reais.

### Configurar Gmail SMTP

Acesse: Conta Google > Seguranca > Verificacao em 2 etapas > Senhas de app

Gere uma senha de 16 caracteres e use como `SMTP_PASSWORD` no `.env`.

## Autenticacao

Todos os endpoints (exceto `/health`) exigem o header `X-API-Key`.

Gere uma chave segura:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Defina no `.env`:
```
API_KEY=<valor-gerado-acima>
```

Use em cada request:
```bash
curl -H "X-API-Key: <sua-chave>" http://localhost:8000/api/v1/notifications/
```

Em desenvolvimento, se `API_KEY` estiver vazia no `.env`, a autenticacao e desabilitada com um aviso de log.

## Configurar producao

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Variaveis obrigatorias com `APP_ENV=production`:

| Variavel | Requisito |
|----------|-----------|
| `SECRET_KEY` | Minimo 32 chars, nao pode ser o valor padrao |
| `API_KEY` | Minimo 32 chars |
| `SMTP_USER` | Obrigatorio |
| `SMTP_PASSWORD` | Obrigatorio |
| `EMAIL_FROM` | Obrigatorio |
| `ALLOWED_ORIGINS` | Nao pode ser `*` |

O sistema recusa iniciar se qualquer um desses requisitos nao for atendido.

## Endpoints

| Metodo | Rota | Descricao |
|--------|------|-----------|
| GET | `/health` | Health check (publico) |
| POST | `/api/v1/notifications/` | Notificacao generica |
| POST | `/api/v1/notifications/welcome` | Boas-vindas |
| POST | `/api/v1/notifications/password-reset` | Redefinicao de senha |
| POST | `/api/v1/notifications/payment-confirmation` | Confirmacao de pagamento |
| POST | `/api/v1/notifications/alert` | Alerta (info / warning / critical) |
| GET | `/api/v1/notifications/` | Listar com filtro por status e paginacao |
| GET | `/api/v1/notifications/{id}` | Detalhe completo (inclui last_error) |
| POST | `/api/v1/notifications/{id}/retry` | Reenviar notificacao com status FAILED |

## Exemplos

**Boas-vindas**
```bash
curl -X POST http://localhost:8000/api/v1/notifications/welcome \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <sua-chave>" \
  -d '{
    "recipient_email": "usuario@exemplo.com",
    "recipient_name": "Maria",
    "app_name": "MeuApp",
    "login_url": "https://meuapp.com/login"
  }'
```

**Alerta critico**
```bash
curl -X POST http://localhost:8000/api/v1/notifications/alert \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <sua-chave>" \
  -d '{
    "recipient_email": "ops@empresa.com",
    "recipient_name": "Time de Ops",
    "alert_title": "CPU acima de 90%",
    "alert_message": "Servidor web-01 com CPU em 95% ha 10 minutos.",
    "severity": "critical",
    "action_url": "https://grafana.empresa.com",
    "action_label": "Abrir Grafana"
  }'
```

**Confirmacao de pagamento**
```bash
curl -X POST http://localhost:8000/api/v1/notifications/payment-confirmation \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <sua-chave>" \
  -d '{
    "recipient_email": "cliente@email.com",
    "recipient_name": "Joao Silva",
    "order_id": "ORD-2024-001",
    "amount": 299.90,
    "currency": "BRL",
    "items": [{"name": "Plano Pro Mensal", "price": 299.90}]
  }'
```

**Agendamento futuro**
```bash
curl -X POST http://localhost:8000/api/v1/notifications/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <sua-chave>" \
  -d '{
    "recipient_email": "usuario@exemplo.com",
    "recipient_name": "Ana",
    "notification_type": "generic",
    "subject": "Lembrete de renovacao",
    "scheduled_at": "2024-12-31T10:00:00Z"
  }'
```

## Ciclo de vida de uma notificacao

```
PENDING --> SENDING --> SENT
                    --> RETRYING (tentativas < maximo)
                              --> FAILED (tentativas = maximo)
```

O scheduler verifica notificacoes `PENDING` e `RETRYING` a cada `SCHEDULER_RETRY_INTERVAL_SECONDS` segundos. Registros presos em `SENDING` por mais de `SCHEDULER_STALE_SENDING_SECONDS` segundos sao automaticamente promovidos para `RETRYING`. Notificacoes `FAILED` podem ser reenviadas manualmente via `POST /api/v1/notifications/{id}/retry`.
