"""
Email sender usando smtplib + email.mime da biblioteca padrao.
Suporta emails texto puro e HTML renderizados via Jinja2.
"""
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "email"

jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render_template(template_name: str, context: dict[str, Any]) -> str:
    """Renderiza um template Jinja2 e retorna o HTML resultante."""
    template = jinja_env.get_template(template_name)
    return template.render(**context)


def send_email(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> None:
    """
    Envia um email via SMTP com STARTTLS (Gmail por padrao).

    Raises:
        smtplib.SMTPException: em qualquer falha de entrega.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>"
    msg["To"] = f"{to_name} <{to_email}>"

    # Texto simples primeiro — prioridade RFC 2046
    plain = text_body or _html_to_plain(html_body)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ssl_context = ssl.create_default_context()

    logger.info("Connecting to SMTP %s:%s", settings.SMTP_HOST, settings.SMTP_PORT)
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=ssl_context)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAIL_FROM, to_email, msg.as_string())

    logger.info("Email sent to %s subject=%r", to_email, subject)


def _html_to_plain(html: str) -> str:
    """Converte HTML para texto simples removendo as tags."""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
