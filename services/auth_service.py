"""
Web-based Google Drive OAuth2 flow.

For panel deployments (Easypanel, Coolify, Portainer) where terminal access
is limited, this provides a browser-based way to authorize Google Drive.

Flow:
  1. User visits GET /auth/drive → gets redirected to Google consent screen
  2. User authorizes → Google redirects back to /auth/drive/callback
  3. Server exchanges code for token, saves token.json → done

Requirements:
  - client_secret.json must be present in credentials/ volume
  - For hosted panels: OAuth client must be "Web application" type
    with redirect URI: {APP_BASE_URL}/auth/drive/callback
  - For local Docker: OAuth client can be "Desktop app" type
    (use scripts/auth_drive.py instead)
"""

import json
import logging
import os

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from core.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_client_secret_path() -> str:
    return settings.google_drive_client_secret_json


def _get_token_path() -> str:
    return settings.google_drive_token_json


def is_drive_authorized() -> bool:
    """Check if a valid Drive token exists."""
    token_path = _get_token_path()
    if not os.path.exists(token_path):
        return False
    try:
        with open(token_path) as f:
            data = json.load(f)
        return bool(data.get("refresh_token"))
    except Exception:
        return False


def get_auth_url() -> str:
    """Generate the Google OAuth2 authorization URL."""
    client_secret = _get_client_secret_path()
    if not os.path.exists(client_secret):
        raise FileNotFoundError(
            "client_secret.json not found. Upload it to the credentials volume."
        )

    redirect_uri = f"{settings.app_base_url.rstrip('/')}/auth/drive/callback"

    flow = Flow.from_client_secrets_file(
        client_secret,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    return auth_url


def exchange_code(code: str) -> dict:
    """Exchange the authorization code for credentials and save token.json."""
    client_secret = _get_client_secret_path()
    redirect_uri = f"{settings.app_base_url.rstrip('/')}/auth/drive/callback"

    flow = Flow.from_client_secrets_file(
        client_secret,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

    flow.fetch_token(code=code)
    creds = flow.credentials

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }

    token_path = _get_token_path()
    os.makedirs(os.path.dirname(token_path), exist_ok=True)

    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)

    logger.info(f"Drive token saved to {token_path}")
    return {"status": "authorized", "token_path": token_path}
