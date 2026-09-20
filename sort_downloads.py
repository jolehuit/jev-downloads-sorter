# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Sort new downloads into folders, with Jev picking the folder.

Run by launchd every time ~/Downloads changes. For each finished download at
the root, one call to Jev (TypeSafe's decision model, via OpenRouter) picks a
destination among the folders you defined. If the API is unreachable, falls
back to an extension map. Never creates folders, never overwrites.
"""

import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DOWNLOADS = Path(os.environ.get("JEV_SORT_DIR", HOME / "Downloads"))
CONFIG = Path(os.environ.get("JEV_SORT_CONFIG", HOME / ".config/jev-downloads-sorter/folders.json"))
LOG = HOME / "Library/Logs/jev-downloads-sorter.log"
ENDPOINT = os.environ.get("JEV_SORT_ENDPOINT", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("JEV_SORT_MODEL", "typesafe/jev-1.13")
STABLE_SECONDS = float(os.environ.get("JEV_SORT_STABLE_SECONDS", "2"))
# Names at the root to leave alone, comma-separated (a folder another tool fills, for instance).
IGNORE = {n.strip() for n in os.environ.get("JEV_SORT_IGNORE", "").split(",") if n.strip()}

# Files the key may live in, first match wins. launchd does not source your shell rc.
KEY_FILES = [
    HOME / ".config/jev-downloads-sorter/env",
    HOME / ".config/zsh/secrets.zsh",
    HOME / ".zshenv",
    HOME / ".env",
]

# Default folders. Override with ~/.config/jev-downloads-sorter/folders.json,
# a {"Folder name": "what goes there"} object. Folders must already exist.
DEFAULT_FOLDERS = {
    "Documents": "things to read: pdf, docx, pptx, txt, md, invoices, resumes, contracts, papers, books",
    "Spreadsheets": "tabular data: xlsx, csv, ods, numbers, database exports",
    "Images": "photos and pictures: jpg, png, heic, webp, svg, gif, logos, generated visuals",
    "Screenshots": "screen captures from macOS or a phone (name starts with Screenshot, Capture d'écran...)",
    "Videos": "video: mp4, mov, webm, screen recordings, footage, renders",
    "Audio": "sound and music: mp3, wav, opus, m4a, stems, beats, samples, voice memos",
    "Archives": "compressed archives not yet extracted: zip, rar, 7z, tar.gz",
    "Installers": "application installers: dmg, pkg, app bundles",
    "Code": "source code and technical data: js, ts, py, sh, json, xml, ipynb, har, sql, scripts, configs",
    "Web pages": "saved web pages: html plus their _files folder",
    "3D": "3D models: glb, usdz, blend, fbx, obj, stl",
    "Folders": "extracted or downloaded folders (projects, kits, exports) that fit no other category",
    "Misc": "anything else: ics, pkpass, unknown formats",
}

# Fallback when the model is unavailable: extension -> default folder name.
EXTENSIONS = {
    "Images": "png jpg jpeg heic webp avif gif svg bmp tiff tif",
    "Videos": "mp4 mov mkv avi webm m4v",
    "Audio": "mp3 opus ogg m4a wav flac aiff aif",
    "Documents": "pdf docx doc odt pptx ppt key txt md rtf pages epub srt",
    "Spreadsheets": "xlsx xls xlsm csv ods numbers",
    "Archives": "zip rar gz tar 7z tgz",
    "Installers": "dmg pkg mcpb",
    "Code": "js mjs jsx ts tsx py sh json jsonl xml ipynb conf mermaid har css yaml yml toml sql",
    "Web pages": "html htm",
    "3D": "glb usdz blend fbx obj stl",
}
BY_EXTENSION = {ext: folder for folder, exts in EXTENSIONS.items() for ext in exts.split()}

# In-progress downloads, per browser.
PARTIAL = (".crdownload", ".part", ".download", ".partial", ".tmp", ".aria2", ".!qb")
TEXT_LIKE = {".txt", ".md", ".csv", ".json", ".html", ".xml", ".js", ".py", ".sh", ".srt"}


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def load_folders():
    if CONFIG.exists():
        folders = json.loads(CONFIG.read_text())
        if not isinstance(folders, dict) or not folders:
            raise ValueError(f"{CONFIG} must be a non-empty object of folder -> description")
        return folders
    return DEFAULT_FOLDERS


def openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    for path in KEY_FILES:
        try:
            for line in path.read_text().splitlines():
                m = re.match(r'\s*(?:export\s+)?OPENROUTER_API_KEY=["\']?([^"\'\s]+)', line)
                if m:
                    return m.group(1)
        except OSError:
            continue
    return None


def candidates(folders):
    for entry in sorted(DOWNLOADS.iterdir()):
        name = entry.name
        if name.startswith(".") or name in folders or name in IGNORE or name.endswith(".app"):
            continue
        if name.lower().endswith(PARTIAL) or name.startswith("Unconfirmed "):
            continue
        yield entry


def size(entry):
    if entry.is_dir():
        return sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
    return entry.stat().st_size


def is_stable(entry):
    """True once the size has not changed for STABLE_SECONDS."""
    try:
        before = size(entry)
        time.sleep(STABLE_SECONDS)
        return entry.exists() and size(entry) == before
    except OSError:
        return False


def origin_url(entry):
    """URL the browser recorded on the file (xattr kMDItemWhereFroms)."""
    try:
        out = subprocess.run(
            ["xattr", "-px", "com.apple.metadata:kMDItemWhereFroms", str(entry)],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        urls = plistlib.loads(bytes.fromhex(out.stdout.replace(" ", "").replace("\n", "")))
        return urls[0] if urls else None
    except Exception:
        return None


def excerpt(entry, limit=400):
    if entry.is_dir():
        names = [p.name for p in list(entry.iterdir())[:12]]
        return "contains: " + ", ".join(names)
    if entry.suffix.lower() in TEXT_LIKE:
        try:
            return entry.read_text(errors="replace")[:limit]
        except OSError:
            return None
    return None


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ask_jev(entry, folders, key):
    file = {
        "name": entry.name,
        "type": "folder" if entry.is_dir() else (entry.suffix.lower().lstrip(".") or "no extension"),
        "size": human(size(entry)),
    }
    if (url := origin_url(entry)):
        file["downloaded_from"] = url
    if (text := excerpt(entry)):
        file["excerpt"] = text
    body = {
        "model": MODEL,
        "state": {"file": file},
        "questions": {
            "folder": {
                "type": "choice",
                "criteria": {name: {"folder": name, "contents": desc} for name, desc in folders.items()},
                "instructions": {
                    "goal": "Pick the folder this download belongs in.",
                    "rules": [
                        "File type comes first, unless the name or the origin clearly says otherwise.",
                        "An extracted folder that is mostly sounds goes with audio, mostly pictures with images, and so on.",
                        "An .html file next to a _files folder is a saved web page.",
                        "The catch-all folder only when nothing else fits.",
                    ],
                },
            }
        },
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.load(resp)
    answer = result["answers"]["folder"]
    choice = answer["choice"]
    if choice not in folders:
        raise ValueError(f"choice outside the folder list: {choice}")
    return choice, answer.get("confidence"), round((time.perf_counter() - started) * 1000)


def by_extension(entry, folders):
    """Fallback. Maps default folder names onto whatever folders exist, by position."""
    names = list(folders)
    defaults = list(DEFAULT_FOLDERS)
    alias = {d: names[i] if i < len(names) else names[-1] for i, d in enumerate(defaults)}
    if entry.is_dir():
        wanted = "Web pages" if entry.name.endswith("_files") else "Folders"
    elif entry.name.startswith(("Screenshot", "Screen Shot", "Capture d’e", "Capture d'e")):
        wanted = "Screenshots"
    else:
        wanted = BY_EXTENSION.get(entry.suffix.lower().lstrip("."), "Misc")
    return wanted if wanted in folders else alias[wanted]


def free_destination(folder, name):
    target = DOWNLOADS / folder / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name) if "." in name[1:] else (name, "")
    n = 2
    while (DOWNLOADS / folder / f"{stem} ({n}){ext}").exists():
        n += 1
    return DOWNLOADS / folder / f"{stem} ({n}){ext}"


def notify(lines):
    text = "\n".join(lines[:4]) + (f"\n… +{len(lines) - 4}" if len(lines) > 4 else "")
    script = f'display notification {json.dumps(text)} with title "Downloads sorted"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)


def main():
    folders = load_folders()
    missing = [f for f in folders if not (DOWNLOADS / f).is_dir()]
    if missing:
        log(f"ERROR missing folders, create them first: {', '.join(missing)}")
        return
    key = openrouter_key()
    if not key:
        log("ERROR no OpenRouter key found, falling back to extensions")
    moved = []
    for entry in list(candidates(folders)):
        if not is_stable(entry):
            log(f"waiting {entry.name} (still being written)")
            continue
        if entry.is_dir() and entry.name.endswith("_files"):
            folder, conf, ms = by_extension(entry, folders), None, 0
        else:
            folder = conf = None
            if key:
                try:
                    folder, conf, ms = ask_jev(entry, folders, key)
                except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError) as e:
                    log(f"ERROR Jev on {entry.name}: {e}")
            if folder is None:
                folder, conf, ms = by_extension(entry, folders), None, 0
        target = free_destination(folder, entry.name)
        try:
            entry.rename(target)
        except OSError as e:
            log(f"ERROR moving {entry.name}: {e}")
            continue
        detail = f"jev {conf:.2f} {ms} ms" if conf is not None else "rule"
        log(f"{entry.name} -> {folder}/ ({detail})")
        moved.append(f"{entry.name} → {folder}")
    if moved:
        notify(moved)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never die silently under launchd
        log(f"FATAL {e!r}")
        sys.exit(1)
