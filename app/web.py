import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.config import BACKUP_CHANNEL_URL, BOT_TOKEN, POSTS_PAGE_SIZE, REQUIRE_TG_AUTH
from app.db import get_post, get_posts_page, init_db, is_banned, latest_post_id, touch_user
from app.security import make_media_token, validate_init_data, verify_media_token

TG_API = "https://api.telegram.org"
TG_FILE_LIMIT = 20 * 1024 * 1024  # Bot API getFile cannot serve files bigger than 20 MB
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
NO_STORE = {"Cache-Control": "no-store"}

# file_id -> (file_path, expires_at). Saves one Telegram API call per media view.
_path_cache: dict[str, tuple[str, float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=120.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Atrangii Mini App API", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ auth

def authorize(request: Request):
    """Returns a media token when auth is enabled, None when it is disabled."""
    if not REQUIRE_TG_AUTH:
        return None
    data = validate_init_data(request.headers.get("x-init-data", ""), BOT_TOKEN)
    if not data:
        raise HTTPException(401, "Please open this app from Telegram.")
    uid = (data.get("user") or {}).get("id")
    if uid and is_banned(uid):
        raise HTTPException(403, "Access denied.")
    request.state.uid = uid
    return make_media_token(BOT_TOKEN)


def check_media_access(token):
    if REQUIRE_TG_AUTH and not (token and verify_media_token(BOT_TOKEN, token)):
        raise HTTPException(401, "Unauthorized")


# ------------------------------------------------------------- serialising

def _tg_link(row):
    username = (row.get("channel_username") or "").strip()
    if username:
        return f"https://t.me/{username}/{row['message_id']}"
    cid = str(row["channel_id"])
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{row['message_id']}"
    return None


def _item(row, token):
    size = row.get("file_size") or 0
    too_large = bool(row["file_id"]) and size > TG_FILE_LIMIT
    suffix = f"?t={token}" if token else ""
    return {
        "id": row["id"],
        "type": row["media_type"],
        "mime": row.get("mime_type") or "",
        "name": row.get("file_name") or "",
        "size": size,
        "url": f"/api/media/{row['id']}{suffix}" if row["file_id"] and not too_large else None,
        "thumb": f"/api/thumb/{row['id']}{suffix}" if row.get("thumb_id") and not too_large else None,
        "too_large": too_large,
    }


def _group_rows(rows):
    """Merge album messages (same media_group_id) into one post; keep newest-first order."""
    groups, index = [], {}
    for r in rows:
        gid = r.get("media_group_id")
        key = (r["channel_id"], gid) if gid else None
        if key and key in index:
            index[key].append(r)
            continue
        g = [r]
        groups.append(g)
        if key:
            index[key] = g
    for g in groups:
        g.sort(key=lambda r: r["message_id"])
    return groups


def _serialize(group, token):
    first = group[0]
    return {
        "id": max(r["id"] for r in group),
        "html": next((r["html_caption"] or r["html_text"] for r in group
                      if r["html_caption"] or r["html_text"]), ""),
        "text": next((r["caption"] or r["text"] for r in group if r["caption"] or r["text"]), ""),
        "date": first.get("posted_at") or first["created_at"],
        "edited": any(r.get("edited_at") for r in group),
        "tg_link": _tg_link(first),
        "items": [_item(r, token) for r in group if r["file_id"]],
    }


# --------------------------------------------------------------- endpoints

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health():
    return JSONResponse({"ok": True}, headers=NO_STORE)


@app.get("/api/latest")
async def latest(request: Request):
    authorize(request)
    return JSONResponse({"latest_id": latest_post_id()}, headers=NO_STORE)


@app.get("/api/posts")
async def posts(request: Request,
                limit: int = Query(POSTS_PAGE_SIZE, ge=1, le=50),
                before: int | None = Query(None, ge=1)):
    token = authorize(request)
    uid = getattr(request.state, "uid", None)
    if uid and before is None:
        touch_user(uid)  # opening the Mini App counts as activity (/stats "active")
    rows, has_more = get_posts_page(limit, before)
    groups = _group_rows(rows)
    cursor = min((r["id"] for r in rows), default=None)
    return JSONResponse({
        "posts": [_serialize(g, token) for g in groups],
        "next_cursor": cursor if has_more else None,
        "latest_id": latest_post_id(),
        "backup_url": BACKUP_CHANNEL_URL,
    }, headers=NO_STORE)


async def _file_path(client: httpx.AsyncClient, file_id: str) -> str:
    hit = _path_cache.get(file_id)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        r = await client.get(f"{TG_API}/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
        data = r.json()
    except (httpx.HTTPError, ValueError):
        # `from None` so the bot token inside the URL never ends up in logs.
        raise HTTPException(502, "Telegram is unreachable") from None
    if not data.get("ok"):
        if "too big" in str(data.get("description", "")).lower():
            raise HTTPException(413, "File is too large to preview")
        raise HTTPException(502, "Telegram getFile failed")
    path = data["result"]["file_path"]
    if len(_path_cache) > 2000:
        _path_cache.clear()
    _path_cache[file_id] = (path, time.time() + 45 * 60)
    return path


async def _proxy(request: Request, file_id: str, default_type: str):
    """Stream a Telegram file to the browser (Range supported, so video seeking works)."""
    client: httpx.AsyncClient = request.app.state.http
    path = await _file_path(client, file_id)

    headers = {"Accept-Encoding": "identity"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        upstream = await client.send(
            client.build_request("GET", f"{TG_API}/file/bot{BOT_TOKEN}/{path}", headers=headers),
            stream=True,
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Telegram download failed") from None

    if upstream.status_code not in (200, 206):
        await upstream.aclose()
        _path_cache.pop(file_id, None)
        raise HTTPException(502, "Telegram download failed")

    out = {"Cache-Control": "private, max-age=86400", "Accept-Ranges": "bytes"}
    for h in ("content-length", "content-range"):
        if h in upstream.headers:
            out[h] = upstream.headers[h]
    content_type = mimetypes.guess_type(path)[0] or default_type
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=content_type,
        headers=out,
        background=BackgroundTask(upstream.aclose),
    )


_DEFAULT_TYPES = {
    "photo": "image/jpeg", "video": "video/mp4", "animation": "video/mp4",
    "video_note": "video/mp4", "audio": "audio/mpeg", "voice": "audio/ogg",
}


@app.get("/api/media/{post_id}")
async def media(post_id: int, request: Request, t: str | None = None):
    check_media_access(t)
    post = get_post(post_id)
    if not post or not post["file_id"]:
        raise HTTPException(404, "Media not found")
    if (post.get("file_size") or 0) > TG_FILE_LIMIT:
        raise HTTPException(413, "File is too large to preview")
    default = post.get("mime_type") or _DEFAULT_TYPES.get(post["media_type"], "application/octet-stream")
    return await _proxy(request, post["file_id"], default)


@app.get("/api/thumb/{post_id}")
async def thumb(post_id: int, request: Request, t: str | None = None):
    check_media_access(t)
    post = get_post(post_id)
    if not post or not post.get("thumb_id"):
        raise HTTPException(404, "Thumbnail not found")
    return await _proxy(request, post["thumb_id"], "image/jpeg")
