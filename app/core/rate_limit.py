"""
Rate limiting via slowapi (wrapper de limits sobre FastAPI).

Por que rate limiting no application layer e nao no Nginx/gateway:
  Em desenvolvimento nao existe gateway. O rate limiter aqui garante
  protecao mesmo sem infraestrutura de borda, e serve como camada de
  defesa em profundidade mesmo quando ha um gateway na frente.

Em producao recomenda-se adicionar rate limiting tambem no API Gateway
ou no Nginx upstream, usando este como segunda linha de defesa.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

settings = get_settings()

# O rate limit e configuravel via RATE_LIMIT_PER_MINUTE no .env.
# get_remote_address usa o IP real do cliente (respeita X-Forwarded-For
# quando o app roda atras de um proxy confiavel).
limiter = Limiter(key_func=get_remote_address, default_limits=[])


def rate_limit_string() -> str:
    """Retorna a string de limite no formato esperado pelo slowapi."""
    return f"{settings.RATE_LIMIT_PER_MINUTE}/minute"
