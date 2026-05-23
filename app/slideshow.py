"""Slideshow web UI for MMS photos. Runs on port 8082."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from app.database import engine, Base, SessionLocal
from app.models import BlockedPhoto

app = FastAPI(title="Photo Slideshow")

PHOTOS_DIR = "/app/photos"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)


def _get_blocked_filenames(db) -> set:
    return {b.filename for b in db.query(BlockedPhoto.filename).all()}


@app.get("/photos/{filename}")
def serve_photo(filename: str):
    """Serve an individual photo file."""
    safe_name = os.path.basename(filename)
    filepath = os.path.join(PHOTOS_DIR, safe_name)

    if not os.path.isfile(filepath):
        return Response(status_code=404)

    ext = Path(filepath).suffix.lower()
    content_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    with open(filepath, "rb") as f:
        return Response(content=f.read(), media_type=content_type)


@app.get("/api/photos")
def list_photos():
    """Return JSON list of photo filenames, excluding blocked ones."""
    if not os.path.isdir(PHOTOS_DIR):
        return []

    db = SessionLocal()
    try:
        blocked = _get_blocked_filenames(db)
    finally:
        db.close()

    return sorted(
        f for f in os.listdir(PHOTOS_DIR)
        if os.path.isfile(os.path.join(PHOTOS_DIR, f))
        and Path(f).suffix.lower() in IMAGE_EXTENSIONS
        and f not in blocked
    )


@app.get("/api/photos/all")
def list_all_photos():
    """Return all photos with their blocked status for the admin page."""
    if not os.path.isdir(PHOTOS_DIR):
        return []

    db = SessionLocal()
    try:
        blocked = _get_blocked_filenames(db)
    finally:
        db.close()

    photos = sorted(
        f for f in os.listdir(PHOTOS_DIR)
        if os.path.isfile(os.path.join(PHOTOS_DIR, f))
        and Path(f).suffix.lower() in IMAGE_EXTENSIONS
    )
    return [{"filename": f, "blocked": f in blocked} for f in photos]


@app.post("/api/block/{filename}")
def block_photo(filename: str):
    """Add a photo to the blocklist."""
    safe_name = os.path.basename(filename)
    db = SessionLocal()
    try:
        existing = db.query(BlockedPhoto).filter(BlockedPhoto.filename == safe_name).first()
        if not existing:
            db.add(BlockedPhoto(filename=safe_name))
            db.commit()
        return {"status": "blocked", "filename": safe_name}
    finally:
        db.close()


@app.post("/api/unblock/{filename}")
def unblock_photo(filename: str):
    """Remove a photo from the blocklist."""
    safe_name = os.path.basename(filename)
    db = SessionLocal()
    try:
        db.query(BlockedPhoto).filter(BlockedPhoto.filename == safe_name).delete()
        db.commit()
        return {"status": "unblocked", "filename": safe_name}
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def slideshow_page():
    """Render the slideshow HTML page."""
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Slideshow</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a1a; color: #e0e0e0; font-family: monospace;
         display: flex; align-items: center; justify-content: center;
         height: 100vh; overflow: hidden; }
  #slideshow { width: 100%; height: 100%; position: relative; }
  #slideshow img {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    max-width: 95%; max-height: 95vh;
    object-fit: contain;
    opacity: 0; transition: opacity 1s ease-in-out;
  }
  #slideshow img.active { opacity: 1; }
  #placeholder {
    text-align: center; color: #555; font-size: 1.2em;
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
  }
  #counter {
    position: fixed; bottom: 10px; right: 15px;
    color: #555; font-size: 12px; z-index: 10;
  }
</style>
</head>
<body>
<div id="slideshow">
  <div id="placeholder">No photos yet. Send an image via MMS to get started.</div>
</div>
<div id="counter"></div>
<script>
  let photos = [];
  let currentIndex = 0;
  const container = document.getElementById('slideshow');
  const placeholder = document.getElementById('placeholder');
  const counter = document.getElementById('counter');

  function shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  async function loadPhotos() {
    const resp = await fetch('/api/photos');
    const fetched = await resp.json();
    if (fetched.length === 0) {
      placeholder.style.display = 'block';
      counter.textContent = '';
      return;
    }
    placeholder.style.display = 'none';
    photos = shuffle(fetched);
    currentIndex = 0;
    showPhoto(currentIndex);
  }

  function showPhoto(index) {
    currentIndex = index % photos.length;
    container.querySelectorAll('img').forEach(img => {
      img.classList.remove('active');
      setTimeout(() => img.remove(), 1000);
    });
    const img = document.createElement('img');
    img.src = '/photos/' + encodeURIComponent(photos[currentIndex]);
    img.onload = () => img.classList.add('active');
    container.appendChild(img);
    counter.textContent = (currentIndex + 1) + ' / ' + photos.length;
  }

  function nextPhoto() {
    if (photos.length === 0) return;
    showPhoto(currentIndex + 1);
  }

  loadPhotos();
  setInterval(nextPhoto, 10000);
  setInterval(loadPhotos, 60000);
</script>
</body>
</html>""")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Admin page to manage slideshow photos."""
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slideshow Admin</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a1a; color: #e0e0e0; font-family: monospace; padding: 20px; }
  h1 { margin-bottom: 20px; font-size: 1.4em; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }
  .card {
    background: #2a2a2a; border-radius: 8px; overflow: hidden;
    display: flex; flex-direction: column;
  }
  .card.blocked { opacity: 0.4; }
  .card img {
    width: 100%; height: 200px; object-fit: cover;
    display: block; cursor: pointer;
  }
  .card .info {
    padding: 8px 10px; display: flex; align-items: center;
    justify-content: space-between; gap: 8px;
  }
  .card .filename {
    font-size: 0.7em; color: #888; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; flex: 1;
  }
  .card button {
    padding: 4px 12px; border: none; border-radius: 4px;
    font-family: monospace; font-size: 0.8em; cursor: pointer;
    white-space: nowrap;
  }
  .btn-block { background: #c0392b; color: #fff; }
  .btn-block:hover { background: #e74c3c; }
  .btn-unblock { background: #27ae60; color: #fff; }
  .btn-unblock:hover { background: #2ecc71; }
  .stats { color: #666; margin-bottom: 16px; font-size: 0.9em; }
  #lightbox {
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.9); z-index: 100;
    align-items: center; justify-content: center; cursor: pointer;
  }
  #lightbox.open { display: flex; }
  #lightbox img { max-width: 95%; max-height: 95vh; object-fit: contain; }
</style>
</head>
<body>
<h1>Slideshow Admin</h1>
<div class="stats" id="stats"></div>
<div class="grid" id="grid"></div>
<div id="lightbox" onclick="this.classList.remove('open')">
  <img id="lb-img">
</div>
<script>
  const grid = document.getElementById('grid');
  const stats = document.getElementById('stats');
  const lightbox = document.getElementById('lightbox');
  const lbImg = document.getElementById('lb-img');

  async function load() {
    const resp = await fetch('/api/photos/all');
    const photos = await resp.json();
    const active = photos.filter(p => !p.blocked).length;
    const blocked = photos.filter(p => p.blocked).length;
    stats.textContent = active + ' active, ' + blocked + ' blocked, ' + photos.length + ' total';
    grid.innerHTML = '';
    photos.forEach(p => {
      const card = document.createElement('div');
      card.className = 'card' + (p.blocked ? ' blocked' : '');
      const img = document.createElement('img');
      img.src = '/photos/' + encodeURIComponent(p.filename);
      img.onclick = () => { lbImg.src = img.src; lightbox.classList.add('open'); };
      const info = document.createElement('div');
      info.className = 'info';
      const fname = document.createElement('span');
      fname.className = 'filename';
      fname.textContent = p.filename;
      const btn = document.createElement('button');
      if (p.blocked) {
        btn.textContent = 'Restore';
        btn.className = 'btn-unblock';
        btn.onclick = () => toggle(p.filename, 'unblock');
      } else {
        btn.textContent = 'Remove';
        btn.className = 'btn-block';
        btn.onclick = () => toggle(p.filename, 'block');
      }
      info.appendChild(fname);
      info.appendChild(btn);
      card.appendChild(img);
      card.appendChild(info);
      grid.appendChild(card);
    });
  }

  async function toggle(filename, action) {
    await fetch('/api/' + action + '/' + encodeURIComponent(filename), { method: 'POST' });
    load();
  }

  load();
</script>
</body>
</html>""")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
