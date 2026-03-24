#!/usr/bin/env python3
"""
Automatisierter Export von Apidog-Markdown inklusive lokaler Bilderspiegelung
und optionaler PDF-Erzeugung via pandoc/extra Docker-Image.

Geeignet für Linux-basierte Azure DevOps Agents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

LOG = logging.getLogger("apidog-export")

DEFAULT_LOGIN_URL = "https://api.apidog.com/api/v1/login?locale=en-US"
DEFAULT_EXPORT_URL_TEMPLATE = (
    "https://api.apidog.com/api/v1/projects/{project_id}/export-markdown"
    "?__xProjectId={project_id}&locale=en-US"
)
DEFAULT_PROJECT_ID = "704579"
DEFAULT_RELEVANT_SUBPATH = Path("waySCI") / "Documentation"

# Markdown-Bildsyntax: ![alt](url) oder ![alt](<url>) plus optionaler Titel
IMAGE_PATTERN = re.compile(
    r"(!\[[^\]]*\]\()\s*(<)?([^\s>)]+)(>)?(\s+\"[^\"]*\")?\s*(\))"
)


class PipelineError(RuntimeError):
    """Domänenspezifischer Fehler für klare Exit-Codes und Logs."""


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PipelineError(
            f"Umgebungsvariable '{name}' fehlt oder ist leer."
        )
    return value


def extract_token(payload: Dict) -> Optional[str]:
    """Versucht bekannte Token-Felder aus typischen API-Strukturen zu lesen."""
    candidates: Iterable[Tuple[str, ...]] = (
        ("token",),
        ("accessToken",),
        ("bearerToken",),
        ("data", "token"),
        ("data", "accessToken"),
        ("data", "bearerToken"),
    )

    for path in candidates:
        cur = payload
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if isinstance(cur, str) and cur.strip():
            return cur.strip()

    # Fallback: rekursiv nach einem String-Feld mit token im Namen suchen
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if isinstance(value, str) and "token" in key.lower() and value.strip():
                    return value.strip()
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)

    return None


def login(session: requests.Session, email: str, password: str, login_url: str) -> str:
    LOG.info("Authentifizierung bei Apidog…")
    payload = {
        "account": email,
        "password": password,
        "loginType": "IntlEmailPassword",
    }

    response = session.post(login_url, json=payload, timeout=60)
    if response.status_code >= 400:
        raise PipelineError(
            f"Login fehlgeschlagen ({response.status_code}): {response.text[:500]}"
        )

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise PipelineError("Login-Response ist kein gültiges JSON.") from exc

    token = extract_token(data)
    if not token:
        raise PipelineError(
            "Bearer-Token konnte nicht aus Login-Response extrahiert werden."
        )

    LOG.info("Login erfolgreich; Token extrahiert.")
    return token


def download_export_zip(
    session: requests.Session,
    token: str,
    project_id: str,
    export_url_template: str,
    zip_path: Path,
) -> None:
    export_url = export_url_template.format(project_id=project_id)
    LOG.info("Exportiere Markdown als ZIP für Projekt %s…", project_id)

    headers = {"Authorization": f"Bearer {token}"}
    response = session.post(export_url, headers=headers, timeout=120, stream=True)

    if response.status_code >= 400:
        raise PipelineError(
            f"Export fehlgeschlagen ({response.status_code}): {response.text[:500]}"
        )

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = zip_path.with_suffix(zip_path.suffix + ".tmp")

    with tmp_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)

    if tmp_path.stat().st_size == 0:
        raise PipelineError("Leeres ZIP vom Export erhalten.")

    tmp_path.replace(zip_path)
    LOG.info("ZIP gespeichert: %s", zip_path)


def extract_zip(zip_path: Path, extract_dir: Path) -> None:
    LOG.info("Entpacke ZIP nach %s…", extract_dir)
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise PipelineError(f"Ungültiges ZIP-Archiv: {zip_path}") from exc


def find_markdown_root(extract_dir: Path, relevant_subpath: Path) -> Path:
    direct = extract_dir / relevant_subpath
    if direct.exists() and direct.is_dir():
        return direct

    matches = list(extract_dir.glob(f"**/{relevant_subpath.as_posix()}"))
    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise PipelineError(
            f"Markdown-Pfad '{relevant_subpath}' wurde im entpackten Archiv nicht gefunden."
        )

    raise PipelineError(
        f"Mehrdeutiger Markdown-Pfad '{relevant_subpath}' (Treffer: {len(matches)})."
    )


def safe_file_name_from_url(url: str) -> Tuple[str, str, str]:
    parsed = urlparse(url)
    netloc = parsed.netloc or "local"
    raw_path = parsed.path or "/unnamed"
    suffix = Path(raw_path).suffix

    if not suffix:
        suffix = ".bin"

    normalized_path = raw_path.lstrip("/") or "unnamed"
    if normalized_path.endswith("/"):
        normalized_path += "index"

    normalized_path = str(Path(normalized_path))

    query_part = parsed.query
    if query_part:
        digest = hashlib.sha1(query_part.encode("utf-8")).hexdigest()[:10]
        base = Path(normalized_path)
        normalized_path = str(base.with_name(f"{base.stem}_{digest}{base.suffix}"))

    return netloc, normalized_path, suffix


def download_image(
    session: requests.Session,
    token: str,
    url: str,
    assets_root: Path,
    image_base_url: str,
    cache: Dict[str, Path],
) -> Path:
    if url in cache:
        return cache[url]

    absolute_url = urljoin(image_base_url, url) if url.startswith("/") else url
    if not absolute_url.startswith(("http://", "https://")):
        raise PipelineError(f"Nicht unterstützte Bild-URL: {url}")

    netloc, normalized_path, _ = safe_file_name_from_url(absolute_url)
    local_path = assets_root / netloc / normalized_path
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if local_path.exists() and local_path.stat().st_size > 0:
        LOG.debug("Bild bereits vorhanden, überspringe Download: %s", absolute_url)
        cache[url] = local_path
        return local_path

    headers = {"Authorization": f"Bearer {token}"}
    response = session.get(absolute_url, headers=headers, timeout=90, stream=True)

    if response.status_code >= 400:
        raise PipelineError(
            f"Bilddownload fehlgeschlagen ({response.status_code}) für URL: {absolute_url}"
        )

    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    with tmp_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

    if tmp_path.stat().st_size == 0:
        raise PipelineError(f"Leere Bilddatei erhalten: {absolute_url}")

    tmp_path.replace(local_path)
    cache[url] = local_path
    LOG.info("Bild gespeichert: %s", local_path)
    return local_path


def rewrite_markdown_images(
    session: requests.Session,
    token: str,
    markdown_root: Path,
    assets_root: Path,
    image_base_url: str,
) -> int:
    LOG.info("Verarbeite Markdown-Dateien unter %s…", markdown_root)
    image_cache: Dict[str, Path] = {}
    replacements = 0

    md_files = sorted(markdown_root.rglob("*.md"))
    if not md_files:
        LOG.warning("Keine Markdown-Dateien gefunden.")
        return 0

    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        original = content

        def _replace(match: re.Match) -> str:
            nonlocal replacements
            prefix, lt, raw_url, gt, title, suffix = match.groups()
            url = raw_url.strip()

            # Bereits lokal oder nicht-dowloadbar
            if url.startswith(("file://", "data:", "mailto:")):
                return match.group(0)
            if not url.startswith(("http://", "https://", "/")):
                return match.group(0)

            local_image = download_image(
                session=session,
                token=token,
                url=url,
                assets_root=assets_root,
                image_base_url=image_base_url,
                cache=image_cache,
            )

            relative = os.path.relpath(local_image, start=md_file.parent)
            relative = relative.replace(os.sep, "/")
            replacements += 1

            return f"{prefix}{lt or ''}{relative}{gt or ''}{title or ''}{suffix}"

        content = IMAGE_PATTERN.sub(_replace, content)
        if content != original:
            md_file.write_text(content, encoding="utf-8")
            LOG.info("Markdown aktualisiert: %s", md_file)

    LOG.info("Bildreferenzen ersetzt: %s", replacements)
    return replacements


def run_pdf_generation(markdown_root: Path) -> None:
    should_generate = os.getenv("GENERATE_PDF", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if not should_generate:
        LOG.info("PDF-Generierung deaktiviert (GENERATE_PDF=false).")
        return

    template = os.getenv("PDF_TEMPLATE", "valantic.latex")
    output = os.getenv("PDF_OUTPUT", "output.pdf")
    inputs_env = os.getenv(
        "PDF_INPUT_FILES",
        "index.md,01_Übersicht.md,02_Installation.md,03_Konfiguration.md",
    )
    input_files = [part.strip() for part in inputs_env.split(",") if part.strip()]

    if not input_files:
        raise PipelineError("PDF_INPUT_FILES enthält keine gültigen Dateien.")

    missing_inputs = [f for f in input_files if not (markdown_root / f).exists()]
    if missing_inputs:
        raise PipelineError(
            "Fehlende Markdown-Dateien für PDF-Generierung: " + ", ".join(missing_inputs)
        )

    if not (markdown_root / template).exists():
        raise PipelineError(f"Template nicht gefunden: {template}")

    cmd = [
        "docker",
        "run",
        "--rm",
        "--volume",
        f"{markdown_root.resolve()}:/data",
        "pandoc/extra",
        *input_files,
        "--template",
        template,
        "-o",
        output,
        "--toc",
    ]

    LOG.info("Starte PDF-Generierung via Docker…")
    LOG.debug("Befehl: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise PipelineError(
            "PDF-Generierung fehlgeschlagen:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    LOG.info("PDF erfolgreich erstellt: %s", markdown_root / output)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apidog-Export mit Bild-Download und optionaler PDF-Generierung für Azure DevOps"
        )
    )
    parser.add_argument(
        "--project-id",
        default=os.getenv("APIDOG_PROJECT_ID", DEFAULT_PROJECT_ID),
        help="Apidog Project-ID (default via APIDOG_PROJECT_ID oder 704579)",
    )
    parser.add_argument(
        "--workdir",
        default=os.getenv("WORKDIR", "./build/apidog"),
        help="Arbeitsverzeichnis für ZIP/Extraktion",
    )
    parser.add_argument(
        "--relevant-subpath",
        default=os.getenv("RELEVANT_SUBPATH", str(DEFAULT_RELEVANT_SUBPATH)),
        help="Pfad innerhalb des Exports zu den Markdown-Dateien",
    )
    parser.add_argument(
        "--image-base-url",
        default=os.getenv("APIDOG_IMAGE_BASE_URL", "https://api.apidog.com"),
        help="Basis-URL für relative Bildpfade",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Debug-Logging aktivieren"
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        email = required_env("APIDOG_EMAIL")
        password = required_env("APIDOG_PASSWORD")

        workdir = Path(args.workdir).resolve()
        zip_path = workdir / "apidog_export.zip"
        extract_dir = workdir / "extracted"

        session = requests.Session()

        token = login(session, email, password, DEFAULT_LOGIN_URL)
        download_export_zip(
            session=session,
            token=token,
            project_id=args.project_id,
            export_url_template=DEFAULT_EXPORT_URL_TEMPLATE,
            zip_path=zip_path,
        )
        extract_zip(zip_path=zip_path, extract_dir=extract_dir)

        markdown_root = find_markdown_root(
            extract_dir=extract_dir,
            relevant_subpath=Path(args.relevant_subpath),
        )
        assets_root = markdown_root / "assets" / "img"

        rewrite_markdown_images(
            session=session,
            token=token,
            markdown_root=markdown_root,
            assets_root=assets_root,
            image_base_url=args.image_base_url,
        )

        run_pdf_generation(markdown_root=markdown_root)

        LOG.info("Pipeline-Schritt erfolgreich abgeschlossen.")
        return 0

    except PipelineError as exc:
        LOG.error("Fehler: %s", exc)
        return 2
    except requests.RequestException as exc:
        LOG.error("Netzwerk-/HTTP-Fehler: %s", exc)
        return 3
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Unerwarteter Fehler: %s", exc)
        return 99


if __name__ == "__main__":
    sys.exit(main())
