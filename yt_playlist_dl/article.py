"""
article.py — Téléchargement d'articles web et génération de notes Obsidian.

Utilise newspaper3k pour extraire automatiquement :
    - Titre, auteur(s), date de publication
    - Texte principal (sans publicités ni navigation)
    - Tags/mots-clés de l'article
    - Image principale

Références :
    - newspaper3k : https://newspaper.readthedocs.io/en/latest/
    - Open Graph protocol : https://ogp.me/
"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin, urldefrag

import requests
from bs4 import BeautifulSoup

from .config import ObsidianConfig
from .logger import get_logger

logger = get_logger(__name__)

# Tentative d'import newspaper3k (optionnel)
try:
    import newspaper
    from newspaper import Article as NewspaperArticle
    HAS_NEWSPAPER = True
except ImportError:
    try:
        from newspaper import Article as NewspaperArticle
        import newspaper
        HAS_NEWSPAPER = True
    except ImportError:
        HAS_NEWSPAPER = False
        logger.warning("newspaper3k non installé — extraction basique uniquement.")


# ── Données extraites ─────────────────────────────────────────────────────


class ArticleMeta:
    """
    Métadonnées extraites d'un article web.

    Attributes:
        url:          URL source de l'article.
        title:        Titre de l'article.
        authors:      Liste des auteurs (peut être vide).
        published:    Date de publication (format YYYY-MM-DD ou vide).
        description:  Résumé / meta description.
        text:         Texte principal de l'article.
        tags:         Mots-clés extraits.
        site_name:    Nom du site (ex. "The Guardian").
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.title: str = ""
        self.authors: list[str] = []
        self.published: str = ""
        self.description: str = ""
        self.text: str = ""
        self.tags: list[str] = []
        self.site_name: str = ""

    @property
    def author_str(self) -> str:
        """Retourne les auteurs sous forme de chaîne."""
        return ", ".join(self.authors) if self.authors else "Unknown"

    @property
    def domain(self) -> str:
        """Retourne le domaine de l'URL (ex. 'medium.com')."""
        return urlparse(self.url).netloc.replace("www.", "")


# ── Extracteur ────────────────────────────────────────────────────────────


class ArticleExtractor:
    """
    Extrait les métadonnées et le contenu d'un article web.

    Stratégie à deux niveaux :
        1. newspaper3k (si disponible) — extraction NLP robuste.
        2. Fallback BeautifulSoup — Open Graph + meta HTML.
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    def extract(self, url: str) -> ArticleMeta:
        """
        Extrait les métadonnées d'un article à partir de son URL.

        Args:
            url:  URL complète de l'article.

        Returns:
            ArticleMeta peuplée.
        """
        meta = ArticleMeta(url)

        if HAS_NEWSPAPER:
            try:
                return self._extract_newspaper(url, meta)
            except Exception as e:
                logger.warning(f"newspaper3k a échoué : {e} — fallback BeautifulSoup")

        return self._extract_bs4(url, meta)

    # ── newspaper3k ──────────────────────────────────────────────────────

    def _extract_newspaper(self, url: str, meta: ArticleMeta) -> ArticleMeta:
        article = NewspaperArticle(url, language="fr")
        article.download()
        article.parse()
        article.nlp()

        meta.title = article.title or ""
        meta.authors = [a for a in (article.authors or []) if a.strip()]
        meta.text = article.text or ""
        meta.tags = list(article.keywords or [])
        meta.description = article.meta_description or article.summary or ""

        if article.publish_date:
            try:
                meta.published = article.publish_date.strftime("%Y-%m-%d")
            except Exception:
                meta.published = str(article.publish_date)[:10]

        # Site name depuis Open Graph
        if article.html:
            soup = BeautifulSoup(article.html, "lxml")
            og_site = soup.find("meta", property="og:site_name")
            if og_site and og_site.get("content"):
                meta.site_name = og_site["content"]

        if not meta.site_name:
            meta.site_name = meta.domain

        logger.info(f"Article extrait via newspaper3k : {meta.title}")
        return meta

    # ── BeautifulSoup (fallback) ──────────────────────────────────────────

    def _extract_bs4(self, url: str, meta: ArticleMeta) -> ArticleMeta:
        try:
            resp = requests.get(url, headers=self.HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Impossible de télécharger {url} : {e}")
            meta.title = url
            return meta

        soup = BeautifulSoup(resp.text, "lxml")

        # Titre
        og_title = soup.find("meta", property="og:title")
        meta.title = (
            og_title["content"] if og_title and og_title.get("content")
            else (soup.find("title") or {}).get_text(strip=True) or url
        )

        # Description
        og_desc = soup.find("meta", property="og:description")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        meta.description = (
            (og_desc["content"] if og_desc and og_desc.get("content") else None)
            or (meta_desc["content"] if meta_desc and meta_desc.get("content") else "")
        )

        # Site name
        og_site = soup.find("meta", property="og:site_name")
        meta.site_name = (
            og_site["content"] if og_site and og_site.get("content") else meta.domain
        )

        # Auteur
        for attr in [
            {"name": "author"}, {"property": "article:author"},
            {"name": "twitter:creator"}, {"itemprop": "author"},
        ]:
            tag = soup.find("meta", attr)
            if tag and tag.get("content"):
                meta.authors = [tag["content"].strip()]
                break
        if not meta.authors:
            rel_author = soup.find("a", rel="author")
            if rel_author:
                meta.authors = [rel_author.get_text(strip=True)]

        # Date de publication
        for attr in [
            {"property": "article:published_time"},
            {"name": "publish_date"}, {"name": "date"},
            {"itemprop": "datePublished"},
        ]:
            tag = soup.find("meta", attr)
            if tag and tag.get("content"):
                raw = tag["content"][:10]
                meta.published = raw
                break
        if not meta.published:
            time_tag = soup.find("time")
            if time_tag and time_tag.get("datetime"):
                meta.published = time_tag["datetime"][:10]

        # Texte principal (heuristique)
        for selector in ["article", "main", ".post-content", ".entry-content", ".content"]:
            body = soup.select_one(selector)
            if body:
                meta.text = body.get_text(separator="\n", strip=True)[:5000]
                break
        if not meta.text:
            meta.text = soup.get_text(separator="\n", strip=True)[:3000]

        logger.info(f"Article extrait via BeautifulSoup : {meta.title}")
        return meta



# ── Téléchargeur HTML + images ────────────────────────────────────────────


class ArticleDownloader:
    """
    Télécharge une page HTML et ses images dans un dossier local.

    Structure de sortie :
        <output_dir>/
            <nom_article>/
                index.html       ← page HTML avec chemins relatifs
                images/
                    image1.jpg
                    image2.png
                    ...
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"}

    def download(self, url: str, output_dir: Path, folder_name: str) -> Optional[Path]:
        """
        Télécharge la page et ses images dans output_dir/folder_name/.

        Args:
            url:         URL de l'article.
            output_dir:  Répertoire de destination racine.
            folder_name: Nom du sous-dossier à créer.

        Returns:
            Chemin du dossier créé, ou None en cas d'échec.
        """
        dest = output_dir / _sanitize_filename(folder_name)
        img_dir = dest / "images"
        dest.mkdir(parents=True, exist_ok=True)
        img_dir.mkdir(exist_ok=True)

        try:
            resp = requests.get(url, headers=self.HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Impossible de télécharger {url} : {e}")
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # ── Téléchargement des images ─────────────────────────────────
        downloaded: dict[str, str] = {}  # url_originale -> chemin_relatif

        for tag in soup.find_all(["img", "source"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("srcset", "").split()[0]
            if not src or src.startswith("data:"):
                continue

            abs_url, _ = urldefrag(urljoin(url, src))
            if abs_url in downloaded:
                tag["src"] = downloaded[abs_url]
                continue

            ext = Path(urlparse(abs_url).path).suffix.lower()
            if ext not in self.IMAGE_EXTS:
                ext = ".jpg"

            img_name = f"img_{len(downloaded):04d}{ext}"
            img_path = img_dir / img_name
            rel_path = f"images/{img_name}"

            try:
                img_resp = requests.get(abs_url, headers=self.HEADERS, timeout=10)
                img_resp.raise_for_status()
                img_path.write_bytes(img_resp.content)
                downloaded[abs_url] = rel_path
                tag["src"] = rel_path
                logger.debug(f"Image téléchargée : {img_name}")
            except Exception as e:
                logger.debug(f"Image ignorée ({abs_url}) : {e}")

        # ── Réécriture des liens CSS/JS (suppression) ─────────────────
        for tag in soup.find_all(["script", "link"]):
            tag.decompose()

        # ── Sauvegarde HTML ───────────────────────────────────────────
        html_path = dest / "index.html"
        html_path.write_text(str(soup), encoding="utf-8")
        logger.info(f"Page sauvegardée : {html_path} ({len(downloaded)} image(s))")

        return dest



# ── Générateur de note Obsidian ───────────────────────────────────────────


class ArticleNoteWriter:
    """
    Génère une note Obsidian pour un article web.

    Format du nom de fichier : "{site} - {titre}.md"
    """

    def __init__(self, config: ObsidianConfig) -> None:
        self.config = config

    def write_note(self, meta: ArticleMeta) -> Optional[Path]:
        """
        Crée la note Markdown dans le coffre Obsidian.

        Args:
            meta:  Métadonnées de l'article.

        Returns:
            Chemin absolu de la note créée, ou None en cas d'erreur.
        """
        note_path = self._resolve_path(meta)

        if note_path.exists() and not self.config.overwrite_notes:
            logger.info(f"Note existante conservée : {note_path.name}")
            return note_path

        content = self._render(meta)

        try:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(content, encoding="utf-8")
            logger.info(f"Note article créée : {note_path}")
            return note_path
        except OSError as e:
            logger.error(f"Impossible d'écrire la note : {e}")
            return None

    def _render(self, meta: ArticleMeta) -> str:
        import yaml

        all_tags = list(dict.fromkeys(
            self.config.default_tags
            + [_sanitize_tag(t) for t in meta.tags]
        ))

        data: dict = {
            "Author": meta.author_str,
            "URL": meta.url,
            "site": meta.site_name,
            "publication": meta.published or date.today().isoformat(),
            "lecture": date.today().isoformat(),
            "tags": all_tags,
        }
        if self.config.default_project:
            data["Project"] = self.config.default_project
        if self.config.default_task:
            data["Task"] = self.config.default_task

        frontmatter = yaml.dump(
            data, allow_unicode=True, default_flow_style=False,
            sort_keys=False, width=120,
        )

        body_lines = [f"# {meta.title}", ""]
        if meta.description:
            body_lines += [f"> {meta.description}", ""]
        if meta.text:
            text = meta.text.strip()
            if len(text) > 3000:
                text = text[:3000] + "\n\n*[Contenu tronqué — lire l'article original]*"
            body_lines.append(text)

        return f"---\n{frontmatter}---\n\n" + "\n".join(body_lines) + "\n"

    def _resolve_path(self, meta: ArticleMeta) -> Path:
        base = self.config.vault_path / self.config.notes_subfolder
        site = _sanitize_filename(meta.site_name or meta.domain)
        title = _sanitize_filename(meta.title or meta.url)
        filename = f"{site} - {title}.md"
        return base / filename


def _sanitize_filename(name: str, maxlen: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name[:maxlen]


def _sanitize_tag(tag: str) -> str:
    tag = re.sub(r"[^\w\s\-]", "", tag).strip()
    if " " in tag:
        tag = "".join(word.capitalize() for word in tag.split())
    return tag
