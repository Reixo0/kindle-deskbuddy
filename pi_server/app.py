"""FastAPI server for the Raspberry Pi brain of Kindle Desk Buddy."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .config import Settings, load_settings
from .renderer import DeskBuddyRenderer
from .spotify_client import create_spotify_service
from .state import DeskBuddyState, ViewMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("deskbuddy.app")


class TapPayload(BaseModel):
    x: int
    y: int
    rotate: Optional[int] = None
    battery: Optional[int] = None
    charging: Optional[bool] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    cfg = settings or load_settings()
    spotify = create_spotify_service(cfg)
    state = DeskBuddyState(cfg, spotify)
    renderer = DeskBuddyRenderer(cfg)

    app = FastAPI(title="Kindle Desk Buddy", version="1.0.0")
    app.state.settings = cfg
    app.state.deskbuddy = state
    app.state.renderer = renderer

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/preview")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "demo_mode": spotify.is_demo}

    @app.get("/api/state")
    def get_state() -> dict[str, object]:
        state.refresh_playback()
        return state.snapshot_dict()

    @app.get("/api/screen.png")
    def get_screen(
        w: Optional[int] = Query(default=None, ge=320, le=2560),
        h: Optional[int] = Query(default=None, ge=240, le=2560),
        rotate: Optional[int] = Query(default=None),
        battery: Optional[int] = Query(default=None, ge=0, le=100),
        charging: Optional[int] = Query(default=None),
        brightness: Optional[int] = Query(default=None, ge=0, le=24),
        preview: bool = Query(default=False),
    ) -> Response:
        if battery is not None or charging is not None or brightness is not None:
            state.update_battery(
                battery=battery,
                charging=(bool(charging) if charging is not None else None),
                brightness=brightness,
            )
        state.refresh_playback()

        req_w = w or cfg.screen_width
        req_h = h or cfg.screen_height
        rot = 0 if preview else (cfg.rotation if rotate is None else rotate)

        png_bytes, digest, full_refresh = renderer.render(
            state=state,
            width=req_w,
            height=req_h,
            rotation_cw=rot,
        )
        if state.view == ViewMode.IDLE_CLOCK:
            poll_sec = 30
        elif state.view == ViewMode.NOW_PLAYING and state.playback.is_playing:
            poll_sec = 2
        else:
            poll_sec = 10

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "X-DeskBuddy-Hash": digest,
                "X-DeskBuddy-Refresh": "full" if full_refresh else "partial",
                "X-DeskBuddy-Poll": str(poll_sec),
                "X-DeskBuddy-View": state.view.value,
            },
        )

    @app.post("/api/tap")
    def post_tap(payload: TapPayload) -> JSONResponse:
        if payload.battery is not None or payload.charging is not None:
            state.update_battery(payload.battery, payload.charging)
        rot = cfg.rotation if payload.rotate is None else payload.rotate
        result = state.handle_tap(payload.x, payload.y, rotation_cw=rot)
        return JSONResponse(result)

    @app.get("/api/tap")
    def get_tap(
        x: int = Query(...),
        y: int = Query(...),
        rotate: Optional[int] = Query(default=None),
        battery: Optional[int] = Query(default=None, ge=0, le=100),
        charging: Optional[int] = Query(default=None),
    ) -> JSONResponse:
        """GET convenience endpoint so ultra-minimal curl/wget Kindle scripts can send taps."""
        if battery is not None or charging is not None:
            state.update_battery(
                battery=battery,
                charging=(bool(charging) if charging is not None else None),
            )
        rot = cfg.rotation if rotate is None else rotate
        result = state.handle_tap(x, y, rotation_cw=rot)
        return JSONResponse(result)

    @app.post("/api/preview/idle")
    def trigger_idle() -> dict[str, object]:
        state.force_idle_clock()
        return {"ok": True, "view": state.view.value}

    @app.get("/auth/login")
    def auth_login() -> RedirectResponse:
        if spotify.is_demo:
            return RedirectResponse(url="/preview")
        return RedirectResponse(url=spotify.get_authorize_url())

    @app.get("/auth/callback", response_class=HTMLResponse)
    def auth_callback(code: Optional[str] = None, error: Optional[str] = None) -> HTMLResponse:
        if error:
            return HTMLResponse(
                f"<h2>Spotify authorization cancelled: {error}</h2><p><a href='/preview'>Back to preview</a></p>",
                status_code=400,
            )
        if not code:
            return HTMLResponse(
                "<h2>Missing OAuth code.</h2><p><a href='/auth/login'>Try again</a></p>",
                status_code=400,
            )
        res = spotify.exchange_code(code)
        if res.ok:
            return HTMLResponse(
                "<h2>Spotify connected!</h2><p>Your Kindle Desk Buddy is ready. "
                "<a href='/preview'>Open Live Preview</a></p>"
            )
        return HTMLResponse(
            f"<h2>Spotify connection failed</h2><p>{res.message}</p><p><a href='/auth/login'>Retry</a></p>",
            status_code=500,
        )

    @app.get("/preview", response_class=HTMLResponse)
    def preview_page() -> HTMLResponse:
        mode_badge = "DEMO MODE (Mock Spotify)" if spotify.is_demo else (
            "Spotify Connected" if spotify.is_authenticated() else "Spotify Not Linked"
        )
        auth_link = "" if (spotify.is_demo or spotify.is_authenticated()) else (
            '<a class="btn" href="/auth/login">Connect Spotify</a>'
        )
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Kindle Desk Buddy — Interactive Preview</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      background: #18181b;
      color: #f4f4f5;
      margin: 0;
      padding: 24px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 16px;
    }}
    .bar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      font-size: 14px;
    }}
    .badge {{
      padding: 4px 10px;
      border-radius: 999px;
      background: #27272a;
      border: 1px solid #3f3f46;
    }}
    .btn {{
      background: #fafafa;
      color: #09090b;
      border: none;
      border-radius: 8px;
      padding: 6px 12px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
    }}
    .btn:hover {{ background: #e4e4e7; }}
    .bezel {{
      background: #111;
      padding: 22px 34px;
      border-radius: 22px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.6);
      border: 2px solid #2e2e32;
    }}
    #screen {{
      display: block;
      width: {cfg.screen_width}px;
      height: {cfg.screen_height}px;
      max-width: 92vw;
      height: auto;
      cursor: pointer;
      background: #fff;
      border-radius: 4px;
      image-rendering: pixelated;
    }}
    #log {{
      font-family: ui-monospace, monospace;
      font-size: 13px;
      color: #a1a1aa;
      min-height: 20px;
    }}
  </style>
</head>
<body>
  <div class="bar">
    <strong>Kindle Desk Buddy</strong>
    <span class="badge">{mode_badge}</span>
    {auth_link}
    <button class="btn" onclick="triggerIdle()">Test Idle Clock</button>
    <button class="btn" onclick="refreshScreen()">Refresh</button>
    <label>Simulated Battery:
      <input id="batt" type="number" min="0" max="100" value="84" style="width:54px" onchange="refreshScreen()" />%
    </label>
    <label><input id="chg" type="checkbox" onchange="refreshScreen()" /> Charging</label>
  </div>
  <div class="bezel">
    <img id="screen" alt="Kindle Desk Buddy Screen" />
  </div>
  <div id="log">Click anywhere on the Kindle screen to test taps.</div>

  <script>
    const img = document.getElementById('screen');
    const log = document.getElementById('log');
    const logicalW = {cfg.screen_width};
    const logicalH = {cfg.screen_height};

    function qs() {{
      const b = document.getElementById('batt').value || 84;
      const c = document.getElementById('chg').checked ? 1 : 0;
      return `preview=1&w=${{logicalW}}&h=${{logicalH}}&battery=${{b}}&charging=${{c}}&_=${{Date.now()}}`;
    }}

    function refreshScreen() {{
      img.src = '/api/screen.png?' + qs();
    }}

    async function triggerIdle() {{
      await fetch('/api/preview/idle', {{ method: 'POST' }});
      log.textContent = 'Switched to Idle Clock view';
      refreshScreen();
    }}

    img.addEventListener('click', async (ev) => {{
      const rect = img.getBoundingClientRect();
      const x = Math.round((ev.clientX - rect.left) * (logicalW / rect.width));
      const y = Math.round((ev.clientY - rect.top) * (logicalH / rect.height));
      const b = parseInt(document.getElementById('batt').value || '84', 10);
      const c = document.getElementById('chg').checked;
      const res = await fetch('/api/tap', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ x, y, rotate: 0, battery: b, charging: c }})
      }});
      const data = await res.json();
      log.textContent = `Tap (${{x}}, ${{y}}) -> action=${{data.action}} view=${{data.view}} ${{data.message || ''}}`;
      refreshScreen();
    }});

    refreshScreen();
    setInterval(refreshScreen, 10000);
  </script>
</body>
</html>"""
        return HTMLResponse(html)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "pi_server.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
