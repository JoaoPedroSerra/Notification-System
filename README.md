# Notification System

API REST para envio assíncrono de emails transacionais. O sistema persiste cada notificação no banco, despacha o envio em background sem bloquear a resposta HTTP e reprocessa automaticamente falhas via scheduler.
## Stack

- **FastAPI** com BackgroundTasks para processamento não-bloqueante
- **SQLAlchemy 2 + PostgreSQL** com Alembic para migrations versionadas
- **smtplib + email.mime** (biblioteca padrão) para envio via SMTP/STARTTLS
- **Jinja2** para templates HTML de email
- **APScheduler** para retry automático sem dependência de Celery/Redis
- **slowapi** para rate limiting por IP
- **Pydantic v2** para validação de entrada e configuração

## Estrutura

```
app/
  api/            rotas e schemas Pydantic
  core/           configuração, autenticação, rate limiting, banco e logging
  models/         ORM: Recipient e Notification
  services/       email_sender, notification_service, scheduler
  templates/email templates HTML (base + 5 tipos)
alembic/          migrations versionadas
tests/            testes unitários, de integração e de segurança (SQLite em memória)
```

## Como rodar

### Pré-requisitos

- Python 3.11+ e PostgreSQL local, ou Docker e Docker Compose

### Localmente (sem Docker)

```bash
# 1. Crie e ative o ambiente virtual
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

# 2. Instale as dependências
pip install -r requirements.txt

# 3. Configure o ambiente
cp .env.example .env
```

Edite o `.env` gerado. Para rodar localmente, o `DATABASE_URL` deve apontar para `localhost`, não para `db`:

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

A API estará disponível em `http://localhost:8000`.
Documentação interativa (apenas em desenvolvimento): `http://localhost:8000/docs`

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

Os testes usam SQLite em memória com `StaticPool` (todas as conexões compartilham o mesmo banco em memória) e nunca fazem chamadas SMTP ou PostgreSQL reais.

### Configurar Gmail SMTP

Acesse: Conta Google > Segurança > Verificação em 2 etapas > Senhas de app

Gere uma senha de 16 caracteres e use como `SMTP_PASSWORD` no `.env`.

## Autenticação

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

Em desenvolvimento, se `API_KEY` estiver vazia no `.env`, a autenticação é desabilitada com um aviso de log.

## Configurar produção

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Variáveis obrigatórias com `APP_ENV=production`:

| Variável | Requisito |
|----------|-----------|
| `SECRET_KEY` | Mínimo 32 chars, não pode ser o valor padrão |
| `API_KEY` | Mínimo 32 chars |
| `SMTP_USER` | Obrigatório |
| `SMTP_PASSWORD` | Obrigatório |
| `EMAIL_FROM` | Obrigatório |
| `ALLOWED_ORIGINS` | Não pode ser `*` |

O sistema recusa iniciar se qualquer um desses requisitos não for atendido.

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/health` | Health check (público) |
| POST | `/api/v1/notifications/` | Notificação genérica |
| POST | `/api/v1/notifications/welcome` | Boas-vindas |
| POST | `/api/v1/notifications/password-reset` | Redefinição de senha |
| POST | `/api/v1/notifications/payment-confirmation` | Confirmação de pagamento |
| POST | `/api/v1/notifications/alert` | Alerta (info / warning / critical) |
| GET | `/api/v1/notifications/` | Listar com filtro por status e paginação |
| GET | `/api/v1/notifications/{id}` | Detalhe completo (inclui last_error) |
| POST | `/api/v1/notifications/{id}/retry` | Reenviar notificação com status FAILED |

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

**Alerta crítico**
```bash
curl -X POST http://localhost:8000/api/v1/notifications/alert \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <sua-chave>" \
  -d '{
    "recipient_email": "ops@empresa.com",
    "recipient_name": "Time de Ops",
    "alert_title": "CPU acima de 90%",
    "alert_message": "Servidor web-01 com CPU em 95% há 10 minutos.",
    "severity": "critical",
    "action_url": "https://grafana.empresa.com",
    "action_label": "Abrir Grafana"
  }'
```

**Confirmação de pagamento**
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
    "subject": "Lembrete de renovação",
    "scheduled_at": "2024-12-31T10:00:00Z"
  }'
```

## Ciclo de vida de uma notificação

```
PENDING --> SENDING --> SENT
                    --> RETRYING (tentativas < máximo)
                              --> FAILED (tentativas = máximo)
```

O scheduler verifica notificações `PENDING` e `RETRYING` a cada `SCHEDULER_RETRY_INTERVAL_SECONDS` segundos. Registros presos em `SENDING` por mais de `SCHEDULER_STALE_SENDING_SECONDS` segundos são automaticamente promovidos para `RETRYING`. Notificações `FAILED` podem ser reenviadas manualmente via `POST /api/v1/notifications/{id}/retry`.
