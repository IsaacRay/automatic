# MMS Photo Slideshow Feature

## Context
Add the ability to receive images via MMS through Twilio, save them to a shared volume, and serve a slideshow website on port 8082 that cycles through all saved photos.

## Files to Change

| File | Action |
|------|--------|
| `app/main.py` | Modify — add MMS media download to `/sms` webhook |
| `app/slideshow.py` | **Create** — new FastAPI app for slideshow on port 8082 |
| `Dockerfile` | Modify — add `slideshow` APP_MODE branch |
| `docker-compose.yaml` | Modify — add photos volume to `api`, add `slideshow` service |

## Step 1: `app/main.py` — Handle MMS images

- Change the route signature from `Form(...)` params to `Request`-based form parsing, so we can access `NumMedia`, `MediaUrl0..N`, `MediaContentType0..N` dynamically
- Add `_save_mms_images(form_data)` helper that:
  - Checks `NumMedia > 0`
  - For each `MediaUrlN` where `MediaContentTypeN` starts with `image/`:
    - Downloads via `requests.get()` with Twilio HTTP Basic auth (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`)
    - Saves to `/app/photos/YYYYMMDD_HHMMSS_<8-hex-uuid>.ext`
  - Returns count of saved images
- After inbound SMS logging, call `_save_mms_images()`. If photos saved, reply "Photo saved!" (or "N photos saved!")
- If Body is empty (image-only MMS), return early — skip intent parsing
- If Body has text, continue to normal intent parsing as usual
- New imports: `os`, `uuid`, `requests` (already in requirements.txt), `Request` from fastapi
- Import `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` from `app.config`

## Step 2: `app/slideshow.py` — New slideshow web app

FastAPI app following the same pattern as `app/ui.py` (dark theme, server-rendered HTML):

- `GET /` — HTML page with CSS/JS slideshow
  - On load, fetches `/api/photos` for the current image list
  - JavaScript cycles through images every 10 seconds with CSS fade transitions
  - Shows "No photos yet" placeholder when empty
  - Re-fetches photo list every 60 seconds to pick up new images without page reload
  - Counter in bottom-right: "3 / 12"
- `GET /api/photos` — JSON array of filenames (sorted chronologically by filename)
- `GET /photos/{filename}` — serves individual image files with path traversal protection (`os.path.basename`)
- `__main__` block runs uvicorn on port 8082

## Step 3: `Dockerfile` — Add slideshow mode

Add `elif [ "$APP_MODE" = "slideshow" ]` branch that runs `python -m app.slideshow`.

## Step 4: `docker-compose.yaml` — Wire it up

- Add `volumes: ["./photos:/app/photos"]` to the `api` service (read-write for saving)
- Add new `slideshow` service:
  - Same image build
  - `APP_MODE: slideshow`
  - Port `8082:8082`
  - Volume `./photos:/app/photos:ro` (read-only)
  - `restart: unless-stopped`
  - No database/API credentials needed

## Verification
1. `mkdir -p photos` on host
2. `docker compose build && docker compose up -d`
3. Send an MMS with an image to the Twilio number — should get "Photo saved!" reply
4. Check `./photos/` on host — image file should be there
5. Open `http://localhost:8082` — should see the image in a slideshow
6. Send more images — slideshow picks them up within 60 seconds (or on page refresh)
7. Visit with no images — should see placeholder text
