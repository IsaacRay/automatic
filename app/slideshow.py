"""Slideshow web UI for MMS photos. Runs on port 8082."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="Photo Slideshow")

PHOTOS_DIR = "/app/photos"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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
    """Return JSON list of photo filenames, sorted chronologically."""
    if not os.path.isdir(PHOTOS_DIR):
        return []

    return sorted(
        f for f in os.listdir(PHOTOS_DIR)
        if os.path.isfile(os.path.join(PHOTOS_DIR, f))
        and Path(f).suffix.lower() in IMAGE_EXTENSIONS
    )


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
