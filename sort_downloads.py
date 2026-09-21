# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Sort new downloads into folders, with a decision model picking the folder.

Run by launchd every time ~/Downloads changes. For each finished download at
the root, one decision picks a destination among the folders you defined:
Jev (TypeSafe's decision model, via OpenRouter) or Laya (open weights, runs on
your machine). If the model is unavailable, falls back to an extension map.
Never creates folders, never overwrites.
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
BACKEND = os.environ.get("JEV_SORT_BACKEND", "jev")  # "jev" (OpenRouter), "laya" (local model) or "rules" (no model)
ENDPOINT = os.environ.get("JEV_SORT_ENDPOINT", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("JEV_SORT_MODEL", "typesafe/jev-1.13")
LAYA_MODEL = os.environ.get("JEV_SORT_LAYA_MODEL", "convaiinnovations/laya")
LAYA_SUBFOLDER = os.environ.get("JEV_SORT_LAYA_SUBFOLDER", "typed-decisions")
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
    "Images": "photos and pictures: jpg, png, heic, webp, svg, gif, logos, generated visuals; also a folder that contains mostly images or logos",
    "Screenshots": "screen captures from macOS or a phone (name starts with Screenshot, Capture d'écran...)",
    "Videos": "video: mp4, mov, webm, screen recordings, footage, renders; also a folder of video files",
    "Audio": "sound and music: mp3, wav, opus, m4a, stems, beats, samples, voice memos; also a folder that contains mostly audio files (drum kit, sample pack, stems)",
    "Archives": "compressed archives not yet extracted: zip, rar, 7z, tar.gz",
    "Installers": "application installers: dmg, pkg, app bundles",
    "Code": "source code and technical data: js, ts, py, sh, json, xml, ipynb, har, sql, scripts, configs; also a folder that is a code project",
    "Web pages": "saved web pages: html plus their _files folder",
    "3D": "3D models: glb, usdz, blend, fbx, obj, stl",
    "Folders": "a folder with mixed or unknown contents that fits no other category",
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
SCREENSHOT_PREFIXES = ("Screenshot", "Screen Shot", "Capture d\u2019e", "Capture d'e")
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


def settled(entries):
    """The entries whose size did not change over STABLE_SECONDS, measured once for the whole batch."""
    before = {}
    for e in entries:
        try:
            before[e] = size(e)
        except OSError:
            pass
    time.sleep(STABLE_SECONDS)
    out = []
    for e, b in before.items():
        try:
            if e.exists() and size(e) == b:
                out.append(e)
            else:
                log(f"waiting {e.name} (still being written)")
        except OSError:
            pass
    return out


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


def dominant_kind(entry):
    """(default folder name, share) of what a folder mostly holds, from the extension map."""
    files = [p for p in entry.rglob("*") if p.is_file() and not p.name.startswith(".")][:500]
    if not files:
        return None, 0
    kinds = {}
    for p in files:
        k = BY_EXTENSION.get(p.suffix.lower().lstrip("."), "other")
        kinds[k] = kinds.get(k, 0) + 1
    top, n = max(kinds.items(), key=lambda kv: kv[1])
    return top, n / len(files)


def contents_kind(entry):
    """'3 files, all images', for the models."""
    top, share = dominant_kind(entry)
    if top is None:
        return None
    count = len([p for p in entry.rglob("*") if p.is_file() and not p.name.startswith(".")][:500])
    word = "all" if share == 1 else ("mostly" if share >= 0.6 else "some")
    return f"{count} files, {word} {KIND_LABEL.get(top, 'files of unknown kind')}"


KIND_LABEL = {"Images": "images", "Videos": "video files", "Audio": "audio files", "Documents": "documents",
              "Spreadsheets": "spreadsheets", "Archives": "archives", "Installers": "installers", "Code": "code",
              "Web pages": "web pages", "3D": "3D models"}


def excerpt(entry, limit=400):
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


GOAL = "Pick the folder this download belongs in."
RULES = [
    "File type comes first, unless the name or the origin clearly says otherwise.",
    "For a folder, decide from what it contains: mostly sounds goes with audio, mostly pictures with images, and so on.",
    "An .html file next to a _files folder is a saved web page.",
    "The catch-all folder only when nothing else fits.",
]


def describe(entry):
    file = {
        "name": entry.name,
        "type": "folder" if entry.is_dir() else (entry.suffix.lower().lstrip(".") or "no extension"),
        "size": human(size(entry)),
    }
    if entry.is_dir():
        file["contents"] = ", ".join(p.name for p in list(entry.iterdir())[:12])
        if (kind := contents_kind(entry)):
            file["contents_kind"] = kind
    elif entry.name.startswith(SCREENSHOT_PREFIXES):
        file["kind"] = "screenshot"
    elif (kind := BY_EXTENSION.get(entry.suffix.lower().lstrip("."))):
        file["kind"] = KIND_LABEL.get(kind, kind)
    if (url := origin_url(entry)):
        file["downloaded_from"] = url
    if not entry.is_dir() and (text := excerpt(entry)):
        file["excerpt"] = text
    return file


def criteria(folders):
    return {name: {"folder": name, "contents": desc} for name, desc in folders.items()}


def ask_jev(entry, folders, key):
    body = {
        "model": MODEL,
        "state": {"file": describe(entry)},
        "questions": {"folder": {"type": "choice", "criteria": criteria(folders), "instructions": {"goal": GOAL, "rules": RULES}}},
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


_laya_agent = None


def laya_agent():
    """Laya loaded once per process, on the GPU when there is one (MPS on a Mac).
    transformers randomly initializes every weight before loading the checkpoint,
    which is 27 s of the 30 s load on an M1 Pro; skipping that brings it to 1.6 s."""
    global _laya_agent
    if _laya_agent is None:
        import laya
        try:
            from transformers.initialization import no_init_weights
        except ImportError:  # transformers < 5
            from transformers.modeling_utils import no_init_weights
        with no_init_weights():
            _laya_agent = laya.load(LAYA_MODEL, subfolder=LAYA_SUBFOLDER or None)
    return _laya_agent


def ask_laya(entry, folders):
    """Laya's question budget is 256 tokens shared by all options, so the options
    are plain strings and the instructions one sentence; longer hurts."""
    agent = laya_agent()
    questions = {"folder": {"type": "choice", "criteria": dict(folders),
                            "instructions": GOAL + " Decide from the file type; for a folder, decide from what it contains."}}
    started = time.perf_counter()
    answer = agent.predict({"file": describe(entry)}, questions)["answers"]["folder"]
    choice = answer["choice"]
    if choice not in folders:
        raise ValueError(f"choice outside the folder list: {choice}")
    return choice, answer.get("confidence"), round((time.perf_counter() - started) * 1000)


def by_extension(entry, folders):
    """No model: screenshot names, the extension map, and for a folder what it
    mostly holds (60 % or more of one kind). Default folder names are mapped
    onto whatever folders exist, by position."""
    names = list(folders)
    defaults = list(DEFAULT_FOLDERS)
    alias = {d: names[i] if i < len(names) else names[-1] for i, d in enumerate(defaults)}
    if entry.is_dir():
        top, share = dominant_kind(entry)
        if entry.name.endswith("_files"):
            wanted = "Web pages"
        elif top in DEFAULT_FOLDERS and share >= 0.6:
            wanted = top
        else:
            wanted = "Folders"
    elif entry.name.startswith(SCREENSHOT_PREFIXES):
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
    key = openrouter_key() if BACKEND == "jev" else None
    if BACKEND == "jev" and not key:
        log("ERROR no OpenRouter key found, falling back to rules")
    moved = []
    for entry in settled(list(candidates(folders))):
        if entry.is_dir() and entry.name.endswith("_files"):
            folder, conf, ms = by_extension(entry, folders), None, 0
        else:
            folder = conf = None
            if BACKEND == "rules":
                pass
            elif BACKEND == "laya":
                try:
                    folder, conf, ms = ask_laya(entry, folders)
                except Exception as e:  # torch/laya raise all sorts; the fallback must run
                    log(f"ERROR Laya on {entry.name}: {e!r}")
            elif key:
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
        detail = f"{BACKEND} {conf:.2f} {ms} ms" if conf is not None else "rules"
        log(f"{entry.name} -> {folder}/ ({detail})")
        moved.append(f"{entry.name} → {folder}")
    if moved:
        notify(moved)


if __name__ == "__main__":
    if "--warm" in sys.argv:  # download and load the local model once, at install time
        started = time.perf_counter()
        laya_agent()
        print(f"Laya ready ({time.perf_counter() - started:.1f} s)")
        sys.exit(0)
    try:
        main()
    except Exception as e:  # never die silently under launchd
        log(f"FATAL {e!r}")
        sys.exit(1)
