"""
cli.py — Interface CLI principale (Typer).

Commandes :
    app interactive           Mode guidé complet (défaut si aucune commande)
    app playlists             Liste les playlists du compte
    app stats                 Affiche les statistiques d'état (SQLite)
    app revoke                Supprime les credentials OAuth2
    app download playlist     Téléchargement de playlists
    app download video        Téléchargement d'une URL isolée

Options globales (disponibles sur toutes les commandes) :
    --config   Chemin vers config.toml
    --log-level DEBUG|INFO|WARNING|ERROR
    --secrets  Chemin vers client_secrets.json

Références :
    - Typer documentation : https://typer.tiangolo.com/
    - questionary : https://questionary.readthedocs.io/
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import questionary
import typer
from googleapiclient.errors import HttpError
from rich.console import Console
from rich.table import Table

from .auth import get_credentials, revoke_credentials as _revoke
from .i18n import t, set_lang
from .config import load_config, CONFIG_DIR, AppConfig
from .downloader import BROWSER_CHOICES, Downloader
from .logger import get_logger, setup_logging
from .youtube import YouTubeClient

console = Console()
logger = get_logger(__name__)
LOG_DIR = CONFIG_DIR / "logs"

# ── Application Typer ─────────────────────────────────────────────────────

app = typer.Typer(
    name="yt-playlist-dl",
    help="Téléchargeur de playlists YouTube — OAuth2 + yt-dlp + Obsidian",
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)
download_app = typer.Typer(help="Commandes de téléchargement.")
app.add_typer(download_app, name="download")

# ── Types Typer annotés ────────────────────────────────────────────────────

ConfigOpt  = Annotated[Optional[Path], typer.Option("--config",  "-c", help="Chemin vers config.toml")]
LogOpt     = Annotated[str,            typer.Option("--log-level","-l", help="DEBUG|INFO|WARNING|ERROR", envvar="YT_DL_LOG_LEVEL")]
SecretsOpt = Annotated[Optional[Path], typer.Option("--secrets", "-s", help="Chemin vers client_secrets.json")]

OutputOpt  = Annotated[Optional[Path], typer.Option("--output",  "-o", help="Répertoire de destination (prioritaire sur config.toml)")]
BrowserOpt = Annotated[Optional[str],  typer.Option("--browser", "-b", help=f"Cookies navigateur : {', '.join(BROWSER_CHOICES)}")]
MaxVidOpt  = Annotated[Optional[int],  typer.Option("--max-videos", "-n", help="Limite nombre de vidéos", min=1)]
MaxSzOpt   = Annotated[Optional[str],  typer.Option("--max-size", help="Taille max/fichier (ex: 500m, 2g)")]
MaxDurOpt  = Annotated[Optional[int],  typer.Option("--max-duration", help="Durée max/vidéo en secondes", min=1)]
SubsOpt    = Annotated[bool,           typer.Option("--subtitles", help="Download subtitles automatically")]
ForceOpt   = Annotated[bool,           typer.Option("--force", "-f", help="Re-télécharger même les vidéos déjà connues")]
EnrichOpt  = Annotated[bool,           typer.Option("--enrich/--no-enrich", help="Enrich metadata via YouTube API (for Obsidian notes)")]


# ── Callback racine ───────────────────────────────────────────────────────

@app.callback(invoke_without_command=True)
def root_callback(
    ctx: typer.Context,
    config: ConfigOpt = None,
    log_level: LogOpt = "INFO",
    secrets: SecretsOpt = None,
) -> None:
    """Lance le mode interactif si aucune sous-commande n'est fournie."""
    setup_logging(LOG_DIR, level=log_level)
    cfg = load_config(config)
    set_lang(cfg.lang)

    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg
    ctx.obj["secrets"] = secrets

    if ctx.invoked_subcommand is None:
        _interactive_mode(cfg, secrets_path=secrets)


# ── Commande : playlists ──────────────────────────────────────────────────

@app.command("playlists")
def cmd_list_playlists(ctx: typer.Context) -> None:
    """Affiche toutes les playlists du compte YouTube authentifié."""
    cfg, secrets = _ctx(ctx)
    yt = YouTubeClient(_auth(secrets))
    with console.status("[cyan]Fetching playlists…"):
        try:
            playlists = yt.get_playlists()
        except HttpError as e:
            console.print(f"[red]API error: {e.reason}")
            raise typer.Exit(1)
    _print_playlists_table(playlists)


# ── Commande : stats ──────────────────────────────────────────────────────

@app.command("stats")
def cmd_stats(ctx: typer.Context) -> None:
    """Affiche les statistiques de la base d'état (vidéos téléchargées, deleted…)."""
    cfg, _ = _ctx(ctx)
    dl = Downloader(cfg)
    dl.print_stats()


# ── Commande : revoke ─────────────────────────────────────────────────────

@app.command("resources")
def cmd_resources(
    ctx: typer.Context,
    status_filter: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filtrer par statut : downloaded | failed | deleted | skipped | all (défaut: all)"),
    ] = "all",
    retry: Annotated[bool, typer.Option("--retry", help="Proposer de re-télécharger les NOK après affichage.")] = False,
) -> None:
    """Affiche l'état de toutes les ressources connues."""
    cfg, secrets = _ctx(ctx)
    from .state import StateManager, DownloadStatus
    from rich.table import Table
    from rich.text import Text
    import datetime

    sm = StateManager(cfg.state_db_path)

    # Récupérer les enregistrements selon le filtre
    if status_filter == "all" or status_filter is None:
        records = []
        for s in DownloadStatus:
            records.extend(sm.get_by_status(s))
    else:
        try:
            records = sm.get_by_status(DownloadStatus(status_filter))
        except ValueError:
            console.print(f"[red]Unknown status : {status_filter}. Values: downloaded, failed, deleted, skipped")
            raise typer.Exit(1)

    if not records:
        console.print("[yellow]No resource found.")
        raise typer.Exit(0)

    # Trier : failed/deleted en premier, puis par titre
    order = {"failed": 0, "deleted": 1, "skipped": 2, "downloaded": 3, "pending": 4}
    records.sort(key=lambda r: (order.get(r.status.value, 9), (r.title or "").lower()))

    # Afficher le tableau
    table = Table(
        title=f"Resources ({len(records)} entrée(s))",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Status", width=12)
    table.add_column("Title", min_width=30, max_width=50, no_wrap=True)
    table.add_column("Author / Channel", max_width=20, no_wrap=True)
    table.add_column("DL Date", width=12)
    table.add_column("File / Info", max_width=40, no_wrap=True)

    status_style = {
        "downloaded": "[green]✓ OK[/green]",
        "failed":     "[red]✗ NOK[/red]",
        "deleted":    "[dim red]✗ Deleted[/dim red]",
        "skipped":    "[yellow]⚠ Skipped[/yellow]",
        "pending":    "[blue]⏳ Pending[/blue]",
    }

    for r in records:
        st = status_style.get(r.status.value, r.status.value)
        title = (r.title or r.video_id or "")[:50]
        author = ""
        date_dl = (r.downloaded_at or "")[:10] if r.downloaded_at else "—"
        file_info = ""

        if r.status.value == "downloaded":
            if r.file_path:
                from pathlib import Path as _P
                p = _P(r.file_path)
                file_info = p.name[:40] if p.exists() else f"[dim]not found[/dim]"
            else:
                file_info = "—"
        elif r.status.value == "failed":
            file_info = f"[red]{(r.error_msg or '')[:40]}[/red]"
        elif r.status.value == "deleted":
            file_info = r.url or ""

        table.add_row(st, title, author, date_dl, file_info)

    console.print(table)

    # Résumé
    from collections import Counter
    counts = Counter(r.status.value for r in records)
    ok_c = counts.get("downloaded", 0)
    nok_c = counts.get("failed", 0)
    skip_c = counts.get("skipped", 0)
    del_c = counts.get("deleted", 0)
    console.print(f"  [green]✓ {ok_c} OK[/green]  [red]✗ {nok_c} NOK[/red]  [yellow]⚠ {skip_c} skipped[/yellow]  [dim red]✗ {del_c} deleted[/dim red]")

    # Proposer de re-télécharger les NOK
    nok = [r for r in records if r.status.value in ("failed", "skipped")]
    if nok and (retry or questionary.confirm(f"Re-download the {len(nok)} NOK resource(s)?", default=False).ask()):
        choices = [
            questionary.Choice(
                title=f"[{r.status.value}] {r.title or r.video_id}",
                value=r,
            )
            for r in nok
        ]
        selected = questionary.checkbox(
            "Select resources to re-download:",
            choices=choices,
        ).ask()

        if selected:
            from .youtube import VideoEntry
            creds_r = _auth(secrets)
            from .youtube import YouTubeClient
            yt_r = YouTubeClient(creds_r)
            dl_r = Downloader(cfg)
            videos_r = [
                VideoEntry(
                    video_id=r.video_id,
                    title=r.title or r.video_id,
                    playlist_id=r.playlist_id or "",
                    position=i,
                )
                for i, r in enumerate(selected)
            ]
            with console.status("[cyan]Enriching metadata…"):
                yt_r.get_video_details(videos_r)
            dl_r.download_batch(videos_r, force=True)


@app.command("revoke")
def cmd_revoke(ctx: typer.Context) -> None:
    """Révoque et supprime les credentials OAuth2 en cache."""
    if questionary.confirm(t("prompt.revoke_confirm"), default=False).ask():
        _revoke()
        console.print("[green]✓ Credentials deleted.")
    else:
        console.print("[dim]Cancelled.")


# ── download playlist ─────────────────────────────────────────────────────

@download_app.command("playlist")
def cmd_download_playlist(
    ctx: typer.Context,
    playlist_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="ID(s) de playlist. Si omis : sélection interactive."),
    ] = None,
    output: OutputOpt = None,
    browser: BrowserOpt = None,
    max_videos: MaxVidOpt = None,
    max_size: MaxSzOpt = None,
    max_duration: MaxDurOpt = None,
    subtitles: SubsOpt = False,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Télécharge une ou plusieurs playlists YouTube."""
    cfg, secrets = _ctx(ctx)
    cfg = _apply_cli_overrides(cfg, output, browser, max_size, max_duration, subtitles)

    yt = YouTubeClient(_auth(secrets))
    dl = Downloader(cfg)

    # Sélection interactive si aucun ID fourni
    if not playlist_ids:
        with console.status("[cyan]Fetching playlists…"):
            playlists = yt.get_playlists()
        if not playlists:
            console.print("[yellow]No playlist found.")
            raise typer.Exit(0)
        choices = [
            questionary.Choice(f"{p.title} ({p.video_count} vidéos)", value=p.id)
            for p in playlists
        ]
        playlist_ids = questionary.checkbox(t("prompt.select_playlists"), choices=choices).ask()
        if not playlist_ids:
            console.print("[dim]No selection.")
            raise typer.Exit(0)

    # Résolution des noms pour les sous-dossiers
    with console.status("[cyan]Fetching playlist names…"):
        all_playlists = yt.get_playlists()
    pl_map = {p.id: p for p in all_playlists}

    total_ok, total_fail, total_skip = 0, 0, 0

    for pid in playlist_ids:
        pl = pl_map.get(pid)
        subfolder = _sanitize(pl.title if pl else pid)
        console.rule(f"[bold cyan]{subfolder}")

        with console.status(f"[cyan]Fetching videos…"):
            try:
                videos = yt.get_playlist_videos(pid, max_videos=max_videos)
            except HttpError as e:
                console.print(f"[red]API error: {e.reason}")
                continue

        if enrich:
            with console.status("[cyan]Enriching metadata…"):
                yt.get_video_details(videos)

        ok, fail, skip = dl.download_batch(videos, subfolder=subfolder, force=force)
        total_ok += ok; total_fail += fail; total_skip += skip

    console.rule(
        f"[bold]Bilan[/bold] — "
        f"[green]{total_ok} succès[/green] / "
        f"[red]{total_fail} échec(s)[/red] / "
        f"[dim]{total_skip} skipped[/dim]"
    )


# ── download video ────────────────────────────────────────────────────────

@download_app.command("video")
def cmd_download_video(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="URL de la vidéo YouTube.")],
    output: OutputOpt = None,
    max_size: MaxSzOpt = None,
    max_duration: MaxDurOpt = None,
    subtitles: SubsOpt = False,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Télécharge une vidéo YouTube à partir de son URL."""
    cfg, secrets = _ctx(ctx)
    cfg = _apply_cli_overrides(cfg, output, None, max_size, max_duration, subtitles)
    creds = _auth(secrets)

    import re
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    video_id = m.group(1) if m else url

    dl = Downloader(cfg)
    if not force and dl.state.is_downloaded(video_id):
        console.print(f"[green]✓ Vidéo déjà téléchargée (ID : {video_id}). Utiliser --force pour re-télécharger.")
        raise typer.Exit(0)

    from .youtube import VideoEntry, YouTubeClient
    yt = YouTubeClient(creds)
    video = VideoEntry(video_id=video_id, title=video_id, playlist_id="", position=0)
    if enrich:
        with console.status("[cyan]Fetching metadata…"):
            yt.get_video_details([video])
    ok, _, _ = dl.download_batch([video], force=force)
    raise typer.Exit(0 if ok else 1)


@download_app.command("article")
def cmd_download_article(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="URL de l'article web.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Destination folder.")] = None,
) -> None:
    """Télécharge un article web et crée une note Obsidian."""
    cfg, _ = _ctx(ctx)
    from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
    output_dir = output.expanduser() if output else cfg.download.article_output_dir

    with console.status("[cyan]Extracting metadata…"):
        meta = ArticleExtractor().extract(url)

    console.print(f"[green]✓ Title   :[/green] {meta.title}")
    console.print(f"[green]✓ Auteur  :[/green] {meta.author_str}")
    console.print(f"[green]✓ Date    :[/green] {meta.published or 'non trouvée'}")

    folder = f"{meta.site_name} - {meta.title}" if meta.title else url
    with console.status("[cyan]Downloading page…"):
        saved = ArticleDownloader().download(url, output_dir, folder)
    if saved:
        console.print(f"[green]✓ Page    :[/green] {saved}")

    note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
    if note:
        console.print(f"[green]✓ Note    :[/green] {note.name}")


@download_app.command("urlfile")
def cmd_download_urlfile(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="File texte contenant les URLs (une par ligne).")],
    output_video: Annotated[Optional[Path], typer.Option("--output-video", "-v", help="Dossier pour les vidéos.")] = None,
    output_article: Annotated[Optional[Path], typer.Option("--output-article", "-a", help="Dossier pour les articles.")] = None,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Télécharge toutes les URLs d'un fichier (vidéos YouTube + articles web)."""
    cfg, secrets = _ctx(ctx)

    if not file.exists():
        console.print(f"[red]File not found: {file}")
        raise typer.Exit(1)

    lines_raw = file.read_text(encoding="utf-8").splitlines()
    urls = [l.strip() for l in lines_raw if l.strip() and not l.strip().startswith("#")]

    if not urls:
        console.print("[yellow]No URL found.")
        raise typer.Exit(0)

    import re as _re
    yt_pattern = _re.compile(r"youtube\.com|youtu\.be")
    yt_urls  = [u for u in urls if yt_pattern.search(u)]
    art_urls = [u for u in urls if not yt_pattern.search(u)]
    console.print(f"[cyan]{len(urls)} URL(s) : [green]{len(yt_urls)} vidéo(s)[/green] / [blue]{len(art_urls)} article(s)[/blue]")

    if yt_urls:
        import copy
        cfg_yt = copy.deepcopy(cfg)
        if output_video:
            cfg_yt.download.output_dir = output_video.expanduser()
        creds_f = _auth(secrets)
        from .youtube import VideoEntry, YouTubeClient
        yt_f = YouTubeClient(creds_f)
        dl_yt = Downloader(cfg_yt)
        videos_f = []
        for i, u in enumerate(yt_urls):
            m = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
            vid_id = m.group(1) if m else u
            videos_f.append(VideoEntry(video_id=vid_id, title=vid_id, playlist_id="", position=i))
        if enrich:
            with console.status("[cyan]Enriching…"):
                yt_f.get_video_details(videos_f)
        dl_yt.download_batch(videos_f, subfolder=None, force=force)

    if art_urls:
        from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
        out_art = output_article.expanduser() if output_article else cfg.download.article_output_dir
        for i, url in enumerate(art_urls, 1):
            console.rule(f"[cyan]Article {i}/{len(art_urls)}")
            with console.status(f"[cyan]{url[:60]}…"):
                meta = ArticleExtractor().extract(url)
            console.print(f"  [green]✓[/green] {meta.title or url}")
            folder = f"{meta.site_name} - {meta.title}" if meta.title else url
            saved = ArticleDownloader().download(url, out_art, folder)
            if saved:
                console.print(f"  [green]✓ Page :[/green] {saved.name}")
            note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
            if note:
                console.print(f"  [green]✓ Note :[/green] {note.name}")


# ── Mode interactif ───────────────────────────────────────────────────────

def _interactive_mode(cfg: AppConfig, secrets_path: Optional[Path] = None) -> None:
    banner = """[bold cyan]
 ██╗   ██╗████████╗      ██████╗ ██╗      ██████╗ ██╗   ██╗██╗     ██╗███████╗████████╗
 ╚██╗ ██╔╝╚══██╔══╝      ██╔══██╗██║     ██╔════╝ ██║   ██║██║     ██║██╔════╝╚══██╔══╝
  ╚████╔╝    ██║   █████╗ ██████╔╝██║     ██║  ███╗██║   ██║██║     ██║███████╗   ██║
   ╚██╔╝     ██║   ╚════╝ ██╔═══╝ ██║     ██║   ██║╚██╗ ██╔╝██║     ██║╚════██║   ██║
    ██║       ██║          ██║     ███████╗╚██████╔╝ ╚████╔╝ ███████╗██║███████║   ██║
    ╚═╝       ╚═╝          ╚═╝     ╚══════╝ ╚═════╝   ╚═══╝  ╚══════╝╚═╝╚══════╝   ╚═╝
[/bold cyan][dim]          YouTube playlist downloader · OAuth2 · yt-dlp · Obsidian[/dim]
"""
    console.print(banner)
    console.print(f"[dim]  Config: {CONFIG_DIR / 'config.toml'}[/dim]\n")

    action = questionary.select(
        t("menu.what_to_do"),
        choices=[
            questionary.Choice(t("menu.download_playlist"),           value="playlist"),
            questionary.Choice(t("menu.download_video"),        value="video"),
            questionary.Choice(t("menu.download_article"),   value="article"),
            questionary.Choice(t("menu.download_urlfile"),  value="urlfile"),
            questionary.Choice(t("menu.retry"),  value="retry"),
            questionary.Choice(t("menu.list_playlists"),               value="list"),
            questionary.Choice(t("menu.stats"),   value="stats"),
            questionary.Choice(t("menu.resources"),                 value="resources"),
            questionary.Choice(t("menu.revoke"),           value="revoke"),
            questionary.Choice(t("menu.quit"),                            value="quit"),
        ],
    ).ask()

    if not action or action == "quit":
        console.print("[dim]Goodbye.")
        sys.exit(0)

    if action == "revoke":
        _revoke(); return
    if action == "stats":
        Downloader(cfg).print_stats(); return
    if action == "resources":
        # Appel direct de la commande resources
        from .state import StateManager, DownloadStatus
        from rich.table import Table
        from collections import Counter

        sm = StateManager(cfg.state_db_path)
        records = []
        for s in DownloadStatus:
            records.extend(sm.get_by_status(s))

        if not records:
            console.print("[yellow]No resource found."); return

        order = {"failed": 0, "deleted": 1, "skipped": 2, "downloaded": 3, "pending": 4}
        records.sort(key=lambda r: (order.get(r.status.value, 9), (r.title or "").lower()))

        table = Table(title=f"Resources ({len(records)})", header_style="bold cyan", show_lines=False)
        table.add_column("Status", width=12)
        table.add_column("Title", min_width=30, max_width=50, no_wrap=True)
        table.add_column("DL Date", width=12)
        table.add_column("File", max_width=45, no_wrap=True)

        status_style = {
            "downloaded": "[green]✓ OK[/green]",
            "failed":     "[red]✗ NOK[/red]",
            "deleted":    "[dim red]✗ Deleted[/dim red]",
            "skipped":    "[yellow]⚠ Skipped[/yellow]",
            "pending":    "[blue]⏳ Pending[/blue]",
        }
        for r in records:
            st = status_style.get(r.status.value, r.status.value)
            date_dl = (r.downloaded_at or "")[:10] or "—"
            if r.status.value == "downloaded" and r.file_path:
                from pathlib import Path as _P
                p = _P(r.file_path)
                file_info = p.name[:45] if p.exists() else "[dim]not found[/dim]"
            elif r.status.value == "failed":
                file_info = f"[red]{(r.error_msg or '')[:45]}[/red]"
            else:
                file_info = "—"
            table.add_row(st, (r.title or r.video_id or "")[:50], date_dl, file_info)

        console.print(table)
        counts = Counter(r.status.value for r in records)
        ok_n=counts.get("downloaded",0); nok_n=counts.get("failed",0); skip_n=counts.get("skipped",0)
        console.print(f"  [green]✓ {ok_n} OK[/green]  [red]✗ {nok_n} NOK[/red]  [yellow]⚠ {skip_n} skipped[/yellow]")

        nok = [r for r in records if r.status.value in ("failed", "skipped")]
        if nok and questionary.confirm(f"Re-download the {len(nok)} NOK?", default=False).ask():
            selected = questionary.checkbox(
                t("prompt.select_resources"),
                choices=[questionary.Choice(f"[{r.status.value}] {r.title or r.video_id}", value=r) for r in nok],
            ).ask()
            if selected:
                from .youtube import VideoEntry
                creds_r = _auth(secrets_path)
                from .youtube import YouTubeClient
                yt_r = YouTubeClient(creds_r)
                dl_r = Downloader(cfg)
                videos_r = [VideoEntry(video_id=r.video_id, title=r.title or r.video_id, playlist_id=r.playlist_id or "", position=i) for i, r in enumerate(selected)]
                with console.status("[cyan]Enriching…"):
                    yt_r.get_video_details(videos_r)
                dl_r.download_batch(videos_r, force=True)
        return
    if action == "retry":
        from .state import DownloadStatus
        dl_tmp = Downloader(cfg)
        candidates = []
        for status in (DownloadStatus.DOWNLOADED, DownloadStatus.FAILED, DownloadStatus.SKIPPED):
            candidates.extend(dl_tmp.state.get_by_status(status))
        if not candidates:
            console.print("[yellow]No video in database."); return
        candidates.sort(key=lambda r: r.title.lower())
        selected = questionary.checkbox(
            t("prompt.select_retry"),
            choices=[
                questionary.Choice(f"[{r.status}] {r.title or r.video_id}", value=r)
                for r in candidates
            ],
        ).ask()
        if not selected:
            console.print("[dim]No selection."); return
        from .youtube import VideoEntry
        videos = [
            VideoEntry(video_id=r.video_id, title=r.title or r.video_id,
                       playlist_id=r.playlist_id or "", position=i)
            for i, r in enumerate(selected)
        ]
        dl_tmp.download_batch(videos, force=True)
        return

    creds = _auth(secrets_path)
    yt = YouTubeClient(creds)

    if action == "list":
        with console.status("[cyan]Fetching…"):
            _print_playlists_table(yt.get_playlists())
        return

    if action == "article":
        from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
        url_article = questionary.text(t("prompt.article_url")).ask()
        if not url_article:
            return
        output_str_art = questionary.text(
            t("prompt.output_html"),
            default=str(cfg.download.article_output_dir),
        ).ask()
        output_dir = Path(output_str_art).expanduser()
        with console.status("[cyan]Extracting metadata…"):
            meta = ArticleExtractor().extract(url_article)
        console.print(f"[green]✓ Title   :[/green] {meta.title}")
        console.print(f"[green]✓ Auteur  :[/green] {meta.author_str}")
        console.print(f"[green]✓ Date    :[/green] {meta.published or 'non trouvée'}")
        console.print(f"[green]✓ Site    :[/green] {meta.site_name}")
        folder_name = f"{meta.site_name} - {meta.title}" if meta.title else url_article
        with console.status("[cyan]Downloading page and images…"):
            saved_dir = ArticleDownloader().download(url_article, output_dir, folder_name)
        if saved_dir:
            console.print(f"[green]✓ Page sauvegardée :[/green] {saved_dir}")
        else:
            console.print("[red]✗ Échec du téléchargement.")
        note_path = ArticleNoteWriter(cfg.obsidian).write_note(meta)
        if note_path:
            console.print(f"[green]✓ Note Obsidian    :[/green] {note_path.name}")
        return


    if action == "urlfile":
        file_path_str = questionary.text(t("prompt.urlfile_path"), default="~/urls.txt").ask()
        if not file_path_str:
            return

        file_path = Path(file_path_str).expanduser()
        if not file_path.exists():
            console.print(f"[red]File not found: {file_path}")
            return

        lines_raw = file_path.read_text(encoding="utf-8").splitlines()
        urls = [l.strip() for l in lines_raw if l.strip() and not l.strip().startswith("#")]

        if not urls:
            console.print("[yellow]No URL found in file.")
            return

        import re as _re
        yt_pattern = _re.compile(r"youtube\.com|youtu\.be")
        yt_urls  = [u for u in urls if yt_pattern.search(u)]
        art_urls = [u for u in urls if not yt_pattern.search(u)]

        console.print(f"[cyan]{len(urls)} URL(s) : [green]{len(yt_urls)} vidéo(s)[/green] / [blue]{len(art_urls)} article(s)[/blue]")

        if yt_urls:
            output_str_yt = questionary.text(
                t("prompt.output_video"),
                default=str(cfg.download.output_dir),
            ).ask()
            import copy
            cfg_yt = copy.deepcopy(cfg)
            cfg_yt.download.output_dir = Path(output_str_yt).expanduser()
            creds_f = _auth(secrets_path)
            yt_f = YouTubeClient(creds_f)
            dl_yt = Downloader(cfg_yt)
            from .youtube import VideoEntry
            videos_f = []
            for i, u in enumerate(yt_urls):
                m = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
                vid_id = m.group(1) if m else u
                videos_f.append(VideoEntry(video_id=vid_id, title=vid_id, playlist_id="", position=i))
            if questionary.confirm(t("prompt.enrich_yt"), default=True).ask():
                with console.status("[cyan]Enriching…"):
                    yt_f.get_video_details(videos_f)
            dl_yt.download_batch(videos_f, subfolder=None, force=False)

        if art_urls:
            from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
            output_str_art = questionary.text(
                t("prompt.output_article"),
                default=str(cfg.download.article_output_dir),
            ).ask()
            output_dir_art = Path(output_str_art).expanduser()
            extractor_f = ArticleExtractor()
            writer_f    = ArticleNoteWriter(cfg.obsidian)
            dl_art      = ArticleDownloader()
            for i, url in enumerate(art_urls, 1):
                console.rule(f"[cyan]Article {i}/{len(art_urls)}")
                with console.status(f"[cyan]{url[:60]}…"):
                    meta = extractor_f.extract(url)
                console.print(f"  [green]✓[/green] {meta.title or url}")
                folder = f"{meta.site_name} - {meta.title}" if meta.title else url
                saved = dl_art.download(url, output_dir_art, folder)
                if saved:
                    console.print(f"  [green]✓ Page :[/green] {saved.name}")
                note = writer_f.write_note(meta)
                if note:
                    console.print(f"  [green]✓ Note :[/green] {note.name}")
        return

    # ── Options communes ──────────────────────────────────────────────────
    output_str = questionary.text(
        t("prompt.output_dir"),
        default=str(cfg.download.output_dir),
    ).ask()

    browser = questionary.select(
        "Cookies navigateur ?",
        choices=[questionary.Choice("Aucun", value=None)]
        + [questionary.Choice(b, value=b) for b in BROWSER_CHOICES],
    ).ask()

    max_vids_str = questionary.text(t("prompt.max_videos"), default="").ask()
    max_videos = int(max_vids_str) if (max_vids_str or "").isdigit() else None

    force = questionary.confirm(t("prompt.force"), default=False).ask()
    enrich = questionary.confirm(t("prompt.enrich"), default=True).ask()

    cfg = _apply_cli_overrides(cfg, Path(output_str) if output_str else None, browser, None, None, False)
    dl = Downloader(cfg)


    if action == "video":
        url = questionary.text(t("prompt.video_url")).ask()
        if url:
            import re
            m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
            video_id = m.group(1) if m else url
            from .youtube import VideoEntry
            video = VideoEntry(video_id=video_id, title=url, playlist_id="", position=0)
            dl.download_batch([video], force=force)

    elif action == "playlist":
        with console.status("[cyan]Fetching playlists…"):
            playlists = yt.get_playlists()
        if not playlists:
            console.print("[yellow]No playlist."); return
        selected = questionary.checkbox(
            t("prompt.select_playlists"),
            choices=[questionary.Choice(f"{p.title} ({p.video_count})", value=p) for p in playlists],
        ).ask()
        for pl in (selected or []):
            subfolder = _sanitize(pl.title)
            with console.status(f"[cyan]Récupération de '{pl.title}'…"):
                videos = yt.get_playlist_videos(pl.id, max_videos=max_videos)
            if enrich:
                with console.status("[cyan]Enriching…"):
                    yt.get_video_details(videos)
            dl.download_batch(videos, subfolder=subfolder, force=force)


# ── Helpers ───────────────────────────────────────────────────────────────

def _ctx(ctx: typer.Context) -> tuple[AppConfig, Optional[Path]]:
    obj = ctx.obj or {}
    return obj.get("config", load_config()), obj.get("secrets")

def _auth(secrets_path: Optional[Path]):
    try:
        with console.status("[cyan]Google OAuth2 authentication…"):
            creds = get_credentials(client_secrets_path=secrets_path)
        console.print("[green]✓ Authenticated")
        return creds
    except FileNotFoundError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1)
    except Exception as e:
        logger.exception("Erreur d'authentification")
        console.print(f"[red]Erreur : {e}")
        raise typer.Exit(1)

def _apply_cli_overrides(
    cfg: AppConfig,
    output: Optional[Path],
    browser: Optional[str],
    max_size: Optional[str],
    max_duration: Optional[int],
    subtitles: bool,
) -> AppConfig:
    """Applique les overrides CLI sur la config chargée depuis TOML."""
    import copy
    cfg = copy.deepcopy(cfg)
    if output:
        cfg.download.output_dir = output.expanduser()
    if browser:
        cfg.download.browser = browser
    if max_size:
        cfg.download.max_filesize = max_size
    if max_duration:
        cfg.download.max_duration = max_duration
    if subtitles:
        cfg.download.write_subtitles = True
    return cfg

def _sanitize(name: str) -> str:
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")[:100]

def _print_playlists_table(playlists: list) -> None:
    table = Table(title="Mes playlists YouTube", header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", min_width=30)
    table.add_column("Vidéos", justify="right")
    table.add_column("ID", style="dim")
    for i, pl in enumerate(playlists, 1):
        table.add_row(str(i), pl.title, str(pl.video_count), pl.id)
    console.print(table)
