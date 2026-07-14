"""Email alerting for Bid Analyzer runs.

Sends a single HTML summary email per run (success, completed-with-errors, or
failure). Uses only the Python standard library (smtplib + email) so no extra
dependencies are needed.

Non-secret settings live in the "notifications" block of project_config.json
(exposed as config.NOTIFICATIONS). Optional SMTP credentials (for relays that
require auth) live in the gitignored credentials/smtp_credentials.json — omit
the file entirely for an unauthenticated internal relay.

send_alert() never raises: a broken mailer must not bring down an analysis run.
"""
import json
import logging
import os
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_CRED_PATH = os.path.join(PROJECT_ROOT, 'credentials', 'smtp_credentials.json')


def _load_credentials():
    """Return {'username', 'password'} from the gitignored credentials file, or {}."""
    if not os.path.exists(_CRED_PATH):
        return {}
    try:
        with open(_CRED_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read SMTP credentials at {_CRED_PATH}: {e}")
        return {}


def send_alert(subject, body_html, cfg):
    """Send an HTML alert email.

    cfg is the "notifications" dict from project_config.json. Returns True if the
    message was handed to the SMTP server, False otherwise. Never raises.
    """
    if not cfg or not cfg.get('enabled', False):
        logger.info("Email notifications disabled; skipping alert.")
        return False

    host       = cfg.get('smtp_host')
    port       = int(cfg.get('smtp_port', 25))
    from_addr  = cfg.get('from_address')
    recipients = cfg.get('recipients', [])
    use_ssl      = cfg.get('use_ssl', False)
    use_starttls = cfg.get('use_starttls', False)

    if not host or not from_addr or not recipients:
        logger.warning(
            "Notification config incomplete (need smtp_host, from_address, recipients); "
            "skipping alert."
        )
        return False

    creds = _load_credentials()
    username = creds.get('username')
    password = creds.get('password')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = from_addr
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    try:
        context = ssl.create_default_context()
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if use_starttls and not use_ssl:
                server.starttls(context=context)
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, recipients, msg.as_string())
        logger.info(f"Alert email sent to {', '.join(recipients)}")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}", exc_info=True)
        return False
