"""
auth.py — Google OAuth2 authentication with keyring support.

Secret resolution priority:
    1. System keyring  (keyring.get_password "yt_playlist_dl" / "client_secrets")
    2. YT_DL_SECRETS  environment variable (path to client_secrets.json)
    3. client_secrets.json at project root

Token cache:
    Stored at CONFIG_DIR / "token.json" (chmod 600).

Usage:
    creds = get_credentials()
    revoke_credentials()
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

# OAuth2 scopes required
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

# Default paths
_DEFAULT_SECRETS = CONFIG_DIR / "client_secrets.json"
_TOKEN_PATH      = CONFIG_DIR / "token.json"


# ── Public API ────────────────────────────────────────────────────────────


def get_credentials(client_secrets_path: Optional[Path] = None) -> Credentials:
    """
    Return valid Google OAuth2 credentials, refreshing or re-authenticating as needed.

    Secret resolution order:
        1. System keyring
        2. YT_DL_SECRETS environment variable
        3. client_secrets_path argument
        4. Default: CONFIG_DIR / client_secrets.json

    Args:
        client_secrets_path: Optional explicit path to client_secrets.json.

    Returns:
        Valid google.oauth2.credentials.Credentials.

    Raises:
        FileNotFoundError: If no client secrets can be found.
    """
    creds = _load_token()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            logger.info("OAuth2 token refreshed")
            return creds
        except Exception as e:
            logger.warning(f"Token refresh failed: {e} — re-authenticating")
            _TOKEN_PATH.unlink(missing_ok=True)

    # Resolve client secrets
    secrets_path = _resolve_secrets(client_secrets_path)

    # Run OAuth2 flow
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    logger.info(f"New OAuth2 token saved: {_TOKEN_PATH}")

    # Clean up temp file if created from keyring
    if secrets_path.name.startswith("_yt_dl_secrets_"):
        secrets_path.unlink(missing_ok=True)

    return creds


def revoke_credentials() -> None:
    """Revoke and delete cached token."""
    if _TOKEN_PATH.exists():
        _TOKEN_PATH.unlink()
        logger.info("OAuth2 token deleted")
    else:
        logger.info("No cached token found")


# ── Internal helpers ──────────────────────────────────────────────────────


def _resolve_secrets(explicit: Optional[Path]) -> Path:
    """
    Resolve client secrets following the priority chain.

    Returns a Path to a readable JSON file (may be a temp file if from keyring).
    """
    # 1. System keyring
    path = _try_keyring()
    if path:
        return path

    # 2. Environment variable
    env_path = os.environ.get("YT_DL_SECRETS")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            logger.debug(f"Client secrets from YT_DL_SECRETS: {p}")
            return p
        logger.warning(f"YT_DL_SECRETS points to non-existent file: {p}")

    # 3. Explicit argument
    if explicit and explicit.exists():
        logger.debug(f"Client secrets from argument: {explicit}")
        return explicit

    # 4. Default location
    if _DEFAULT_SECRETS.exists():
        logger.debug(f"Client secrets from default path: {_DEFAULT_SECRETS}")
        return _DEFAULT_SECRETS

    raise FileNotFoundError(
        "No client secrets found.\n"
        "Options:\n"
        "  1. Store in keyring:       python3 -m yt_playlist_dl.store_secret\n"
        "  2. Set env variable:       export YT_DL_SECRETS=/path/to/client_secrets.json\n"
        f"  3. Place file at:          {_DEFAULT_SECRETS}"
    )


def _try_keyring() -> Optional[Path]:
    """Try to load client secrets from system keyring. Returns temp Path or None."""
    try:
        import keyring
        secret_json = keyring.get_password("yt_playlist_dl", "client_secrets")
        if not secret_json:
            return None
        # Validate JSON
        json.loads(secret_json)
        # Write to temp file for google-auth-oauthlib
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="_yt_dl_secrets_",
            delete=False,
            dir=tempfile.gettempdir(),
        )
        tmp.write(secret_json)
        tmp.flush()
        tmp.close()
        logger.debug("Client secrets loaded from system keyring")
        return Path(tmp.name)
    except ImportError:
        logger.debug("keyring not installed — skipping")
        return None
    except Exception as e:
        logger.debug(f"Keyring read failed: {e}")
        return None


def _load_token() -> Optional[Credentials]:
    """Load cached token from disk."""
    if not _TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)
        return creds
    except Exception as e:
        logger.warning(f"Corrupted token, deleting: {e}")
        _TOKEN_PATH.unlink(missing_ok=True)
        return None


def _save_token(creds: Credentials) -> None:
    """Persist token to disk with restricted permissions."""
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(creds.to_json())
    _TOKEN_PATH.chmod(0o600)
