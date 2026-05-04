"""
state.py — Gestion de l'état des téléchargements (SQLite).

Maintient un registre persistant de chaque vidéo connue afin de :
    - Éviter les téléchargements redondants (statut `downloaded`).
    - Marquer les vidéos supprimées de YouTube (statut `deleted`).
    - Enregistrer les échecs pour reprise ultérieure (statut `failed`).
    - Tracer le chemin du fichier vidéo et de la note Obsidian associée.

Schéma SQLite :
    Table `downloads` — une ligne par vidéo, clé primaire = video_id.
    Table `meta`      — version du schéma pour les migrations futures.

La base est stockée dans ~/.config/yt_playlist_dl/state.db par défaut
(configurable via config.toml → state_db_path).

Références :
    - sqlite3 stdlib : https://docs.python.org/3/library/sqlite3.html
    - yt-dlp download archive : https://github.com/yt-dlp/yt-dlp#--download-archive
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Generator, Optional

from .logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1


# ── Statuts ───────────────────────────────────────────────────────────────


class DownloadStatus(str, Enum):
    """États possibles d'une vidéo dans le registre."""

    PENDING = "pending"         # Connue mais pas encore téléchargée
    DOWNLOADED = "downloaded"   # Téléchargée avec succès
    FAILED = "failed"           # Échec lors du dernier essai
    DELETED = "deleted"         # Vidéo supprimée de YouTube
    SKIPPED = "skipped"         # Exclue par une limite (taille, durée…)


# ── Enregistrement de résultat ────────────────────────────────────────────


class DownloadRecord:
    """
    Représente une ligne de la table `downloads`.

    Attributes:
        video_id:       Identifiant YouTube (ex. dQw4w9WgXcQ).
        title:          Titre de la vidéo au moment de l'enregistrement.
        playlist_id:    ID de la playlist source (peut être None).
        url:            URL complète de la vidéo.
        status:         DownloadStatus courant.
        file_path:      Chemin absolu du fichier vidéo sur disque (si téléchargé).
        note_path:      Chemin absolu de la note Obsidian (si créée).
        downloaded_at:  Horodatage ISO 8601 du dernier téléchargement réussi.
        error_msg:      Dernier message d'erreur (si status == FAILED).
    """

    __slots__ = (
        "video_id", "title", "playlist_id", "url", "status",
        "file_path", "note_path", "downloaded_at", "error_msg",
    )

    def __init__(
        self,
        video_id: str,
        title: str = "",
        playlist_id: Optional[str] = None,
        url: str = "",
        status: DownloadStatus = DownloadStatus.PENDING,
        file_path: Optional[str] = None,
        note_path: Optional[str] = None,
        downloaded_at: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        self.video_id = video_id
        self.title = title
        self.playlist_id = playlist_id
        self.url = url
        self.status = DownloadStatus(status) if isinstance(status, str) else status
        self.file_path = file_path
        self.note_path = note_path
        self.downloaded_at = downloaded_at
        self.error_msg = error_msg

    def __repr__(self) -> str:
        return (
            f"DownloadRecord(video_id={self.video_id!r}, "
            f"title={self.title!r}, status={self.status.value!r})"
        )


# ── Gestionnaire d'état ───────────────────────────────────────────────────


class StateManager:
    """
    Interface CRUD autour de la base SQLite d'état.

    Usage recommandé (context manager) :
        with StateManager(db_path) as sm:
            sm.mark_downloaded(video_id, file_path, note_path)

    Attributes:
        db_path:  Chemin vers le fichier SQLite.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.debug(f"StateManager initialisé : {db_path}")

    # ── Contexte ──────────────────────────────────────────────────────────

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(self, *_) -> None:
        pass  # Connexions gérées par _conn()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Fournit une connexion SQLite avec autocommit sur succès."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Initialisation du schéma ──────────────────────────────────────────

    def _init_db(self) -> None:
        """Crée les tables si elles n'existent pas encore."""
        with self._conn() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT OR IGNORE INTO meta VALUES ('schema_version', '{SCHEMA_VERSION}');

                CREATE TABLE IF NOT EXISTS downloads (
                    video_id      TEXT PRIMARY KEY,
                    title         TEXT NOT NULL DEFAULT '',
                    playlist_id   TEXT,
                    url           TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'pending',
                    file_path     TEXT,
                    note_path     TEXT,
                    downloaded_at TEXT,
                    error_msg     TEXT,
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_status     ON downloads(status);
                CREATE INDEX IF NOT EXISTS idx_playlist   ON downloads(playlist_id);
                """
            )

    # ── Lecture ───────────────────────────────────────────────────────────

    def get(self, video_id: str) -> Optional[DownloadRecord]:
        """
        Retourne l'enregistrement d'une vidéo, ou None si inconnue.

        Args:
            video_id:  Identifiant YouTube de la vidéo.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM downloads WHERE video_id = ?", (video_id,)
            ).fetchone()
        if row is None:
            return None
        return DownloadRecord(**{k: v for k, v in dict(row).items() if k != "updated_at"})

    def is_downloaded(self, video_id: str) -> bool:
        """Retourne True si la vidéo a déjà été téléchargée avec succès."""
        record = self.get(video_id)
        return record is not None and record.status == DownloadStatus.DOWNLOADED

    def is_deleted(self, video_id: str) -> bool:
        """Retourne True si la vidéo est marquée comme supprimée sur YouTube."""
        record = self.get(video_id)
        return record is not None and record.status == DownloadStatus.DELETED

    def get_by_status(self, status: DownloadStatus) -> list[DownloadRecord]:
        """Retourne toutes les vidéos ayant un statut donné."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM downloads WHERE status = ? ORDER BY updated_at DESC",
                (status.value,),
            ).fetchall()
        return [DownloadRecord(**{k: v for k, v in dict(r).items() if k != "updated_at"}) for r in rows]

    def stats(self) -> dict[str, int]:
        """Retourne un comptage par statut."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as n FROM downloads GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ── Écriture ──────────────────────────────────────────────────────────

    def _upsert(self, record: DownloadRecord) -> None:
        """Insère ou met à jour un enregistrement."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO downloads
                    (video_id, title, playlist_id, url, status,
                     file_path, note_path, downloaded_at, error_msg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title         = excluded.title,
                    playlist_id   = excluded.playlist_id,
                    url           = excluded.url,
                    status        = excluded.status,
                    file_path     = excluded.file_path,
                    note_path     = excluded.note_path,
                    downloaded_at = excluded.downloaded_at,
                    error_msg     = excluded.error_msg,
                    updated_at    = excluded.updated_at
                """,
                (
                    record.video_id,
                    record.title,
                    record.playlist_id,
                    record.url,
                    record.status.value,
                    record.file_path,
                    record.note_path,
                    record.downloaded_at,
                    record.error_msg,
                    now,
                ),
            )

    def mark_downloaded(
        self,
        video_id: str,
        title: str,
        url: str,
        file_path: str,
        note_path: Optional[str] = None,
        playlist_id: Optional[str] = None,
    ) -> None:
        """Enregistre un téléchargement réussi."""
        now = datetime.now(timezone.utc).isoformat()
        self._upsert(
            DownloadRecord(
                video_id=video_id,
                title=title,
                playlist_id=playlist_id,
                url=url,
                status=DownloadStatus.DOWNLOADED,
                file_path=file_path,
                note_path=note_path,
                downloaded_at=now,
            )
        )
        logger.debug(f"[state] downloaded : {video_id} → {file_path}")

    def mark_failed(
        self,
        video_id: str,
        title: str,
        url: str,
        error_msg: str,
        playlist_id: Optional[str] = None,
    ) -> None:
        """Enregistre un échec de téléchargement."""
        existing = self.get(video_id)
        self._upsert(
            DownloadRecord(
                video_id=video_id,
                title=title,
                playlist_id=playlist_id,
                url=url,
                status=DownloadStatus.FAILED,
                file_path=existing.file_path if existing else None,
                error_msg=error_msg,
            )
        )
        logger.debug(f"[state] failed : {video_id} — {error_msg}")

    def mark_deleted(
        self,
        video_id: str,
        title: str = "Deleted video",
        playlist_id: Optional[str] = None,
    ) -> None:
        """Marque une vidéo comme supprimée de YouTube."""
        url = f"https://www.youtube.com/watch?v={video_id}"
        existing = self.get(video_id)
        self._upsert(
            DownloadRecord(
                video_id=video_id,
                title=title,
                playlist_id=playlist_id,
                url=url,
                status=DownloadStatus.DELETED,
                file_path=existing.file_path if existing else None,
                note_path=existing.note_path if existing else None,
            )
        )
        logger.debug(f"[state] deleted : {video_id}")

    def mark_skipped(
        self,
        video_id: str,
        title: str,
        url: str,
        reason: str = "",
        playlist_id: Optional[str] = None,
    ) -> None:
        """Marque une vidéo comme ignorée (limite taille/durée…)."""
        self._upsert(
            DownloadRecord(
                video_id=video_id,
                title=title,
                playlist_id=playlist_id,
                url=url,
                status=DownloadStatus.SKIPPED,
                error_msg=reason,
            )
        )

    def update_note_path(self, video_id: str, note_path: str) -> None:
        """Met à jour uniquement le chemin de la note Obsidian."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE downloads SET note_path = ?, updated_at = ? WHERE video_id = ?",
                (note_path, now, video_id),
            )
