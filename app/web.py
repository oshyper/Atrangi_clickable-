from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
import httpx
from app.config import BOT_TOKEN, BACKUP_CHANNEL_URL
from app.db import init_db, get_posts, get_post

app = FastAPI(title="Atrangii Mini App API")
init_db()

@app.get("/")
async def index():
    return FileResponse("web/index.html")

@app.get("/api/health")
async def health():
    return {"ok": True}

@app.get("/api/posts")
async def posts(limit: int = 50):
    limit = max(1, min(limit, 100))
    rows = get_posts(limit)
    for r in rows:
        r["media_url"] = f"/api/media/{r['id']}" if r["file_id"] else None
    return {"posts": rows, "backup_url": BACKUP_CHANNEL_URL}

@app.get("/api/media/{post_id}")
async def media(post_id: int):
    post = get_post(post_id)
    if not post or not post["file_id"]:
        raise HTTPException(404, "Media not found")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": post["file_id"]}
        )
        data = r.json()
        if not data.get("ok"):
            raise HTTPException(502, "Telegram getFile failed")
        path = data["result"]["file_path"]
        stream = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}")
        if stream.status_code != 200:
            raise HTTPException(502, "Telegram media download failed")
        content_type = stream.headers.get("content-type", "application/octet-stream")
        return StreamingResponse(iter([stream.content]), media_type=content_type)
