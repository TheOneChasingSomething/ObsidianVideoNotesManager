"""
config.py — Gestion de la configuration via fichier TOML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError as exc:
        raise ImportError("pip install tomli") from exc

from .logger import get_logger

logger = get_logger(__name__)

CONFIG_DIR: Path = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH: Path = CONFIG_DIR / "config.toml"


@dataclass
class ObsidianConfig:
    vault_path: Path = field(default_factory=lambda: Path.home() / "Documents" / "Obsidian" / "MyVault")
    notes_subfolder: str = "Resources/YouTube"
    default_tags: list[str] = field(default_factory=lambda: ["literature-note", "ressources-note", "resource", "resource-note"])
    default_project: str = ""
    default_task: str = ""
    default_knowledge_index: str = ""
    overwrite_notes: bool = False


@dataclass
class DownloadConfig:
    output_dir: Path = field(default_factory=lambda: Path.home() / "Downloads" / "YouTube")
    article_output_dir: Path = field(default_factory=lambda: Path.home() / "Downloads" / "Articles")
    browser: Optional[str] = None
    cookiefile: Optional[str] = None
    max_filesize: Optional[str] = None
    max_duration: Optional[int] = None
    embed_metadata: bool = True
    write_subtitles: bool = False
    video_format: str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"


@dataclass
class AppConfig:
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    log_level: str = "INFO"
    state_db_path: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.state_db_path is None:
            self.state_db_path = CONFIG_DIR / "state.db"


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    if config_path:
        resolved = config_path
    elif "YT_DL_CONFIG" in os.environ:
        resolved = Path(os.environ["YT_DL_CONFIG"])
    else:
        resolved = DEFAULT_CONFIG_PATH

    if not resolved.exists():
        logger.info(f"Configuration absente ({resolved}). Valeurs par défaut.")
        _write_example_config(DEFAULT_CONFIG_PATH)
        return AppConfig()

    with open(resolved, "rb") as f:
        raw: dict = tomllib.load(f)

    return _parse_config(raw)


def _parse_config(raw: dict) -> AppConfig:
    obs_raw = raw.get("obsidian", {})
    dl_raw  = raw.get("download", {})

    obsidian = ObsidianConfig(
        vault_path=Path(obs_raw.get("vault_path", str(Path.home() / "Documents" / "Obsidian" / "MyVault"))).expanduser(),
        notes_subfolder=obs_raw.get("notes_subfolder", "Resources/YouTube"),
        default_tags=obs_raw.get("default_tags", ["literature-note", "ressources-note", "resource", "resource-note"]),
        default_project=obs_raw.get("default_project", ""),
        default_task=obs_raw.get("default_task", ""),
        default_knowledge_index=obs_raw.get("default_knowledge_index", ""),
        overwrite_notes=obs_raw.get("overwrite_notes", False),
    )

    download = DownloadConfig(
        output_dir=Path(dl_raw.get("output_dir", str(Path.home() / "Downloads" / "YouTube"))).expanduser(),
        article_output_dir=Path(dl_raw.get("article_output_dir", str(Path.home() / "Downloads" / "Articles"))).expanduser(),
        browser=dl_raw.get("browser") or None,
        cookiefile=dl_raw.get("cookiefile") or None,
        max_filesize=dl_raw.get("max_filesize") or None,
        max_duration=dl_raw.get("max_duration") or None,
        embed_metadata=dl_raw.get("embed_metadata", True),
        write_subtitles=dl_raw.get("write_subtitles", False),
        video_format=dl_raw.get("video_format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
    )

    return AppConfig(
        obsidian=obsidian,
        download=download,
        log_level=raw.get("log_level", "INFO"),
        state_db_path=Path(raw["state_db_path"]).expanduser() if raw.get("state_db_path") else None,
    )


def _write_example_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    example = """\
# yt-playlist-dl — Configuration (TOML 1.0)

log_level = "INFO"

[obsidian]
vault_path              = "~/Documents/Obsidian/MyVault"
notes_subfolder         = "Resources/YouTube"
overwrite_notes         = false
default_tags            = ["literature-note", "ressources-note", "resource", "resource-note"]
default_project         = ""
default_task            = ""
default_knowledge_index = ""

[download]
output_dir         = "~/Downloads/YouTube"
article_output_dir = "~/Downloads/Articles"
browser            = ""
cookiefile         = ""
max_filesize       = ""
max_duration       = 0
embed_metadata     = true
write_subtitles    = false
video_format       = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
"""
    if not path.exists():
        path.write_text(example, encoding="utf-8")
        logger.info(f"Config exemple créée : {path}")
