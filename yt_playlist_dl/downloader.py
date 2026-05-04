"""
downloader.py — Wrapper yt-dlp avec state management et génération de notes Obsidian.

Intègre :
    - StateManager : vérifie si une vidéo est déjà téléchargée avant de lancer yt-dlp.
    - ObsidianNoteWriter : génère la note .md après chaque téléchargement réussi.
    - Barres de progression Rich (par fichier + globale).

Références :
    - yt-dlp Python API : https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp
    - Rich Progress : https://rich.readthedocs.io/en/latest/progress.html
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Callable

import yt_dlp
from rich.console import Console
from rich.progress import (
    BarColumn, DownloadColumn, MofNCompleteColumn, Progress,
    SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn, TransferSpeedColumn,
)
from rich.table import Column

from .config import AppConfig
from .logger import get_logger
from .obsidian import ObsidianNoteWriter, VideoMeta
from .state import DownloadStatus, StateManager
from .youtube import VideoEntry

logger = get_logger(__name__)
console = Console()

BROWSER_CHOICES = ("firefox", "chrome", "chromium", "brave", "edge", "safari", "opera")
DEFAULT_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"


# ── Barres Rich ───────────────────────────────────────────────────────────

def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.fields[filename]}", table_column=Column(ratio=2)),
        BarColumn(bar_width=None, table_column=Column(ratio=1)),
        DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn(),
        console=console, transient=False,
    )

def _make_overall_progress() -> Progress:
    return Progress(
        TextColumn("[bold green]Progression globale"),
        BarColumn(), MofNCompleteColumn(),
        console=console, transient=False,
    )

def _make_progress_hook(
    progress: Progress, task_id: TaskID,
    on_finish: Optional[Callable[[], None]] = None,
) -> Callable[[dict], None]:
    def hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress.update(task_id, completed=downloaded, total=total)
        elif status == "finished":
            total = d.get("total_bytes", 0)
            progress.update(task_id, completed=total, total=total)
            if on_finish:
                on_finish()
        elif status == "error":
            logger.error(f"yt-dlp erreur : {d.get('filename', '?')}")
    return hook

def _short_name(title: str, maxlen: int = 40) -> str:
    title = re.sub(r"[^\w\s\-\(\)\[\]]", "", title).strip()
    return title[:maxlen] + "…" if len(title) > maxlen else title


# ── Résolution du fichier téléchargé ─────────────────────────────────────

def _find_downloaded_file(output_dir: Path, video_id: str) -> Optional[str]:
    """
    Tente de retrouver le fichier vidéo téléchargé par yt-dlp.
    
    Cherche dans output_dir les fichiers contenant le video_id ou
    récemment modifiés. Retourne le chemin absolu ou None.
    """
    patterns = [f"*{video_id}*", "*.mp4", "*.mkv", "*.webm"]
    for pattern in patterns:
        files = sorted(output_dir.rglob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            return str(files[0].resolve())
    return None


# ── Classe principale ─────────────────────────────────────────────────────

class Downloader:
    """
    Téléchargeur intégrant state management, génération de notes Obsidian,
    et barres de progression Rich.

    Attributes:
        config:       Configuration complète de l'application.
        state:        StateManager SQLite.
        note_writer:  ObsidianNoteWriter pour la génération de notes.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        dl = config.download

        self.output_dir = dl.output_dir
        self.browser = dl.browser
        self.max_filesize = dl.max_filesize
        self.max_duration = dl.max_duration
        self.video_format = dl.video_format
        self.embed_metadata = dl.embed_metadata
        self.write_subtitles = dl.write_subtitles

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.state = StateManager(config.state_db_path)
        self.note_writer = ObsidianNoteWriter(config.obsidian)

    # ── Options yt-dlp ────────────────────────────────────────────────────

    def _build_ydl_opts(
        self,
        progress_hooks: list[Callable],
        subfolder: Optional[str] = None,
    ) -> dict:
        dest = self.output_dir / (subfolder or "")
        dest.mkdir(parents=True, exist_ok=True)

        opts: dict = {
            "format": self.video_format,
            "outtmpl": str(dest / "%(playlist_index)s - %(title)s.%(ext)s"),
            "progress_hooks": progress_hooks,
            "quiet": True,
            "no_warnings": False,
            "ignoreerrors": True,
            "retries": 5,
            "fragment_retries": 5,
            "postprocessors": [],
        }

        if self.browser:
            opts["cookiesfrombrowser"] = (self.browser,)
        if self.max_filesize:
            opts["max_filesize"] = self.max_filesize
        if self.max_duration:
            opts["match_filter"] = yt_dlp.utils.match_filter_func(
                f"duration <= {self.max_duration}"
            )
        if self.embed_metadata:
            opts["postprocessors"].extend([
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            ])
            opts["writethumbnail"] = True
        if self.write_subtitles:
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = ["fr", "en"]
            opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

        return opts

    # ── Téléchargement par lot ────────────────────────────────────────────

    def download_batch(
        self,
        videos: list[VideoEntry],
        subfolder: Optional[str] = None,
        force: bool = False,
    ) -> tuple[int, int, int]:
        """
        Télécharge une liste de vidéos avec gestion d'état et notes Obsidian.

        Comportement :
            - Vidéo déjà téléchargée (statut DOWNLOADED) → ignorée sauf si force=True.
            - Vidéo supprimée (is_available=False) → marquée DELETED, ignorée.
            - Succès → marquée DOWNLOADED + note Obsidian créée.
            - Échec → marquée FAILED.

        Args:
            videos:     Liste de VideoEntry à traiter.
            subfolder:  Sous-répertoire de destination.
            force:      Si True, re-télécharge même les vidéos déjà connues.

        Returns:
            Tuple (succès, échecs, ignorées).
        """
        if not videos:
            logger.warning("Liste vide, rien à télécharger.")
            return 0, 0, 0

        successes, failures, skipped = 0, 0, 0
        total = len(videos)

        console.rule(f"[bold green]Traitement de {total} vidéo(s)")

        overall = _make_overall_progress()
        overall_task = overall.add_task("global", total=total)
        overall.start()

        for i, video in enumerate(videos, start=1):
            short = _short_name(video.title)

            # ── Vidéo supprimée ──────────────────────────────────────────
            if not video.is_available:
                self.state.mark_deleted(
                    video_id=video.video_id,
                    title=video.title,
                    playlist_id=video.playlist_id,
                )
                console.print(
                    f"  [dim][{i}/{total}][/dim] [red]✗ Supprimée[/red] : {short}"
                )
                skipped += 1
                overall.update(overall_task, advance=1)
                continue

            # ── Déjà téléchargée ─────────────────────────────────────────
            if not force and self.state.is_downloaded(video.video_id):
                record = self.state.get(video.video_id)
                console.print(
                    f"  [dim][{i}/{total}][/dim] [green]✓ Déjà téléchargée[/green] : {short}"
                )
                logger.info(f"Ignorée (déjà téléchargée) : {video.title}")
                skipped += 1
                overall.update(overall_task, advance=1)
                continue

            # ── Téléchargement ────────────────────────────────────────────
            console.print(f"\n  [dim][{i}/{total}][/dim] [bold]{short}[/bold]")
            logger.info(f"Téléchargement [{i}/{total}] : {video.title} ({video.url})")

            dest_dir = self.output_dir / (subfolder or "")
            success, file_path = self._download_single(video, subfolder)

            if success:
                successes += 1
                note_path = self._write_obsidian_note(video, subfolder, file_path=file_path)
                self.state.mark_downloaded(
                    video_id=video.video_id,
                    title=video.title,
                    url=video.url,
                    file_path=file_path or str(dest_dir),
                    note_path=str(note_path) if note_path else None,
                    playlist_id=video.playlist_id,
                )
                console.print(f"  [green]✓[/green] Téléchargée — note : [dim]{note_path.name if note_path else 'N/A'}[/dim]")
            else:
                failures += 1
                # Créer la note même en cas d'échec avec download=KO
                note_path = self._write_obsidian_note(video, subfolder, file_path=None, failed=True)
                if note_path:
                    console.print(f"  [red]✗[/red] Échec — note créée : [dim]{note_path.name}[/dim]")

            overall.update(overall_task, advance=1)

        overall.stop()
        console.rule(
            f"[bold]Terminé[/bold] — "
            f"[green]{successes} succès[/green] / "
            f"[red]{failures} échec(s)[/red] / "
            f"[dim]{skipped} ignorée(s)[/dim]"
        )
        logger.info(f"Batch terminé : {successes} succès, {failures} échecs, {skipped} ignorées")
        return successes, failures, skipped

    # ── Téléchargement d'une vidéo unique ─────────────────────────────────

    def _download_single(
        self, video: VideoEntry, subfolder: Optional[str]
    ) -> tuple[bool, Optional[str]]:
        """
        Lance yt-dlp pour une vidéo et retourne (succès, chemin_fichier).
        """
        dest_dir = self.output_dir / (subfolder or "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        short = _short_name(video.title)
        file_path: Optional[str] = None

        with _make_progress() as progress:
            task = progress.add_task("dl", total=None, filename=short)
            hook = _make_progress_hook(progress, task)
            opts = self._build_ydl_opts(progress_hooks=[hook], subfolder=subfolder)

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ret = ydl.download([video.url])

                if ret == 0:
                    file_path = _find_downloaded_file(dest_dir, video.video_id)
                    return True, file_path
                else:
                    self.state.mark_failed(
                        video.video_id, video.title, video.url,
                        f"yt-dlp exit code {ret}", video.playlist_id
                    )
                    logger.warning(f"✗ Échec (code {ret}) : {video.title}")
                    return False, None

            except yt_dlp.utils.DownloadError as e:
                self.state.mark_failed(
                    video.video_id, video.title, video.url,
                    str(e), video.playlist_id
                )
                logger.error(f"✗ DownloadError : {video.title} — {e}")
                return False, None

    # ── Génération de note Obsidian ───────────────────────────────────────

    def _write_obsidian_note(
        self, video: VideoEntry, subfolder: Optional[str],
        file_path: Optional[str] = None, failed: bool = False,
    ) -> Optional[Path]:
        """Construit le VideoMeta et délègue à ObsidianNoteWriter."""
        meta = VideoMeta(
            video_id=video.video_id,
            title=video.title,
            author=video.channel_name,
            url=video.url,
            description=video.description,
            published_date=video.published_date_short() or None,
            tags=video.keywords,
            playlist_name=None,
            download_path="KO" if failed else (file_path or ""),
        )
        return self.note_writer.write_note(meta)
    # ── Affichage des statistiques d'état ─────────────────────────────────

    def print_stats(self) -> None:
        """Affiche un résumé des statuts de la base d'état."""
        from rich.table import Table
        stats = self.state.stats()
        table = Table(title="État des téléchargements", header_style="bold cyan")
        table.add_column("Statut")
        table.add_column("Nombre", justify="right")

        colors = {
            "downloaded": "green",
            "failed": "red",
            "deleted": "dim red",
            "skipped": "yellow",
            "pending": "blue",
        }
        for status, count in sorted(stats.items()):
            color = colors.get(status, "white")
            table.add_row(f"[{color}]{status}[/{color}]", str(count))

        console.print(table)
