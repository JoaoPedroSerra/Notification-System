"""
Autenticacao via API Key no header X-API-Key.

Por que API Key e nao JWT aqui:
  Esta e uma API server-to-server (servicos internos chamando o notification
  system). API Keys sao o padrao correto para esse padrao de uso —
  JWTs fazem sentido quando ha sessoes de usuario humano com expiracao.

Em producao, a API_KEY deve ser injetada via secrets manager
(Vault, AWS Secrets Manager, GCP Secret Manager) e rotacionada periodicamente.
"""
import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import get_settings, Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(
    api_key: str | None = Security(_API_KEY_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency que valida o header X-API-Key.

    Usa hmac.compare_digest para comparacao em tempo constante,
    evitando timing attacks que poderiam enumerar o valor da chave
    caractere por caractere comparando tempos de resposta.

    Se API_KEY estiver vazia (ambiente de desenvolvimento sem config),
    a autenticacao e ignorada com um aviso de log.
    """
    configured_key = settings.API_KEY

    if not configured_key:
        if settings.APP_ENV == "production":
            # Nao deveria chegar aqui — o validator de settings ja bloqueia.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: API_KEY not set in production.",
            )
        logger.warning(
            "API_KEY nao configurada — autenticacao desabilitada (APP_ENV=%s)",
            settings.APP_ENV,
        )
        return

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Comparacao em tempo constante — nao use == diretamente.
    if not hmac.compare_digest(
        api_key.encode("utf-8"),
        configured_key.encode("utf-8"),
    ):
        logger.warning("Invalid API key attempt from unknown caller")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )
