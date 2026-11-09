# MangaFlow

A local manga/manhwa reader that runs entirely on your machine. FastAPI backend + a single-page browser UI, powered by the Nyora source engine. No accounts, no cloud — your library lives on disk.

## Easy start

Double-click `MangaFlow.command`, or run:

```bash
./run.sh
```

The launcher creates `work/.venv`, installs Python packages, uses Homebrew OpenJDK when available, opens the browser, and starts MangaFlow at `http://127.0.0.1:8000/`.

Requirements: Python 3 and Java (Nyora's helper needs a JVM). If Java is missing on macOS:

```bash
brew install openjdk
./run.sh
```

Useful options:

```bash
PORT=8010 ./run.sh      # run on a different port
OPEN_URL=0 ./run.sh     # don't auto-open the browser
```

## Home screen

Netflix-style feed: a featured hero banner (top trending title, with Start Reading and + My List), then horizontal rows — Continue Reading, Trending Now, Popular Manhwa, Popular Manga — built from your enabled sources' popular lists and cached for 30 minutes. Rows scroll with hover arrows or touch swipe; clicking a card opens the title.

## Search & sources

- Search all enabled sources at once, or filter by source and by type (manga / manhwa / manhua).
- Load More pagination and a cache-bypassing Refresh Results.
- Library → Sources manages which sources are enabled and shows per-source health.
- Chapter lists offer alternative sources per chapter when available.

## Reader

- Modes: Long Strip, Single Page, Double Page.
- Fit: width, height, or original size; adjustable page width and strip gap under the gear button.
- Right-to-left paging for manga; keyboard: `←/→` pages (paged) or chapters (strip), `Shift+←/→` chapters, `Space` next page, `N/P` next/prev chapter, `Esc` back, `/` focus search, `?` shortcuts help.
- Chapters auto-mark read; details page shows Resume and read markers.

## Downloads

- Per-chapter Download, plus Download Unread, Download All, and a **Batch download** dialog: choose a start point (next unread / first / latest), pick how many chapters (presets 5/10/25/50/100/All), preview the selection, then queue.
- Library → Downloads → **Download settings**: chapters at once (1–6), pages per chapter (1–8), retries per page (0–5), retry delay, per-page timeout, skip already-downloaded chapters, Pause/Resume queue, Retry failed. Settings persist and apply to running jobs.
- Pages download in parallel with per-page retries and exponential backoff; live percent progress per job; interrupted jobs recover on restart.
- Read saved chapters offline, with previous/next chapter across everything saved for a title.

## Export

- **Export** on any completed download saves a ZIP (e.g. `Vagabond-CH2.zip`) containing the page images plus a standalone `index.html` reader. Unzip anywhere and open it — no server or internet needed.
- The exported reader has its own settings: scroll or single-page mode, page fit (width/height/original), dark/light/sepia theme, left-to-right or right-to-left paging, keyboard and click-half navigation, and it remembers your settings and last page per chapter.
- Library → Export/Import also backs up favorites, history, and reader settings as JSON.

## Library

Favorites (with Check for Updates for new chapters), reading history, downloads, and the source manager. Favorites, history, and reader settings live in browser localStorage; server-side data lives under `work/`.

## Project layout

```
server.py                  FastAPI backend: sources, search, chapter/page proxy, downloads, export
static/index.html          the entire frontend (HTML + CSS + JS, no build step)
run.sh / MangaFlow.command launchers (venv + deps + server + browser)
requirements.txt           fastapi, uvicorn, nyora
android/                   Android wrapper app (WebView shell) + Gradle project
Build MangaFlow Android.command   builds the debug APK (needs JDK 17 + Android SDK 36)
work/                      runtime data: .venv, cache, downloads.json,
                           download-settings.json, offline/<job>/ page images
```

## Notes

- `static/index.html` edits apply on browser reload; `server.py` edits need a server restart.
- The server binds to 127.0.0.1 only — nothing is exposed to your network.
- Cache, download history, and offline images are under `work/`; delete that folder's cache files to reset them (keep `.venv` and `offline/` if you want your saved chapters).