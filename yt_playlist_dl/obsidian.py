"""
obsidian.py — Génération de notes Markdown pour le coffre Obsidian.

Crée un fichier `.md` avec frontmatter YAML pour chaque vidéo téléchargée.
Le frontmatter respecte le schéma défini dans le cahier des charges :

    Author, URL, publication, lecture, tags, Project, Task

    Puis le corps : titre H1 + description YouTube.

Le nom du fichier est dérivé du titre vidéo (caractères illicites remplacés).
Le répertoire de destination est :
    <vault_path> / <notes_subfolder> / <playlist_name (optionnel)> /

Références :
    - Obsidian frontmatter YAML : https://help.obsidian.md/Editing+and+formatting/Properties
    - PyYAML : https://pyyaml.org/wiki/PyYAMLDocumentation
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from .config import ObsidianConfig
from .logger import get_logger

logger = get_logger(__name__)


# ── Données d'entrée ──────────────────────────────────────────────────────


class VideoMeta:
    """
    Métadonnées d'une vidéo nécessaires à la génération de la note Obsidian.

    Attributes:
        video_id:       Identifiant YouTube.
        title:          Titre de la vidéo.
        author:         Nom de la chaîne YouTube.
        url:            URL complète.
        description:    Description YouTube (corps de la note).
        published_date: Date de publication (format YYYY-MM-DD).
        tags:           Tags issus des métadonnées YouTube (keywords).
        playlist_name:  Nom de la playlist source (pour le sous-dossier).
    """

    __slots__ = (
        "video_id", "title", "author", "url",
        "description", "published_date", "tags", "playlist_name",
        "download_path", "knowledge_index", "project",
    )

    def __init__(
        self,
        video_id: str,
        title: str,
        author: str = "",
        url: str = "",
        description: str = "",
        published_date: Optional[str] = None,
        tags: Optional[list[str]] = None,
        playlist_name: Optional[str] = None,
        download_path: Optional[str] = None,
        knowledge_index: Optional[str] = None,
        project: Optional[str] = None,
    ) -> None:
        self.video_id = video_id
        self.title = title
        self.author = author
        self.url = url or f"https://www.youtube.com/watch?v={video_id}"
        self.description = description
        self.published_date = published_date or date.today().isoformat()
        self.tags = tags or []
        self.playlist_name = playlist_name
        self.download_path = download_path
        self.knowledge_index = knowledge_index
        self.project = project  # override du default_project config


# ── Générateur de notes ───────────────────────────────────────────────────


class ObsidianNoteWriter:
    """
    Génère et écrit les notes Markdown dans le coffre Obsidian.

    Attributes:
        config:  ObsidianConfig issu du fichier TOML.
    """

    def __init__(self, config: ObsidianConfig) -> None:
        self.config = config

    # ── Interface publique ────────────────────────────────────────────────

    def write_note(self, meta: VideoMeta) -> Optional[Path]:
        """
        Génère et écrit la note Obsidian pour une vidéo.

        Args:
            meta:  Métadonnées de la vidéo.

        Returns:
            Chemin absolu du fichier `.md` créé, ou None en cas d'erreur.
        """
        note_path = self._resolve_note_path(meta)

        if note_path.exists() and not self.config.overwrite_notes:
            logger.info(f"Note existante conservée (overwrite_notes=false) : {note_path.name}")
            return note_path

        content = self._render(meta)

        try:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(content, encoding="utf-8")
            logger.info(f"Note Obsidian créée : {note_path}")
            return note_path
        except OSError as e:
            logger.error(f"Impossible d'écrire la note Obsidian : {e}")
            return None

    # ── Rendu Markdown ────────────────────────────────────────────────────

    def _render(self, meta: VideoMeta) -> str:
        """
        Construit le contenu complet du fichier Markdown.

        Structure :
            ---
            <frontmatter YAML>
            ---

            # Titre
            Description
        """
        frontmatter = self._build_frontmatter(meta)
        body = self._build_body(meta)
        return f"---\n{frontmatter}---\n\n{body}"

    def _build_frontmatter(self, meta: VideoMeta) -> str:
        """
        Construit le bloc YAML entre les délimiteurs `---`.

        Utilise PyYAML avec `allow_unicode=True` et `default_flow_style=False`
        pour garantir un YAML lisible et compatible Obsidian.
        """
        # Fusion des tags : config defaults + tags YouTube (dédupliqués, conservant l'ordre)
        all_tags = list(dict.fromkeys(
            self.config.default_tags + [_sanitize_tag(t) for t in meta.tags]
        ))

        data: dict = {
            "Author": meta.author or "Unknown",
            "URL": meta.url,
            "publication": meta.published_date,
            "lecture": date.today().isoformat(),
            "tags": all_tags,
            "download": meta.download_path or "KO",
        }

        # Knowledge-index (always included, [[...]] added if absent)
        ki = meta.knowledge_index or self.config.default_knowledge_index or ""
        if ki:
            ki = ki.strip()
            if not ki.startswith("[["):
                ki = f"[[{ki}]]"
        data["Knowledge-index"] = ki

        # Project : meta.project > config.default_project (always included)
        project_raw = meta.project if meta.project else self.config.default_project
        if project_raw:
            p = project_raw.strip()
            if not p.startswith("[["):
                p = f"[[{p}]]"
            data["Project"] = p
        else:
            data["Project"] = ""  # empty field for manual editing
        data["Task"] = self.config.default_task or ""  # empty field for manual editing

        return yaml.dump(
            data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )

    def _build_body(self, meta: VideoMeta) -> str:
        """Construit le corps Markdown (titre H1 + description)."""
        lines = [f"# {meta.title}"]
        if meta.description.strip():
            # Tronque les descriptions très longues (YouTube peut dépasser 5 000 caractères)
            desc = meta.description.strip()
            if len(desc) > 3000:
                desc = desc[:3000] + "\n\n*[Description tronquée — voir la vidéo originale]*"
            lines.append("")
            lines.append(desc)
        return "\n".join(lines) + "\n"

    # ── Résolution du chemin ──────────────────────────────────────────────

    def _resolve_note_path(self, meta: VideoMeta) -> Path:
        """
        Calcule le chemin absolu du fichier `.md`.

        Hiérarchie :
            vault_path / notes_subfolder / [playlist_name] / {titre}.md
        """
        base = self.config.vault_path / self.config.notes_subfolder

        if meta.playlist_name:
            base = base / _sanitize_filename(meta.playlist_name)

        filename = _sanitize_filename(meta.title) + ".md"
        return base / filename


# ── Utilitaires ───────────────────────────────────────────────────────────


def _sanitize_filename(name: str, maxlen: int = 100) -> str:
    """
    Nettoie une chaîne pour l'utiliser comme composant de chemin de fichier.

    Remplace les caractères interdits sur Linux/Windows/macOS,
    et tronque à `maxlen` caractères.
    """
    # Caractères interdits sur les systèmes de fichiers courants
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Espaces multiples → un seul
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:maxlen]


def _sanitize_tag(tag: str) -> str:
    """Normalise un tag YouTube pour Obsidian (CamelCase, sans espaces)."""
    # Supprimer les caractères spéciaux, conserver les tirets et CamelCase
    tag = re.sub(r"[^\w\s\-]", "", tag).strip()
    # Espaces → CamelCase
    if " " in tag:
        tag = "".join(word.capitalize() for word in tag.split())
    return tag
