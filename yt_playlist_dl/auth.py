"""
auth.py — Gestion des credentials OAuth2 Google.

Implémente le flux « Installed Application » (RFC 6749 §4.1).
Les tokens sont persistés dans ~/.config/yt_playlist_dl/token.json (chmod 600).
"""

import os
import stat
from pathlib import Path
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .logger import get_logger

logger = get_logger(__name__)

SCOPES: list[str] = ["https://www.googleapis.com/auth/youtube.readonly"]

CONFIG_DIR: Path = Path(__file__).parent.parent  # racine du projet
TOKEN_PATH: Path = CONFIG_DIR / "token.json"
DEFAULT_SECRETS_PATH: Path = CONFIG_DIR / "client_secrets.json"


def _secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def get_credentials(
    client_secrets_path: Optional[Path] = None,
    force_reauth: bool = False,
) -> Credentials:
    if client_secrets_path is None:
        client_secrets_path = DEFAULT_SECRETS_PATH

    creds: Optional[Credentials] = None

    if force_reauth and TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        logger.info("Token existant supprimé (--reauth demandé)")

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except (ValueError, KeyError) as e:
            logger.warning(f"Token corrompu, suppression : {e}")
            TOKEN_PATH.unlink(missing_ok=True)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _secure_write(TOKEN_PATH, creds.to_json())
            logger.info("Token OAuth2 rafraîchi")
        except RefreshError as e:
            logger.warning(f"Rafraîchissement échoué : {e}")
            TOKEN_PATH.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if not client_secrets_path.exists():
            raise FileNotFoundError(
                f"\n[!] client_secrets.json introuvable : {client_secrets_path}\n"
                "    → Créez un projet sur https://console.cloud.google.com\n"
                "    → Activez l'API YouTube Data v3\n"
                "    → Credentials → OAuth client ID → Desktop app\n"
                "    → Téléchargez le JSON et placez-le à l'emplacement ci-dessus"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        _secure_write(TOKEN_PATH, creds.to_json())
        logger.info(f"Token OAuth2 sauvegardé : {TOKEN_PATH}")

    return creds


def revoke_credentials() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        logger.info("Credentials révoqués.")
    else:
        logger.info("Aucun credential en cache.")
