"""
cli.py — Main CLI interface (Typer + questionary).

Commands:
    python3 main.py                          # Interactive mode
    python3 main.py playlists                # List playlists
    python3 main.py stats                    # Download statistics
    python3 main.py resources                # Resource status + retry NOK
    python3 main.py revoke                   # Revoke OAuth2 credentials
    python3 main.py download video URL       # Download a video
    python3 main.py download playlist [IDs]  # Download playlists
    python3 main.py download article URL     # Download a web article
    python3 main.py download urlfile FILE    # Download from URL file
"""

from __future__ import annotations

import copy
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

import questionary
import typer
from googleapiclient.errors import HttpError
from rich.console import Console
from rich.table import Table

from .auth import get_credentials, revoke_credentials as _revoke
from .config import load_config, CONFIG_DIR, AppConfig
from .downloader import BROWSER_CHOICES, Downloader
from .logger import get_logger, setup_logging
from .youtube import YouTubeClient

console = Console()
logger = get_logger(__name__)
LOG_DIR = CONFIG_DIR / "logs"

# ── Typer app ─────────────────────────────────────────────────────────────

app = typer.Typer(
    name="yt-playlist-dl",
    help="YouTube playlist downloader — OAuth2 + yt-dlp + Obsidian",
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)
download_app = typer.Typer(help="Download commands.")
app.add_typer(download_app, name="download")

# ── Annotated option types ─────────────────────────────────────────────────

ConfigOpt  = Annotated[Optional[Path], typer.Option("--config",    "-c", help="Path to config.toml")]
LogOpt     = Annotated[str,            typer.Option("--log-level", "-l", help="DEBUG|INFO|WARNING|ERROR", envvar="YT_DL_LOG_LEVEL")]
SecretsOpt = Annotated[Optional[Path], typer.Option("--secrets",   "-s", help="Path to client_secrets.json")]
OutputOpt  = Annotated[Optional[Path], typer.Option("--output",    "-o", help="Destination folder (overrides config.toml)")]
MaxVidOpt  = Annotated[Optional[int],  typer.Option("--max-videos","-n", help="Max number of videos", min=1)]
MaxSzOpt   = Annotated[Optional[str],  typer.Option("--max-size",        help="Max file size (e.g. 500m, 2g)")]
MaxDurOpt  = Annotated[Optional[int],  typer.Option("--max-duration",    help="Max video duration in seconds", min=1)]
SubsOpt    = Annotated[bool,           typer.Option("--subtitles",        help="Download automatic subtitles")]
ForceOpt   = Annotated[bool,           typer.Option("--force",     "-f", help="Re-download even known videos")]
EnrichOpt  = Annotated[bool,           typer.Option("--enrich/--no-enrich", help="Enrich metadata via YouTube API")]


# ── Root callback ──────────────────────────────────────────────────────────

@app.callback(invoke_without_command=True)
def root_callback(
    ctx: typer.Context,
    config: ConfigOpt = None,
    log_level: LogOpt = "INFO",
    secrets: SecretsOpt = None,
) -> None:
    """Launch interactive mode if no subcommand is provided."""
    setup_logging(LOG_DIR, level=log_level)
    cfg = load_config(config)

    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg
    ctx.obj["secrets"] = secrets

    if ctx.invoked_subcommand is None:
        _interactive_mode(cfg, secrets_path=secrets)


# ── playlists ──────────────────────────────────────────────────────────────

@app.command("playlists")
def cmd_list_playlists(ctx: typer.Context) -> None:
    """List all playlists from the authenticated YouTube account."""
    cfg, secrets = _ctx(ctx)
    yt = YouTubeClient(_auth(secrets))
    with console.status("[cyan]Fetching playlists…"):
        try:
            playlists = yt.get_playlists()
        except HttpError as e:
            console.print(f"[red]API error: {e.reason}")
            raise typer.Exit(1)
    _print_playlists_table(playlists)


# ── stats ──────────────────────────────────────────────────────────────────

@app.command("stats")
def cmd_stats(ctx: typer.Context) -> None:
    """Show download statistics."""
    cfg, _ = _ctx(ctx)
    Downloader(cfg).print_stats()


# ── resources ─────────────────────────────────────────────────────────────

@app.command("resources")
def cmd_resources(
    ctx: typer.Context,
    status_filter: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filter by status: downloaded|failed|deleted|skipped|all"),
    ] = "all",
    retry: Annotated[bool, typer.Option("--retry", help="Offer to retry NOK resources after display.")] = False,
) -> None:
    """Show status of all known resources and optionally retry failed ones."""
    cfg, secrets = _ctx(ctx)
    from .state import StateManager, DownloadStatus

    sm = StateManager(cfg.state_db_path)

    if status_filter == "all" or status_filter is None:
        records = []
        for s in DownloadStatus:
            records.extend(sm.get_by_status(s))
    else:
        try:
            records = sm.get_by_status(DownloadStatus(status_filter))
        except ValueError:
            console.print(f"[red]Unknown status: {status_filter}. Valid values: downloaded, failed, deleted, skipped")
            raise typer.Exit(1)

    if not records:
        console.print("[yellow]No resource found.")
        raise typer.Exit(0)

    order = {"failed": 0, "deleted": 1, "skipped": 2, "downloaded": 3, "pending": 4}
    records.sort(key=lambda r: (order.get(r.status.value, 9), (r.title or "").lower()))

    table = Table(title=f"Resources ({len(records)})", header_style="bold cyan", show_lines=False)
    table.add_column("Status",  width=14)
    table.add_column("Title",   min_width=30, max_width=50, no_wrap=True)
    table.add_column("DL Date", width=12)
    table.add_column("File / Info", max_width=45, no_wrap=True)

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
            p = Path(r.file_path)
            file_info = p.name[:45] if p.exists() else "[dim]file not found[/dim]"
        elif r.status.value == "failed":
            file_info = f"[red]{(r.error_msg or '')[:45]}[/red]"
        else:
            file_info = r.url[:45] if r.url else "—"
        table.add_row(st, (r.title or r.video_id or "")[:50], date_dl, file_info)

    console.print(table)

    counts = Counter(r.status.value for r in records)
    ok_c   = counts.get("downloaded", 0)
    nok_c  = counts.get("failed", 0)
    skip_c = counts.get("skipped", 0)
    del_c  = counts.get("deleted", 0)
    console.print(
        f"\n  [green]✓ {ok_c} OK[/green]  "
        f"[red]✗ {nok_c} NOK[/red]  "
        f"[yellow]⚠ {skip_c} skipped[/yellow]  "
        f"[dim red]✗ {del_c} deleted[/dim red]"
    )

    nok = [r for r in records if r.status.value in ("failed", "skipped")]
    if nok and (retry or questionary.confirm(f"Re-download the {len(nok)} NOK resource(s)?", default=False).ask()):
        selected = questionary.checkbox(
            "Select resources to re-download:",
            choices=[questionary.Choice(f"[{r.status.value}] {r.title or r.video_id}", value=r) for r in nok],
        ).ask()
        if selected:
            from .youtube import VideoEntry
            creds_r = _auth(secrets)
            yt_r = YouTubeClient(creds_r)
            dl_r = Downloader(cfg)
            videos_r = [
                VideoEntry(video_id=r.video_id, title=r.title or r.video_id,
                           playlist_id=r.playlist_id or "", position=i)
                for i, r in enumerate(selected)
            ]
            with console.status("[cyan]Enriching metadata…"):
                yt_r.get_video_details(videos_r)
            dl_r.download_batch(videos_r, force=True)


# ── revoke ─────────────────────────────────────────────────────────────────

@app.command("revoke")
def cmd_revoke(ctx: typer.Context) -> None:
    """Revoke and delete cached OAuth2 credentials."""
    if questionary.confirm("Delete OAuth2 credentials?", default=False).ask():
        _revoke()
        console.print("[green]✓ Credentials deleted.")
    else:
        console.print("[dim]Cancelled.")


# ── retry ──────────────────────────────────────────────────────────────────

@app.command("retry")
def cmd_retry(ctx: typer.Context) -> None:
    """Re-download known videos (interactive selection)."""
    cfg, secrets = _ctx(ctx)
    from .state import DownloadStatus
    dl_tmp = Downloader(cfg)
    candidates = []
    for status in (DownloadStatus.DOWNLOADED, DownloadStatus.FAILED, DownloadStatus.SKIPPED):
        candidates.extend(dl_tmp.state.get_by_status(status))

    if not candidates:
        console.print("[yellow]No video in database.")
        raise typer.Exit(0)

    candidates.sort(key=lambda r: r.title.lower())
    selected = questionary.checkbox(
        "Select videos to re-download:",
        choices=[questionary.Choice(f"[{r.status.value}] {r.title or r.video_id}", value=r) for r in candidates],
    ).ask()

    if not selected:
        console.print("[dim]No selection.")
        raise typer.Exit(0)

    from .youtube import VideoEntry
    creds_r = _auth(secrets)
    yt_r = YouTubeClient(creds_r)
    dl_r = Downloader(cfg)
    videos_r = [
        VideoEntry(video_id=r.video_id, title=r.title or r.video_id,
                   playlist_id=r.playlist_id or "", position=i)
        for i, r in enumerate(selected)
    ]
    with console.status("[cyan]Enriching metadata…"):
        yt_r.get_video_details(videos_r)
    dl_r.download_batch(videos_r, force=True)


# ── download playlist ──────────────────────────────────────────────────────

@download_app.command("playlist")
def cmd_download_playlist(
    ctx: typer.Context,
    playlist_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Playlist ID(s). If omitted: interactive selection."),
    ] = None,
    output: OutputOpt = None,
    max_videos: MaxVidOpt = None,
    max_size: MaxSzOpt = None,
    max_duration: MaxDurOpt = None,
    subtitles: SubsOpt = False,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Download one or more YouTube playlists."""
    cfg, secrets = _ctx(ctx)
    cfg = _apply_overrides(cfg, output, None, max_size, max_duration, subtitles)
    yt = YouTubeClient(_auth(secrets))
    dl = Downloader(cfg)

    if not playlist_ids:
        with console.status("[cyan]Fetching playlists…"):
            playlists = yt.get_playlists()
        if not playlists:
            console.print("[yellow]No playlist found.")
            raise typer.Exit(0)
        playlist_ids = questionary.checkbox(
            "Select playlists:",
            choices=[questionary.Choice(f"{p.title} ({p.video_count} videos)", value=p.id) for p in playlists],
        ).ask()
        if not playlist_ids:
            console.print("[dim]No selection.")
            raise typer.Exit(0)

    with console.status("[cyan]Fetching playlist names…"):
        all_pl = yt.get_playlists()
    pl_map = {p.id: p for p in all_pl}

    total_ok, total_fail, total_skip = 0, 0, 0
    for pid in playlist_ids:
        pl = pl_map.get(pid)
        console.rule(f"[bold cyan]{pl.title if pl else pid}")
        with console.status("[cyan]Fetching videos…"):
            try:
                videos = yt.get_playlist_videos(pid, max_videos=max_videos)
            except HttpError as e:
                console.print(f"[red]API error: {e.reason}")
                continue
        if enrich:
            with console.status("[cyan]Enriching metadata…"):
                yt.get_video_details(videos)
        ok, fail, skip = dl.download_batch(videos, subfolder=None, force=force)
        total_ok += ok; total_fail += fail; total_skip += skip

    console.rule(
        f"[bold]Summary[/bold] — "
        f"[green]{total_ok} succeeded[/green] / "
        f"[red]{total_fail} failure(s)[/red] / "
        f"[dim]{total_skip} skipped[/dim]"
    )


# ── download video ─────────────────────────────────────────────────────────

@download_app.command("video")
def cmd_download_video(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="YouTube video URL.")],
    output: OutputOpt = None,
    max_size: MaxSzOpt = None,
    max_duration: MaxDurOpt = None,
    subtitles: SubsOpt = False,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Download a YouTube video from its URL."""
    cfg, secrets = _ctx(ctx)
    cfg = _apply_overrides(cfg, output, None, max_size, max_duration, subtitles)
    creds = _auth(secrets)

    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    video_id = m.group(1) if m else url

    dl = Downloader(cfg)
    if not force and dl.state.is_downloaded(video_id):
        console.print(f"[green]✓ Already downloaded (ID: {video_id}). Use --force to re-download.")
        raise typer.Exit(0)

    from .youtube import VideoEntry
    yt = YouTubeClient(creds)
    video = VideoEntry(video_id=video_id, title=video_id, playlist_id="", position=0)
    if enrich:
        with console.status("[cyan]Fetching metadata…"):
            yt.get_video_details([video])
    ok, _, _ = dl.download_batch([video], force=force)
    raise typer.Exit(0 if ok else 1)


# ── download article ───────────────────────────────────────────────────────

@download_app.command("article")
def cmd_download_article(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Web article URL.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Destination folder.")] = None,
) -> None:
    """Download a web article and create an Obsidian note."""
    cfg, _ = _ctx(ctx)
    from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
    output_dir = output.expanduser() if output else cfg.download.article_output_dir

    with console.status("[cyan]Extracting metadata…"):
        meta = ArticleExtractor().extract(url)

    console.print(f"[green]✓ Title :[/green] {meta.title}")
    console.print(f"[green]✓ Author:[/green] {meta.author_str}")
    console.print(f"[green]✓ Date  :[/green] {meta.published or 'not found'}")

    folder = f"{meta.site_name} - {meta.title}" if meta.title else url
    with console.status("[cyan]Downloading page…"):
        saved = ArticleDownloader().download(url, output_dir, folder)
    if saved:
        console.print(f"[green]✓ Page  :[/green] {saved}")
    note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
    if note:
        console.print(f"[green]✓ Note  :[/green] {note.name}")


# ── download urlfile ───────────────────────────────────────────────────────

@download_app.command("urlfile")
def cmd_download_urlfile(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Text file containing URLs (one per line).")],
    output_video: Annotated[Optional[Path], typer.Option("--output-video",   "-v", help="Folder for videos.")] = None,
    output_article: Annotated[Optional[Path], typer.Option("--output-article", "-a", help="Folder for articles.")] = None,
    force: ForceOpt = False,
    enrich: EnrichOpt = True,
) -> None:
    """Download all URLs from a file (YouTube videos + web articles)."""
    cfg, secrets = _ctx(ctx)

    if not file.exists():
        console.print(f"[red]File not found: {file}")
        raise typer.Exit(1)

    lines_raw = file.read_text(encoding="utf-8").splitlines()
    urls = [l.strip() for l in lines_raw if l.strip() and not l.strip().startswith("#")]

    if not urls:
        console.print("[yellow]No URL found in file.")
        raise typer.Exit(0)

    yt_pattern = re.compile(r"youtube\.com|youtu\.be")
    yt_urls  = [u for u in urls if yt_pattern.search(u)]
    art_urls = [u for u in urls if not yt_pattern.search(u)]
    console.print(
        f"[cyan]{len(urls)} URL(s): "
        f"[green]{len(yt_urls)} video(s)[/green] / "
        f"[blue]{len(art_urls)} article(s)[/blue]"
    )

    if yt_urls:
        cfg_yt = copy.deepcopy(cfg)
        if output_video:
            cfg_yt.download.output_dir = output_video.expanduser()
        creds_f = _auth(secrets)
        from .youtube import VideoEntry
        yt_f = YouTubeClient(creds_f)
        dl_yt = Downloader(cfg_yt)
        videos_f = []
        for i, u in enumerate(yt_urls):
            m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
            vid_id = m.group(1) if m else u
            videos_f.append(VideoEntry(video_id=vid_id, title=vid_id, playlist_id="", position=i))
        if enrich:
            with console.status("[cyan]Enriching metadata…"):
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
                console.print(f"  [green]✓ Page:[/green] {saved.name}")
            note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
            if note:
                console.print(f"  [green]✓ Note:[/green] {note.name}")


# ── Interactive mode ───────────────────────────────────────────────────────

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
        "What would you like to do?",
        choices=[
            questionary.Choice("📋  Download playlists",            value="playlist"),
            questionary.Choice("🎬  Download a video (URL)",        value="video"),
            questionary.Choice("📰  Download a web article (URL)",  value="article"),
            questionary.Choice("📋  Download from URL file",        value="urlfile"),
            questionary.Choice("🔄  Re-download known videos",      value="retry"),
            questionary.Choice("📄  List my playlists",             value="list"),
            questionary.Choice("📊  Download statistics",           value="stats"),
            questionary.Choice("🔍  Resource status",               value="resources"),
            questionary.Choice("🗑️   Flush database",               value="flush"),
            questionary.Choice("🔑  Revoke credentials",            value="revoke"),
            questionary.Choice("❌  Quit",                          value="quit"),
        ],
    ).ask()

    if not action or action == "quit":
        console.print("[dim]Goodbye.")
        sys.exit(0)

    if action == "revoke":
        _revoke(); return

    if action == "flush":
        import sqlite3
        if questionary.confirm("Flush the entire database? (downloaded files will NOT be deleted)", default=False).ask():
            conn = sqlite3.connect(cfg.state_db_path)
            conn.execute("DELETE FROM downloads")
            conn.commit()
            conn.close()
            console.print(f"[green]✓ Database flushed ({cfg.state_db_path})")
        else:
            console.print("[dim]Cancelled.")
        return

    if action == "stats":
        Downloader(cfg).print_stats(); return

    if action == "article":
        url_article = questionary.text("Article URL:").ask()
        if not url_article:
            return
        output_str = questionary.text(
            "Destination folder for HTML page:",
            default=str(cfg.download.article_output_dir),
        ).ask()
        output_dir = Path(output_str).expanduser()
        from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
        with console.status("[cyan]Extracting metadata…"):
            meta = ArticleExtractor().extract(url_article)
        console.print(f"[green]✓ Title :[/green] {meta.title}")
        console.print(f"[green]✓ Author:[/green] {meta.author_str}")
        console.print(f"[green]✓ Date  :[/green] {meta.published or 'not found'}")
        console.print(f"[green]✓ Site  :[/green] {meta.site_name}")
        folder = f"{meta.site_name} - {meta.title}" if meta.title else url_article
        with console.status("[cyan]Downloading page and images…"):
            saved = ArticleDownloader().download(url_article, output_dir, folder)
        if saved:
            console.print(f"[green]✓ Page saved:[/green] {saved}")
        note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
        if note:
            console.print(f"[green]✓ Note created:[/green] {note.name}")
        return

    if action == "urlfile":
        file_path_str = questionary.text("Path to URL file:", default="~/urls.txt").ask()
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
        yt_pattern = re.compile(r"youtube\.com|youtu\.be")
        yt_urls  = [u for u in urls if yt_pattern.search(u)]
        art_urls = [u for u in urls if not yt_pattern.search(u)]
        console.print(f"[cyan]{len(urls)} URL(s): [green]{len(yt_urls)} video(s)[/green] / [blue]{len(art_urls)} article(s)[/blue]")

        if yt_urls:
            output_str_yt = questionary.text("Destination folder for videos:", default=str(cfg.download.output_dir)).ask()
            cfg_yt = copy.deepcopy(cfg)
            cfg_yt.download.output_dir = Path(output_str_yt).expanduser()
            force_yt = questionary.confirm("Re-download already known videos?", default=False).ask()
            enrich_yt = questionary.confirm("Enrich YouTube metadata?", default=True).ask()
            creds_f = _auth(secrets_path)
            from .youtube import VideoEntry
            yt_f = YouTubeClient(creds_f)
            dl_yt = Downloader(cfg_yt)
            videos_f = []
            for i, u in enumerate(yt_urls):
                m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
                vid_id = m.group(1) if m else u
                videos_f.append(VideoEntry(video_id=vid_id, title=vid_id, playlist_id="", position=i))
            if enrich_yt:
                with console.status("[cyan]Enriching metadata…"):
                    yt_f.get_video_details(videos_f)
            dl_yt.download_batch(videos_f, subfolder=None, force=force_yt)

        if art_urls:
            from .article import ArticleExtractor, ArticleNoteWriter, ArticleDownloader
            output_str_art = questionary.text("Destination folder for articles:", default=str(cfg.download.article_output_dir)).ask()
            out_art = Path(output_str_art).expanduser()
            for i, url in enumerate(art_urls, 1):
                console.rule(f"[cyan]Article {i}/{len(art_urls)}")
                with console.status(f"[cyan]{url[:60]}…"):
                    meta = ArticleExtractor().extract(url)
                console.print(f"  [green]✓[/green] {meta.title or url}")
                folder = f"{meta.site_name} - {meta.title}" if meta.title else url
                saved = ArticleDownloader().download(url, out_art, folder)
                if saved:
                    console.print(f"  [green]✓ Page:[/green] {saved.name}")
                note = ArticleNoteWriter(cfg.obsidian).write_note(meta)
                if note:
                    console.print(f"  [green]✓ Note:[/green] {note.name}")
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
            "Select videos to re-download:",
            choices=[questionary.Choice(f"[{r.status.value}] {r.title or r.video_id}", value=r) for r in candidates],
        ).ask()
        if not selected:
            console.print("[dim]No selection."); return
        from .youtube import VideoEntry
        creds_r = _auth(secrets_path)
        yt_r = YouTubeClient(creds_r)
        dl_r = Downloader(cfg)
        videos_r = [VideoEntry(video_id=r.video_id, title=r.title or r.video_id, playlist_id=r.playlist_id or "", position=i) for i, r in enumerate(selected)]
        with console.status("[cyan]Enriching metadata…"):
            yt_r.get_video_details(videos_r)
        dl_r.download_batch(videos_r, force=True)
        return

    if action == "resources":
        from .state import StateManager, DownloadStatus
        sm = StateManager(cfg.state_db_path)
        records = []
        for s in DownloadStatus:
            records.extend(sm.get_by_status(s))
        if not records:
            console.print("[yellow]No resource found."); return
        order = {"failed": 0, "deleted": 1, "skipped": 2, "downloaded": 3, "pending": 4}
        records.sort(key=lambda r: (order.get(r.status.value, 9), (r.title or "").lower()))
        table = Table(title=f"Resources ({len(records)})", header_style="bold cyan", show_lines=False)
        table.add_column("Status", width=14)
        table.add_column("Title", min_width=30, max_width=50, no_wrap=True)
        table.add_column("DL Date", width=12)
        table.add_column("File / Info", max_width=45, no_wrap=True)
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
                p = Path(r.file_path)
                file_info = p.name[:45] if p.exists() else "[dim]file not found[/dim]"
            elif r.status.value == "failed":
                file_info = f"[red]{(r.error_msg or '')[:45]}[/red]"
            else:
                file_info = "—"
            table.add_row(st, (r.title or r.video_id or "")[:50], date_dl, file_info)
        console.print(table)
        counts = Counter(r.status.value for r in records)
        ok_n = counts.get("downloaded", 0); nok_n = counts.get("failed", 0); skip_n = counts.get("skipped", 0)
        console.print(f"  [green]✓ {ok_n} OK[/green]  [red]✗ {nok_n} NOK[/red]  [yellow]⚠ {skip_n} skipped[/yellow]")
        nok = [r for r in records if r.status.value in ("failed", "skipped")]
        if nok and questionary.confirm(f"Re-download the {len(nok)} NOK resource(s)?", default=False).ask():
            selected = questionary.checkbox(
                "Select resources:",
                choices=[questionary.Choice(f"[{r.status.value}] {r.title or r.video_id}", value=r) for r in nok],
            ).ask()
            if selected:
                from .youtube import VideoEntry
                creds_r = _auth(secrets_path)
                yt_r = YouTubeClient(creds_r)
                dl_r = Downloader(cfg)
                videos_r = [VideoEntry(video_id=r.video_id, title=r.title or r.video_id, playlist_id=r.playlist_id or "", position=i) for i, r in enumerate(selected)]
                with console.status("[cyan]Enriching…"):
                    yt_r.get_video_details(videos_r)
                dl_r.download_batch(videos_r, force=True)
        return

    # ── Common options (playlist / video / list) ───────────────────────────
    creds = _auth(secrets_path)
    yt = YouTubeClient(creds)

    if action == "list":
        with console.status("[cyan]Fetching…"):
            _print_playlists_table(yt.get_playlists())
        return

    output_str = questionary.text("Destination folder:", default=str(cfg.download.output_dir)).ask()
    max_vids_str = questionary.text("Video limit (empty = unlimited):", default="").ask()
    max_videos = int(max_vids_str) if (max_vids_str or "").isdigit() else None
    force = questionary.confirm("Re-download already known videos?", default=False).ask()
    enrich = questionary.confirm("Enrich metadata (author, tags…)?", default=True).ask()
    cfg = _apply_overrides(cfg, Path(output_str) if output_str else None, None, None, None, False)
    dl = Downloader(cfg)

    if action == "video":
        url = questionary.text("Video URL:").ask()
        if url:
            m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
            video_id = m.group(1) if m else url
            from .youtube import VideoEntry
            video = VideoEntry(video_id=video_id, title=video_id, playlist_id="", position=0)
            if enrich:
                with console.status("[cyan]Fetching metadata…"):
                    yt.get_video_details([video])
            dl.download_batch([video], force=force)

    elif action == "playlist":
        with console.status("[cyan]Fetching playlists…"):
            playlists = yt.get_playlists()
        if not playlists:
            console.print("[yellow]No playlist found."); return
        selected = questionary.checkbox(
            "Select playlists:",
            choices=[questionary.Choice(f"{p.title} ({p.video_count})", value=p) for p in playlists],
        ).ask()
        for pl in (selected or []):
            console.rule(f"[bold cyan]{pl.title}")
            with console.status(f"[cyan]Fetching '{pl.title}'…"):
                videos = yt.get_playlist_videos(pl.id, max_videos=max_videos)
            if enrich:
                with console.status("[cyan]Enriching metadata…"):
                    yt.get_video_details(videos)
            dl.download_batch(videos, subfolder=None, force=force)


# ── Helpers ────────────────────────────────────────────────────────────────

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
        logger.exception("Authentication error")
        console.print(f"[red]Error: {e}")
        raise typer.Exit(1)


def _apply_overrides(
    cfg: AppConfig,
    output: Optional[Path],
    browser: Optional[str],
    max_size: Optional[str],
    max_duration: Optional[int],
    subtitles: bool,
) -> AppConfig:
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
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")[:100]


def _print_playlists_table(playlists: list) -> None:
    table = Table(title="My YouTube Playlists", header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", min_width=30)
    table.add_column("Videos", justify="right")
    table.add_column("ID", style="dim")
    for i, pl in enumerate(playlists, 1):
        table.add_row(str(i), pl.title, str(pl.video_count), pl.id)
    console.print(table)
