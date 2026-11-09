from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from nyora import Nyora
except Exception:  # pragma: no cover - handled at runtime with a clean API error.
    Nyora = None


ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
CACHE_PATH = ROOT / "work" / "mangaflow_cache.json"
DOWNLOADS_PATH = ROOT / "work" / "downloads.json"
OFFLINE_DIR = ROOT / "work" / "offline"
CACHE_VERSION = 2
PAGE_TTL_SECONDS = 60 * 60
DETAILS_TTL_SECONDS = 10 * 60
SEARCH_TTL_SECONDS = 30 * 60
MAX_ALTERNATIVES = 3
MAX_SEARCH_RESULTS = 60
MAX_RESULTS_PER_SOURCE = 8
SEARCH_TIMEOUT_SECONDS = 15
# Verified working against live sites (Nov 2026). MangaDex/ComicK/MangaPlus/Aqua/Flame
# currently return empty responses, so they are available in the source manager but not defaults.
DEFAULT_SOURCE_IDS = [
    "parser:ASURASCANS",
    "parser:WEEBCENTRAL",
    "parser:MANGAPILL",
    "parser:ZINMANGA",
    "parser:MANGAGO",
    "parser:MANGAFREAK",
    "parser:MANGADNA",
    "parser:MANGAREAD",
    "parser:DEMONICSCANS",
    "parser:ATSUMOE",
    "parser:WEBTOONS_EN",
]
NSFW_NAME_WORDS = ["hentai", "porn", "18", "adult", "sex", "xxx", "yaoi"]

_client: Any | None = None
_source_names: dict[str, str] = {}
_cache: dict[str, Any] = {"manga": {}, "chapters": {}, "searches": {}}
_page_cache: dict[str, dict[str, Any]] = {}
_downloads: dict[str, dict[str, Any]] = {}
_downloads_lock = threading.RLock()
_download_pool = ThreadPoolExecutor(max_workers=8)
_cancelled_jobs: set[str] = set()

DOWNLOAD_SETTINGS_PATH = ROOT / "work" / "download-settings.json"
DEFAULT_DOWNLOAD_SETTINGS: dict[str, Any] = {
    "concurrency": 2,
    "pageConcurrency": 3,
    "retries": 2,
    "retryDelay": 1.5,
    "timeout": 60,
    "skipExisting": True,
    "paused": False,
}
_download_settings: dict[str, Any] = dict(DEFAULT_DOWNLOAD_SETTINGS)
_gate_cond = threading.Condition()
_active_downloads = 0


def _clamp_download_settings() -> None:
    s = _download_settings
    s["concurrency"] = max(1, min(6, int(s["concurrency"])))
    s["pageConcurrency"] = max(1, min(8, int(s["pageConcurrency"])))
    s["retries"] = max(0, min(5, int(s["retries"])))
    s["retryDelay"] = max(0.0, min(10.0, float(s["retryDelay"])))
    s["timeout"] = max(10, min(180, int(s["timeout"])))
    s["skipExisting"] = bool(s["skipExisting"])
    s["paused"] = bool(s["paused"])


def load_download_settings() -> None:
    global _download_settings
    _download_settings = dict(DEFAULT_DOWNLOAD_SETTINGS)
    if DOWNLOAD_SETTINGS_PATH.exists():
        try:
            saved = json.loads(DOWNLOAD_SETTINGS_PATH.read_text())
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in _download_settings:
                        _download_settings[key] = value
        except Exception:
            pass
    _clamp_download_settings()


def save_download_settings() -> None:
    DOWNLOAD_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_SETTINGS_PATH.write_text(json.dumps(_download_settings, indent=2, sort_keys=True))


def _acquire_download_slot() -> None:
    global _active_downloads
    with _gate_cond:
        while _download_settings["paused"] or _active_downloads >= _download_settings["concurrency"]:
            _gate_cond.wait()
        _active_downloads += 1


def _release_download_slot() -> None:
    global _active_downloads
    with _gate_cond:
        _active_downloads -= 1
        _gate_cond.notify_all()


load_download_settings()


def http_client(**kwargs: Any) -> httpx.Client:
    try:
        return httpx.Client(**kwargs)
    except httpx.InvalidURL:
        # Malformed proxy env vars (e.g. NO_PROXY with IPv6/CIDR entries) must not break fetching.
        return httpx.Client(trust_env=False, **kwargs)


def sanitize_proxy_env() -> None:
    """httpx (and Nyora) crash on NO_PROXY entries like '[::1]' or CIDR ranges; drop them."""
    try:
        httpx.Client().close()
        return
    except httpx.InvalidURL:
        pass
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name, "")
        kept = [entry.strip() for entry in raw.split(",") if re.fullmatch(r"[A-Za-z0-9.*_-]+", entry.strip())]
        os.environ[name] = ",".join(kept)
    try:
        httpx.Client().close()
        return
    except httpx.InvalidURL:
        pass
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)


sanitize_proxy_env()
_image_client = http_client(timeout=30, follow_redirects=True, limits=httpx.Limits(max_connections=16))


def load_cache() -> None:
    global _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text())
        except Exception:
            _cache = {"manga": {}, "chapters": {}, "searches": {}}
    if _cache.get("version") != CACHE_VERSION:
        _cache = {"version": CACHE_VERSION, "manga": {}, "chapters": {}, "searches": {}}
    _cache.setdefault("version", CACHE_VERSION)
    _cache.setdefault("manga", {})
    _cache.setdefault("chapters", {})
    _cache.setdefault("searches", {})
    _cache.setdefault("details", {})
    for query, entry in list(_cache["searches"].items()):
        if isinstance(entry, list):
            entry = {"at": 0, "payload": {"results": entry, "errors": [], "sourceStatus": []}}
        elif "payload" not in entry:
            entry = {"at": entry.get("at", 0), "payload": {"results": entry.get("results", []), "errors": [], "sourceStatus": []}}
        if time.time() - entry.get("at", 0) > SEARCH_TTL_SECONDS:
            del _cache["searches"][query]
        else:
            _cache["searches"][query] = entry
    if DOWNLOADS_PATH.exists():
        try:
            saved = json.loads(DOWNLOADS_PATH.read_text())
            _downloads.update(saved if isinstance(saved, dict) else {})
        except Exception:
            pass
    for job in _downloads.values():
        if job.get("status") in {"downloading", "queued"}:
            job.update(status="failed", error="Download was interrupted while MangaFlow was closed.")
    save_downloads()


def save_cache() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_cache, indent=2, sort_keys=True))


def save_downloads() -> None:
    DOWNLOADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DOWNLOADS_PATH.with_suffix(".tmp")
    with _downloads_lock:
        temporary.write_text(json.dumps(_downloads, indent=2, sort_keys=True))
        temporary.replace(DOWNLOADS_PATH)


def update_download(job_id: str, **changes: Any) -> None:
    with _downloads_lock:
        job = _downloads.get(job_id)
        if job is None:
            return
        job.update(changes, updatedAt=time.time())
    save_downloads()


def nyora_client() -> Any:
    global _client
    if Nyora is None:
        raise HTTPException(500, "Nyora is not installed. Run: pip install fastapi uvicorn nyora")
    if _client is None:
        try:
            helper_port = ROOT / "work" / "nyora-helper.port"
            os.environ.setdefault("NYORA_HELPER_PORT_FILE", str(helper_port))
            helper_port = Path(os.environ["NYORA_HELPER_PORT_FILE"]).expanduser()
            if helper_port.exists():
                try:
                    port = helper_port.read_text().strip()
                    httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).raise_for_status()
                except Exception:
                    helper_port.unlink(missing_ok=True)
            _client = Nyora(timeout=180, retries=1)
        except Exception as exc:
            raise HTTPException(503, f"Could not start/connect to Nyora helper: {exc}") from exc
    return _client


def engine_call(callback: Any) -> Any:
    """Retry once with a fresh helper when Nyora's local engine has died."""
    global _client
    try:
        return callback()
    except Exception as exc:
        message = str(exc).casefold()
        if "connection refused" not in message and "could not reach the nyora engine" not in message:
            raise
        stale = _client
        _client = None
        if stale is not None:
            try:
                stale.close()
            except Exception:
                pass
        return callback()


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_cache()
    try:
        yield
    finally:
        if _client is not None:
            _client.close()
        _image_client.close()
        _download_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="MangaFlow", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DownloadRequest(BaseModel):
    source: str
    id: str
    label: str = ""
    title: str = ""
    mangaTitle: str = ""
    mangaId: str = ""
    mangaSource: str = ""


class BatchChapter(BaseModel):
    source: str
    id: str
    label: str = ""
    title: str = ""


class BatchDownloadRequest(BaseModel):
    chapters: list[BatchChapter]
    mangaTitle: str = ""
    mangaId: str = ""
    mangaSource: str = ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/sources")
def sources() -> dict[str, Any]:
    return {"sources": [{"id": source, "name": label} for source, label in fast_sources()]}


def obj(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {name: getattr(value, name) for name in dir(value) if not name.startswith("_")}


def val(value: Any, *names: str, default: Any = "") -> Any:
    data = obj(value)
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


def source_name(source: str, fallback: str = "") -> str:
    if source in _source_names:
        return _source_names[source]
    try:
        for item in nyora_client().sources.list():
            source_id = str(val(item, "id", default=""))
            _source_names[source_id] = str(val(item, "name", default=source_id))
    except Exception:
        pass
    return _source_names.get(source, fallback or source)


def installed_sources() -> dict[str, Any]:
    try:
        return {str(val(item, "id", default="")): item for item in nyora_client().sources.list()}
    except Exception:
        return {}


def fast_sources() -> list[tuple[str, str]]:
    installed = installed_sources()
    result: list[tuple[str, str]] = []
    for source_id in DEFAULT_SOURCE_IDS:
        if not installed or source_id in installed:
            result.append((source_id, source_name(source_id)))
    if result:
        return result
    for source_id, item in installed.items():
        name = str(val(item, "name", default=source_id))
        if not any(word in name.casefold() for word in NSFW_NAME_WORDS):
            result.append((source_id, name))
        if len(result) >= 5:
            break
    return result


@app.get("/api/sources/all")
def all_sources() -> dict[str, Any]:
    result: list[dict[str, str]] = []
    for source_id, item in installed_sources().items():
        name = str(val(item, "name", default=source_id))
        lang = str(val(item, "lang", default=""))
        nsfw = val(item, "nsfw", default="")
        if nsfw or any(word in name.casefold() for word in NSFW_NAME_WORDS):
            continue
        if lang not in {"en", "all"}:
            continue
        result.append({"id": source_id, "name": name, "lang": lang})
    result.sort(key=lambda entry: entry["name"].casefold())
    return {"sources": result, "defaults": DEFAULT_SOURCE_IDS}


def manga_key(source: str, manga_id: str) -> str:
    return f"{source}:{manga_id}"


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.casefold())


def clean_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def media_url(url: str) -> str:
    if not url or url.startswith(("http://", "https://", "data:", "/api/")):
        return url
    if url.startswith("/"):
        base_url = str(getattr(nyora_client(), "base_url", "")).rstrip("/")
        return f"{base_url}{url}" if base_url else url
    return url


def normalize_manga(entry: Any, source: str, source_label: str = "") -> dict[str, str]:
    item = obj(entry)
    def clean_type(value: Any) -> str:
        text = str(value or "").strip()
        return text if re.fullmatch(r"[A-Za-z][A-Za-z -]{0,19}", text) else ""

    raw_type = clean_type(val(item, "type", "manga_type", "mangaType", "content_type", "contentType", default=""))
    if not raw_type:
        raw_type = clean_type(val(val(item, "source", default={}), "type", "manga_type", "content_type", default=""))
    type_text = json.dumps({"type": raw_type, "tags": val(item, "tags", default=[])}, default=str).casefold()
    type_match = re.search(r"\b(manga|manhwa|manhua)\b", type_text)
    if type_match:
        raw_type = type_match.group(1)
    elif raw_type.casefold() in {"", "unknown"}:
        raw_type = {
            "parser:ASURASCANS": "Manhwa",
            "parser:FLAMECOMICS": "Manhwa",
            "parser:WEBTOONS": "Manhwa",
            "parser:WEBTOONS_EN": "Manhwa",
            "parser:MANGAPLUSPARSER": "Manga",
        }.get(source, "Unknown")
    manga_id = str(val(item, "id", default=val(item, "url", "public_url", default="")))
    url = str(val(item, "url", "public_url", default=manga_id))
    cover = media_url(str(val(item, "large_cover_url", "largeCoverUrl", "cover_url", "coverUrl", default="")))
    result = {
        "id": manga_id,
        "source": source,
        "sourceName": source_label or source_name(source),
        "title": str(val(item, "title", default="Untitled")),
        "cover": cover,
        "url": url,
        "type": str(raw_type or "Unknown"),
    }
    _cache["manga"][manga_key(source, manga_id)] = result
    return result


def normalize_page(page: Any) -> dict[str, Any]:
    data = obj(page)
    return {
        "url": media_url(str(val(data, "url", default=""))),
        "headers": dict(val(data, "headers", default={}) or {}),
    }


def chapter_number(chapter: Any) -> float:
    raw = val(chapter, "number", default=None)
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or val(chapter, "title", default=""))
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def chapter_label(number: float) -> str:
    return str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip(".")


def normalize_chapter(chapter: Any, source: str, source_label: str = "") -> dict[str, Any]:
    number = chapter_number(chapter)
    data = obj(chapter)
    chapter_id = str(val(data, "id", default=val(data, "url", default="")))
    return {
        "id": chapter_id,
        "source": source,
        "sourceName": source_label or source_name(source),
        "title": str(val(data, "title", default=f"Chapter {chapter_label(number)}")),
        "number": number,
        "url": str(val(data, "url", default=chapter_id)),
        "branch": val(data, "branch", default=None),
    }


def details_for(source: str, manga: dict[str, Any]) -> Any:
    return engine_call(lambda: nyora_client().manga.details(source, manga["url"], manga_id=manga.get("id")))


def alternative_manga(primary: dict[str, Any], title: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = [primary]
    seen = {manga_key(primary["source"], primary["id"])}
    wanted = title_key(title)
    for search in _cache.get("searches", {}).values():
        for item in search.get("payload", {}).get("results", []):
            if title_key(str(item.get("title", ""))) != wanted:
                continue
            key = manga_key(item["source"], item["id"])
            if key in seen:
                continue
            found.append(item)
            seen.add(key)
            if len(found) >= MAX_ALTERNATIVES + 1:
                return found
    return found


def clean_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text else exc.__class__.__name__


def safe_filename(value: str, fallback: str = "chapter") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip().replace(" ", "-")
    return cleaned[:90].strip(".-") or fallback


def extension_for(content_type: str, url: str) -> str:
    lower_type = content_type.split(";", 1)[0].strip().lower()
    if lower_type == "image/png":
        return ".png"
    if lower_type == "image/webp":
        return ".webp"
    if lower_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    match = re.search(r"\.(jpe?g|png|webp|gif)(?:[?#]|$)", url, flags=re.IGNORECASE)
    return f".{match.group(1).lower().replace('jpeg', 'jpg')}" if match else ".jpg"


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    source: str = Query("all"),
    sources: str = Query(""),
    page: int = Query(1, ge=1, le=20),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    source_status: list[dict[str, Any]] = []

    installed = installed_sources()
    if source != "all":
        pool = [(source, source_name(source))]
        if installed and source not in installed:
            raise HTTPException(400, "Selected source is not installed.")
    elif sources.strip():
        wanted = [item.strip() for item in sources.split(",") if item.strip()]
        pool = [(sid, source_name(sid)) for sid in dict.fromkeys(wanted) if not installed or sid in installed]
        if not pool:
            raise HTTPException(400, "None of the selected sources are available.")
    else:
        pool = fast_sources()
    if not pool:
        raise HTTPException(400, "No sources enabled. Open Library → Sources and enable at least one.")

    pool_key = ",".join(sorted(sid for sid, _ in pool))
    query_key = f"{q.strip().casefold()}|source={source}|pool={pool_key}|page={page}"
    searches = _cache.get("searches", {})
    cached = searches.get(query_key)
    if cached and not refresh and time.time() - cached.get("at", 0) < SEARCH_TTL_SECONDS:
        return {**cached["payload"], "cached": True, "page": page}

    nyora_client()
    source_results: dict[str, list[Any]] = {}
    search_client = Nyora(base_url=nyora_client().base_url, timeout=SEARCH_TIMEOUT_SECONDS, retries=0)
    try:
        def search_source(sid: str) -> tuple[str, list[Any]]:
            found = search_client.manga.search(sid, q, page=page)
            return sid, list(val(found, "entries", default=[]))

        with ThreadPoolExecutor(max_workers=max(1, min(12, len(pool)))) as pool_exec:
            pending = {pool_exec.submit(search_source, sid): (sid, label) for sid, label in pool}
            for future in as_completed(pending):
                sid, label = pending[future]
                try:
                    _, entries = future.result()
                    source_results[sid] = entries[:MAX_RESULTS_PER_SOURCE]
                except Exception as exc:
                    errors.append({"source": sid, "error": clean_error(exc)})
    finally:
        search_client.close()

    normalized: dict[str, list[dict[str, str]]] = {}
    for sid, label in pool:
        entries = []
        for entry in source_results.get(sid, []):
            entries.append(normalize_manga(entry, sid, label))
        normalized[sid] = entries
        error = next((item["error"] for item in errors if item["source"] == sid), None)
        source_status.append({
            "source": sid,
            "name": label,
            "count": len(entries),
            "status": "error" if error else ("ok" if entries else "empty"),
            "error": error,
        })

    seen: set[str] = set()
    queues = {sid: iter(entries) for sid, entries in normalized.items()}
    while len(results) < MAX_SEARCH_RESULTS:
        added = False
        for sid, _ in pool:
            item = next(queues[sid], None)
            if item is None:
                continue
            added = True
            key = manga_key(item["source"], item["id"])
            if key not in seen:
                results.append(item)
                seen.add(key)
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        if not added:
            break

    if not results and errors and page == 1:
        raise HTTPException(502, {"message": "Search failed for every enabled source.", "errors": errors})

    payload = {"results": results, "errors": errors, "sourceStatus": source_status}
    searches[query_key] = {"at": time.time(), "payload": payload}
    save_cache()
    return {**payload, "page": page}


HOME_TTL_SECONDS = 30 * 60
HOME_SOURCE_LIMIT = 6
HOME_ROW_SIZE = 20


@app.get("/api/home")
def home_feed(refresh: bool = Query(False)) -> dict[str, Any]:
    cached = _cache.get("home") or {}
    if cached and not refresh and time.time() - cached.get("at", 0) < HOME_TTL_SECONDS:
        return {**cached["payload"], "cached": True}
    pool = fast_sources()[:HOME_SOURCE_LIMIT]
    if not pool:
        return {"hero": None, "rows": [], "errors": ["No sources enabled."]}
    labels = dict(pool)
    nyora_client()
    feed_client = Nyora(base_url=nyora_client().base_url, timeout=SEARCH_TIMEOUT_SECONDS, retries=0)
    popular: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    try:
        def fetch(sid: str) -> tuple[str, list[Any]]:
            found = feed_client.manga.popular(sid, page=1)
            return sid, list(val(found, "entries", default=[]))

        with ThreadPoolExecutor(max_workers=max(1, len(pool))) as feed_exec:
            pending = {feed_exec.submit(fetch, sid): sid for sid, _ in pool}
            for future in as_completed(pending):
                sid = pending[future]
                try:
                    _, entries = future.result()
                    popular[sid] = [normalize_manga(entry, sid, labels[sid]) for entry in entries[:12]]
                except Exception as exc:
                    errors.append(f"{labels[sid]}: {clean_error(exc)}")
    finally:
        feed_client.close()

    seen: set[str] = set()
    trending: list[dict[str, str]] = []
    lists = [popular.get(sid, []) for sid, _ in pool]
    for index in range(12):
        for entries in lists:
            if index < len(entries):
                item = entries[index]
                key = item["title"].casefold()
                if key not in seen:
                    seen.add(key)
                    trending.append(item)

    def kind(item: dict[str, str]) -> str:
        text = item["type"].casefold()
        if "manhwa" in text or "manhua" in text:
            return "manhwa"
        if "manga" in text:
            return "manga"
        return "manhwa" if re.search(r"asura|flame|webtoon", item["source"], re.IGNORECASE) else "manga"

    manhwa = [item for item in trending if kind(item) == "manhwa"]
    manga = [item for item in trending if kind(item) == "manga"]
    rows = []
    if trending:
        rows.append({"id": "trending", "title": "Trending Now", "items": trending[:HOME_ROW_SIZE]})
    if manhwa:
        rows.append({"id": "manhwa", "title": "Popular Manhwa", "items": manhwa[:HOME_ROW_SIZE]})
    if manga:
        rows.append({"id": "manga", "title": "Popular Manga", "items": manga[:HOME_ROW_SIZE]})
    hero = next((item for item in trending if item["cover"]), trending[0] if trending else None)
    payload = {"hero": hero, "rows": rows, "errors": errors}
    _cache["home"] = {"at": time.time(), "payload": payload}
    save_cache()
    return payload


@app.get("/api/manga")
def manga(source: str = Query(...), id: str = Query(...), refresh: bool = Query(False)) -> dict[str, Any]:
    key = manga_key(source, id)
    cached_detail = _cache["details"].get(key)
    if cached_detail and not refresh and time.time() - cached_detail.get("at", 0) < DETAILS_TTL_SECONDS:
        return {**cached_detail["payload"], "cached": True}

    cached = _cache["manga"].get(key)
    if not cached:
        cached = {"id": id, "source": source, "sourceName": source_name(source), "title": id, "url": id, "cover": ""}

    errors: list[dict[str, str]] = []
    groups: dict[str, dict[str, Any]] = {}
    title = cached.get("title", "Untitled")
    cover = cached.get("cover", "")
    description = ""

    candidates = alternative_manga(cached, title)
    nyora_client()
    fetched: dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
        pending = {pool.submit(details_for, candidate["source"], candidate): idx for idx, candidate in enumerate(candidates)}
        for future in as_completed(pending):
            idx = pending[future]
            try:
                fetched[idx] = future.result()
            except Exception as exc:
                errors.append({"source": candidates[idx]["source"], "error": clean_error(exc)})

    for idx, candidate in enumerate(candidates):
        info = fetched.get(idx)
        if info is None:
            continue
        manga_info = val(info, "manga", default={})
        if idx == 0:
            title = str(val(manga_info, "title", default=title))
            cover = media_url(str(val(manga_info, "large_cover_url", "largeCoverUrl", "cover_url", "coverUrl", default=cover)))
            description = clean_text(str(val(manga_info, "description", default="")))
        for chapter in val(info, "chapters", default=[]):
            item = normalize_chapter(chapter, candidate["source"], candidate.get("sourceName", ""))
            number_key = chapter_label(item["number"])
            groups.setdefault(
                number_key,
                {"number": item["number"], "label": number_key, "title": item["title"], "choices": []},
            )
            groups[number_key]["choices"].append(item)

    if not groups and errors:
        raise HTTPException(502, {"message": "Manga details failed for every source.", "errors": errors})

    chapters = sorted(groups.values(), key=lambda item: item["number"])
    for group in chapters:
        first = group["choices"][0]
        group["source"] = first["source"]
        group["sourceName"] = first["sourceName"]
        group["id"] = first["id"]
        for choice in group["choices"]:
            _cache["chapters"][manga_key(choice["source"], choice["id"])] = {
                **choice,
                "choices": group["choices"],
                "number": group["number"],
                "label": group["label"],
                "manga": key,
            }

    payload = {"title": title, "cover": cover, "description": description, "chapters": chapters, "errors": errors}
    _cache["details"][key] = {"at": time.time(), "payload": payload}
    save_cache()
    return payload


def put_pages(pages: list[dict[str, Any]]) -> str:
    sweep_pages()
    token = uuid.uuid4().hex
    _page_cache[token] = {"created": time.time(), "pages": pages}
    return token


def sweep_pages() -> None:
    now = time.time()
    for token, value in list(_page_cache.items()):
        if now - value["created"] > PAGE_TTL_SECONDS:
            _page_cache.pop(token, None)


def resolve_chapter_pages(source: str, chapter_id: str) -> dict[str, Any]:
    chapter_key = manga_key(source, chapter_id)
    cached = _cache["chapters"].get(chapter_key)
    if not cached:
        cached = {
            "source": source,
            "id": chapter_id,
            "url": chapter_id,
            "branch": None,
            "label": chapter_id,
            "title": f"Chapter {chapter_id}",
            "choices": [{"source": source, "id": chapter_id, "url": chapter_id, "branch": None}],
        }

    choices = cached.get("choices") or [cached]
    requested_index = next((idx for idx, item in enumerate(choices) if item.get("source") == source and item.get("id") == chapter_id), 0)
    ordered_choices = choices[requested_index:] + choices[:requested_index]
    errors: list[dict[str, str]] = []

    for choice in ordered_choices:
        try:
            raw_pages = engine_call(lambda: nyora_client().manga.pages(choice["source"], choice["url"], branch=choice.get("branch")))
            pages = [normalized for page in raw_pages if (normalized := normalize_page(page))["url"]]
            if not pages:
                raise RuntimeError("Source returned no readable pages.")
            return {
                "source": choice["source"],
                "sourceName": choice.get("sourceName", choice["source"]),
                "id": choice["id"],
                "label": cached.get("label", choice.get("label", "")),
                "title": cached.get("title", choice.get("title", "")),
                "pages": pages,
                "errors": errors,
            }
        except Exception as exc:
            errors.append({"source": choice.get("source", source), "error": clean_error(exc)})

    raise HTTPException(502, {"message": "Chapter failed for every available source.", "errors": errors})


@app.get("/api/chapter")
def chapter(source: str = Query(...), id: str = Query(...)) -> dict[str, Any]:
    resolved = resolve_chapter_pages(source, id)
    token = put_pages(resolved["pages"])
    rendered = []
    for idx, page in enumerate(resolved["pages"]):
        headers = page.get("headers") or {}
        rendered.append(
            {
                "url": f"/api/image?token={quote(token)}&i={idx}" if headers else page["url"],
                "headers": headers,
                "sourceUrl": page["url"],
            }
        )
    return {**resolved, "pages": rendered}


@app.get("/api/download/chapter")
def download_chapter(source: str = Query(...), id: str = Query(...)) -> Response:
    resolved = resolve_chapter_pages(source, id)
    chapter_label = str(resolved.get("label") or resolved.get("id") or id)
    chapter_title = clean_text(str(resolved.get("title") or f"Chapter {chapter_label}"))
    source_label = str(resolved.get("sourceName") or resolved.get("source") or source)
    zip_name = safe_filename(f"{chapter_title or 'chapter'}-{source_label}.zip", "chapter.zip")

    files: list[dict[str, str]] = []
    failures: list[str] = []
    buffer = BytesIO()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with http_client(timeout=60, follow_redirects=True) as client:
            for index, page in enumerate(resolved["pages"], start=1):
                url = page["url"]
                headers = page.get("headers") or {}
                try:
                    response = client.get(url, headers=headers)
                    response.raise_for_status()
                    ext = extension_for(response.headers.get("content-type", ""), url)
                    filename = f"pages/page-{index:03d}{ext}"
                    archive.writestr(filename, response.content)
                    files.append({"file": filename, "sourceUrl": url})
                except Exception as exc:
                    failures.append(f"page {index}: {clean_error(exc)}")

        if not files:
            raise HTTPException(502, {"message": "No pages could be downloaded.", "errors": failures})

        html_pages = "\n".join(
            f'    <img src="{escape(item["file"])}" alt="Page {idx}">'
            for idx, item in enumerate(files, start=1)
        )
        offline_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(chapter_title)}</title>
  <style>
    body {{ margin: 0; background: #050608; color: #f5f7fa; font-family: system-ui, sans-serif; }}
    header {{ position: sticky; top: 0; padding: 12px 16px; background: rgba(5, 6, 8, .92); border-bottom: 1px solid #202633; }}
    h1 {{ margin: 0; font-size: 16px; }}
    p {{ margin: 4px 0 0; color: #9aa6b8; font-size: 13px; }}
    main {{ display: grid; gap: 8px; justify-items: center; padding: 8px 0 32px; }}
    img {{ display: block; width: min(100%, 960px); height: auto; background: #111; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(chapter_title)}</h1>
    <p>{escape(source_label)} · {len(files)} pages</p>
  </header>
  <main>
{html_pages}
  </main>
</body>
</html>
"""
        archive.writestr("index.html", offline_html)
        archive.writestr(
            "metadata.json",
            json.dumps(
                {
                    "title": chapter_title,
                    "label": chapter_label,
                    "source": resolved.get("source"),
                    "sourceName": source_label,
                    "pageCount": len(files),
                    "failures": failures,
                    "files": files,
                },
                indent=2,
            ),
        )
        if failures:
            archive.writestr("failed-pages.txt", "\n".join(failures))

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


def _fetch_page(client: Any, page: dict[str, Any], index: int, target: Path, settings: dict[str, Any]) -> tuple[dict[str, Any] | None, int, str | None]:
    attempts = settings["retries"] + 1
    last_error = "unknown error"
    for attempt in range(attempts):
        try:
            response = client.get(page["url"], headers=page.get("headers") or {})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not response.content or content_type.lower().startswith("text/html"):
                raise RuntimeError(f"Unexpected page response: {content_type or 'unknown content type'}")
            filename = f"page-{index:04d}{extension_for(content_type, page['url'])}"
            (target / filename).write_bytes(response.content)
            return {"index": index, "file": filename, "url": f"/api/library/{target.name}/pages/{filename}"}, len(response.content), None
        except Exception as exc:
            last_error = clean_error(exc)
            if attempt < attempts - 1:
                time.sleep(settings["retryDelay"] * (2 ** attempt))
    return None, 0, f"Page {index}: {last_error}"


def run_download(job_id: str, request: DownloadRequest) -> None:
    target = OFFLINE_DIR / job_id
    stored_pages: list[dict[str, Any]] = []
    total_bytes = 0
    settings = dict(_download_settings)
    try:
        if job_id in _cancelled_jobs:
            raise RuntimeError("Download cancelled.")
        _acquire_download_slot()
        try:
            if job_id in _cancelled_jobs:
                raise RuntimeError("Download cancelled.")
            resolved = resolve_chapter_pages(request.source, request.id)
            pages = resolved["pages"]
            target.mkdir(parents=True, exist_ok=True)
            update_download(job_id, status="downloading", total=len(pages), source=resolved["source"], sourceName=resolved["sourceName"])
            errors: list[str] = []
            progress_lock = threading.Lock()
            done = 0

            def worker(item: tuple[int, dict[str, Any]]) -> None:
                nonlocal done, total_bytes
                index, page = item
                if job_id in _cancelled_jobs:
                    return
                record, nbytes, error = _fetch_page(client, page, index, target, settings)
                with progress_lock:
                    done += 1
                    if record is not None:
                        stored_pages.append(record)
                        total_bytes += nbytes
                    elif error:
                        errors.append(error)
                    update_download(job_id, progress=done, downloadedPages=len(stored_pages), bytes=total_bytes)

            with http_client(timeout=settings["timeout"], follow_redirects=True, limits=httpx.Limits(max_connections=settings["pageConcurrency"])) as client:
                with ThreadPoolExecutor(max_workers=settings["pageConcurrency"]) as page_pool:
                    list(page_pool.map(worker, enumerate(pages, start=1)))

            if job_id in _cancelled_jobs:
                raise RuntimeError("Download cancelled.")
            if not stored_pages:
                raise RuntimeError("No chapter pages could be downloaded. " + "; ".join(errors[:3]))
            stored_pages.sort(key=lambda record: record["index"])
            update_download(
                job_id,
                status="completed",
                progress=len(pages),
                downloadedPages=len(stored_pages),
                pages=stored_pages,
                pageCount=len(stored_pages),
                errors=errors,
                bytes=total_bytes,
                label=resolved["label"],
                title=request.title or resolved["title"],
                source=resolved["source"],
                sourceName=resolved["sourceName"],
                mangaTitle=request.mangaTitle or "",
                mangaId=request.mangaId,
                mangaSource=request.mangaSource,
            )
        finally:
            _release_download_slot()
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        cancelled = job_id in _cancelled_jobs
        _cancelled_jobs.discard(job_id)
        update_download(job_id, status="cancelled" if cancelled else "failed", error=None if cancelled else clean_error(exc), progress=0)


def _create_job(source: str, chapter_id: str, label: str, title: str, manga_title: str, manga_id: str, manga_source: str) -> tuple[str, dict[str, Any]]:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "chapterId": chapter_id,
        "source": source,
        "sourceName": source_name(source),
        "label": label or chapter_id,
        "title": title or f"Chapter {label or chapter_id}",
        "mangaTitle": manga_title,
        "mangaId": manga_id,
        "mangaSource": manga_source,
        "status": "queued",
        "progress": 0,
        "total": 0,
        "downloadedPages": 0,
        "bytes": 0,
        "createdAt": time.time(),
        "updatedAt": time.time(),
    }
    return job_id, job


def _submit_job(job_id: str) -> None:
    job = _downloads[job_id]
    request = DownloadRequest(
        source=job["source"], id=job["chapterId"], label=job["label"], title=job["title"],
        mangaTitle=job["mangaTitle"], mangaId=job["mangaId"], mangaSource=job["mangaSource"],
    )
    _download_pool.submit(run_download, job_id, request)


@app.post("/api/downloads")
def start_download(request: DownloadRequest) -> dict[str, Any]:
    key = manga_key(request.source, request.id)
    with _downloads_lock:
        existing = next(
            (job for job in _downloads.values() if manga_key(job.get("source", ""), job.get("chapterId", "")) == key and job.get("status") in {"queued", "downloading", "completed"}),
            None,
        )
        if existing:
            return dict(existing)
        job_id, job = _create_job(request.source, request.id, request.label, request.title, request.mangaTitle, request.mangaId, request.mangaSource)
        _downloads[job_id] = job
    save_downloads()
    _submit_job(job_id)
    return dict(_downloads[job_id])


@app.post("/api/downloads/batch")
def start_batch_download(request: BatchDownloadRequest) -> dict[str, Any]:
    queued: list[str] = []
    skipped = 0
    with _downloads_lock:
        for chapter in request.chapters:
            key = manga_key(chapter.source, chapter.id)
            duplicate = None
            for job in _downloads.values():
                if manga_key(job.get("source", ""), job.get("chapterId", "")) != key:
                    continue
                if job.get("status") in {"queued", "downloading"}:
                    duplicate = job
                    break
                if _download_settings["skipExisting"] and job.get("status") == "completed":
                    duplicate = job
                    break
            if duplicate is not None:
                skipped += 1
                continue
            job_id, job = _create_job(chapter.source, chapter.id, chapter.label, chapter.title, request.mangaTitle, request.mangaId, request.mangaSource)
            _downloads[job_id] = job
            queued.append(job_id)
    save_downloads()
    for job_id in queued:
        _submit_job(job_id)
    return {"queued": len(queued), "skipped": skipped, "ids": queued}


@app.get("/api/download-settings")
def get_download_settings() -> dict[str, Any]:
    return dict(_download_settings)


@app.put("/api/download-settings")
def put_download_settings(payload: dict[str, Any]) -> dict[str, Any]:
    with _gate_cond:
        for key, value in payload.items():
            if key in DEFAULT_DOWNLOAD_SETTINGS:
                _download_settings[key] = value
        _clamp_download_settings()
        _gate_cond.notify_all()
    save_download_settings()
    return dict(_download_settings)


@app.get("/api/downloads")
def downloads() -> dict[str, Any]:
    with _downloads_lock:
        items = sorted((dict(job) for job in _downloads.values()), key=lambda job: job.get("createdAt", 0), reverse=True)
    return {"downloads": items}


@app.post("/api/downloads/{job_id}/cancel")
def cancel_download(job_id: str) -> dict[str, Any]:
    with _downloads_lock:
        job = _downloads.get(job_id)
        if not job:
            raise HTTPException(404, "Download not found.")
        if job.get("status") not in {"queued", "downloading"}:
            raise HTTPException(409, "Only queued or active downloads can be cancelled.")
        _cancelled_jobs.add(job_id)
        if job.get("status") == "queued":
            job.update(status="cancelled", updatedAt=time.time())
    save_downloads()
    return {"cancelled": True}


@app.delete("/api/downloads/{job_id}")
def remove_download(job_id: str) -> dict[str, bool]:
    with _downloads_lock:
        job = _downloads.get(job_id)
        if not job:
            raise HTTPException(404, "Download not found.")
        if job.get("status") in {"queued", "downloading"}:
            raise HTTPException(409, "Wait for the download to finish before removing it.")
        _downloads.pop(job_id)
        _cancelled_jobs.discard(job_id)
    save_downloads()
    shutil.rmtree(OFFLINE_DIR / job_id, ignore_errors=True)
    return {"removed": True}


@app.get("/api/library")
def library() -> dict[str, Any]:
    with _downloads_lock:
        items = sorted(
            (dict(job) for job in _downloads.values() if job.get("status") == "completed"),
            key=lambda job: job.get("createdAt", 0),
            reverse=True,
        )
    return {"chapters": items}


@app.get("/api/library/{download_id}")
def offline_chapter(download_id: str) -> dict[str, Any]:
    with _downloads_lock:
        item = _downloads.get(download_id)
        if not item or item.get("status") != "completed":
            raise HTTPException(404, "Downloaded chapter is unavailable.")
        return dict(item)


@app.get("/api/library/{download_id}/pages/{filename}")
def offline_page(download_id: str, filename: str) -> FileResponse:
    if Path(filename).name != filename:
        raise HTTPException(400, "Invalid page filename.")
    with _downloads_lock:
        item = _downloads.get(download_id)
    if not item or item.get("status") != "completed":
        raise HTTPException(404, "Downloaded chapter is unavailable.")
    path = OFFLINE_DIR / download_id / filename
    if not path.is_file():
        raise HTTPException(404, "Downloaded page is unavailable.")
    return FileResponse(path)


EXPORT_READER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; --bg:#100e0c; --surface:#181512; --text:#f3ece1; --muted:#b6ab99;
    --accent:#ff5a36; --line:rgba(255,255,255,.1);
    background: var(--bg); color: var(--text);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  body[data-theme="light"] { --bg:#f6f2ea; --surface:#fffdf8; --text:#221d18; --muted:#6d6355; --line:rgba(0,0,0,.12); }
  body[data-theme="sepia"] { --bg:#efe4cd; --surface:#f7eeda; --text:#3a2f22; --muted:#7a6a52; --line:rgba(0,0,0,.14); }
  #bar { position: fixed; top: 0; left: 0; height: 3px; background: var(--accent); width: 0; z-index: 30; transition: width .15s; }
  header {
    position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 12px;
    padding: 10px 16px; background: var(--surface); border-bottom: 1px solid var(--line);
  }
  header h1 { font-size: 15px; font-weight: 600; margin: 0; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #indicator { font: 13px/1 ui-monospace, "SF Mono", Menlo, monospace; color: var(--muted); white-space: nowrap; }
  button {
    background: transparent; border: 1px solid var(--line); color: var(--text);
    border-radius: 8px; padding: 7px 13px; font: inherit; font-size: 13px; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .35; cursor: default; }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #pages { padding: 16px; display: flex; flex-direction: column; align-items: center; gap: 8px; }
  #pages img { display: block; border-radius: 4px; }
  body[data-mode="page"] #pages { min-height: calc(100vh - 58px); justify-content: center; }
  body[data-mode="page"] #pages img { display: none; }
  body[data-mode="page"] #pages img.current { display: block; }
  body[data-fit="width"] #pages img { max-width: 100%; height: auto; }
  body[data-fit="height"] #pages img { max-height: calc(100vh - 92px); width: auto; max-width: 100%; }
  body[data-fit="original"] #pages img { max-width: none; max-height: none; }
  #overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: none; align-items: center; justify-content: center; z-index: 40; }
  #overlay.open { display: flex; }
  #panel { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 24px; width: min(420px, 92vw); }
  #panel h2 { margin: 0 0 4px; font-size: 18px; }
  #panel .sub { margin: 0 0 18px; color: var(--muted); font-size: 13px; }
  .field { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
  .field label { font-size: 14px; }
  select {
    background: var(--bg); color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 10px; font: inherit; font-size: 13px; min-width: 150px;
  }
  #panel .hint { color: var(--muted); font-size: 12px; margin: 16px 0 18px; }
  #panel .close { width: 100%; background: var(--accent); border-color: var(--accent); color: #1c0b05; font-weight: 600; }
</style>
</head>
<body data-mode="scroll" data-fit="width" data-theme="dark">
<div id="bar"></div>
<header>
  <h1>__TITLE__</h1>
  <span id="indicator"></span>
  <button id="prev" type="button" aria-label="Previous page">&#8592;</button>
  <button id="next" type="button" aria-label="Next page">&#8594;</button>
  <button id="settingsBtn" type="button">Settings</button>
</header>
<main id="pages"></main>
<div id="overlay">
  <div id="panel" role="dialog" aria-modal="true" aria-label="Reader settings">
    <h2>Reader settings</h2>
    <p class="sub">Saved automatically for this chapter.</p>
    <div class="field"><label for="mode">Reading mode</label>
      <select id="mode"><option value="scroll">Long strip (scroll)</option><option value="page">Single page</option></select></div>
    <div class="field"><label for="fit">Page fit</label>
      <select id="fit"><option value="width">Fit width</option><option value="height">Fit height</option><option value="original">Original size</option></select></div>
    <div class="field"><label for="theme">Theme</label>
      <select id="theme"><option value="dark">Dark</option><option value="light">Light</option><option value="sepia">Sepia</option></select></div>
    <div class="field"><label for="dir">Page direction</label>
      <select id="dir"><option value="ltr">Left to right</option><option value="rtl">Right to left (manga)</option></select></div>
    <p class="hint">In single-page mode: &#8592;/&#8594; or Space turns pages, or click the left/right half of the page. Home/End jump to the first/last page.</p>
    <button class="close" id="closeSettings" type="button">Done</button>
  </div>
</div>
<script>
  const PAGES = __PAGES_JSON__;
  const KEY = "mangaflow-export:" + "__CHAPTER_ID__";
  const $ = (id) => document.getElementById(id);
  const main = $("pages"), overlay = $("overlay"), bar = $("bar"), indicator = $("indicator");
  const prev = $("prev"), next = $("next");
  const selects = { mode: $("mode"), fit: $("fit"), theme: $("theme"), dir: $("dir") };
  let settings = { mode: "scroll", fit: "width", theme: "dark", dir: "ltr" };
  let current = 0;
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
    if (saved.settings) settings = Object.assign(settings, saved.settings);
    if (Number.isInteger(saved.page)) current = saved.page;
  } catch (e) {}
  current = Math.max(0, Math.min(PAGES.length - 1, current));
  document.title = document.querySelector("h1").textContent;
  const imgs = PAGES.map((src, i) => {
    const img = new Image();
    img.src = src; img.loading = "lazy"; img.alt = "Page " + (i + 1);
    main.appendChild(img);
    return img;
  });
  function persist() { try { localStorage.setItem(KEY, JSON.stringify({ settings, page: current })); } catch (e) {} }
  function apply() {
    document.body.dataset.mode = settings.mode;
    document.body.dataset.fit = settings.fit;
    document.body.dataset.theme = settings.theme;
    imgs.forEach((img, i) => img.classList.toggle("current", i === current));
    indicator.textContent = (current + 1) + " / " + PAGES.length;
    prev.disabled = current <= 0;
    next.disabled = current >= PAGES.length - 1;
    bar.style.width = ((current + 1) / PAGES.length * 100) + "%";
    for (const k in selects) selects[k].value = settings[k];
  }
  function turn(n) {
    current = Math.max(0, Math.min(PAGES.length - 1, n));
    apply(); persist();
    if (settings.mode === "scroll") imgs[current].scrollIntoView({ block: "start" });
  }
  prev.addEventListener("click", () => turn(current - 1));
  next.addEventListener("click", () => turn(current + 1));
  main.addEventListener("click", (e) => {
    if (settings.mode !== "page") return;
    const r = main.getBoundingClientRect();
    const clickedLeft = (e.clientX - r.left) < r.width / 2;
    const forward = settings.dir === "rtl" ? clickedLeft : !clickedLeft;
    turn(current + (forward ? 1 : -1));
  });
  let ticking = false;
  addEventListener("scroll", () => {
    if (settings.mode !== "scroll" || ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const mid = innerHeight / 2;
      let best = 0, bd = Infinity;
      imgs.forEach((img, i) => {
        const r = img.getBoundingClientRect();
        const d = Math.abs((r.top + r.bottom) / 2 - mid);
        if (d < bd) { bd = d; best = i; }
      });
      if (best !== current) { current = best; apply(); persist(); }
    });
  }, { passive: true });
  addEventListener("keydown", (e) => {
    if (e.key === "Escape") { overlay.classList.remove("open"); return; }
    if (e.target instanceof Element && e.target.matches("select, input")) return;
    if (settings.mode !== "page") return;
    const fwdKey = settings.dir === "rtl" ? "ArrowLeft" : "ArrowRight";
    const backKey = settings.dir === "rtl" ? "ArrowRight" : "ArrowLeft";
    if (e.key === fwdKey || e.key === " ") { e.preventDefault(); turn(current + 1); }
    else if (e.key === backKey) { e.preventDefault(); turn(current - 1); }
    else if (e.key === "Home") { e.preventDefault(); turn(0); }
    else if (e.key === "End") { e.preventDefault(); turn(PAGES.length - 1); }
  });
  $("settingsBtn").addEventListener("click", () => overlay.classList.add("open"));
  $("closeSettings").addEventListener("click", () => overlay.classList.remove("open"));
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.classList.remove("open"); });
  for (const k in selects) selects[k].addEventListener("change", () => {
    settings[k] = selects[k].value;
    apply(); persist();
    if (k === "mode" && settings.mode === "scroll") requestAnimationFrame(() => imgs[current].scrollIntoView({ block: "start" }));
  });
  apply(); persist();
  if (settings.mode === "scroll" && current > 0) requestAnimationFrame(() => imgs[current].scrollIntoView({ block: "start" }));
</script>
</body>
</html>
"""


@app.get("/api/library/{download_id}/export")
def export_chapter(download_id: str) -> Response:
    with _downloads_lock:
        item = _downloads.get(download_id)
        job = dict(item) if item else None
    if not job or job.get("status") != "completed":
        raise HTTPException(404, "Downloaded chapter is unavailable.")
    folder = OFFLINE_DIR / download_id
    pages = sorted(job.get("pages", []), key=lambda p: p.get("index", 0))
    files = [(p["file"], folder / p["file"]) for p in pages if p.get("file") and (folder / p["file"]).is_file()]
    if not files:
        raise HTTPException(404, "No saved pages to export.")
    title = f'{job.get("mangaTitle") or "Manga"} · CH. {job.get("label") or "?"}'
    html = (
        EXPORT_READER_HTML.replace("__TITLE__", escape(title))
        .replace("__CHAPTER_ID__", download_id)
        .replace("__PAGES_JSON__", json.dumps([name for name, _ in files]).replace("</", "<\\/"))
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("index.html", html)
        for name, path in files:
            zf.write(path, name)
    safe = re.sub(r"[^\w.-]+", "_", f'{job.get("mangaTitle") or "chapter"}-CH{job.get("label") or ""}')[:80] or "chapter"
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )


@app.get("/api/image")
def image(token: str = Query(...), i: int = Query(..., ge=0)) -> Response:
    sweep_pages()
    entry = _page_cache.get(token)
    if not entry or i >= len(entry["pages"]):
        raise HTTPException(404, "Page proxy entry expired. Reopen the chapter.")
    entry["created"] = time.time()
    page = entry["pages"][i]
    url = page["url"]
    headers = page.get("headers") or {}
    upstream = None
    for attempt in range(2):
        try:
            upstream = _image_client.get(url, headers=headers)
            upstream.raise_for_status()
            break
        except Exception as exc:
            if attempt == 1:
                raise HTTPException(502, f"Image request failed: {clean_error(exc)}") from exc
            time.sleep(0.4)
    media_type = upstream.headers.get("content-type", "image/jpeg")
    return Response(
        content=upstream.content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
