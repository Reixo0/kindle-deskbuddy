# Kindle Desk Buddy

A lightweight, landscape e-ink **Spotify controller + desk clock** for a jailbroken **Kindle 10th Generation**, powered by a **Raspberry Pi** over Wi-Fi. No KUAL required.

## Features
- **Album Art Centerpiece (Now Playing):** Large Floyd-Steinberg dithered album art in landscape, with full playback controls (**Play/Pause**, **Previous**, **Next**, **Volume −/+**, **Shuffle**, and **Like/Unlike**).
- **Idle Clock Mode (5-minute auto-switch):** When no music has played for 5 minutes, the screen switches to a giant desk clock with date, Kindle battery, and a **quick Song / Album / Playlist selector** right below the clock so you can start music with one tap.
- **Full Library Browser:** Browse your **Saved Albums** (tap to play the album or open its tracklist to pick a specific song), **Liked Songs**, **Playlists**, and **Recently Played**.
- **Queue & Device Selector:** Inspect your **Up Next** queue and switch playback between any active **Spotify Connect** device (speakers, PC, phone, Raspotify, etc.).
- **Kindle Battery & Smart E-Ink Refresh:** Displays live Kindle battery % and charging status, skips redraws when nothing changed, and triggers full flashing refreshes automatically to prevent ghosting.
- **Interactive Browser Preview (`/preview`):** Test the entire UI and click every button from your desktop or phone browser before deploying to the Kindle.

---

## 1. Raspberry Pi Setup

```bash
git clone <your-repo-or-copy-folder> kindle-deskbuddy
cd kindle-deskbuddy

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

### Quick Test (Demo Mode — no Spotify keys needed yet)
```bash
DEMO_MODE=1 uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
```
Open `http://<pi-ip>:8765/preview` in your browser to click through **Now Playing**, **Idle Clock**, **Library**, **Queue**, and **Devices**.

---

## 2. Connect Your Spotify Account

> Requires a **Spotify Premium** account (Spotify restricts remote playback control APIs to Premium).

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and click **Create app**.
2. Set the **Redirect URI** to your Pi's URL, for example:
   ```
   http://192.168.1.50:8765/auth/callback
   ```
   *(Or `http://127.0.0.1:8765/auth/callback` if you authorize from a browser on the Pi itself.)*
3. Copy your **Client ID** and **Client Secret** into `.env`:
   ```env
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   SPOTIFY_REDIRECT_URI=http://192.168.1.50:8765/auth/callback
   DEMO_MODE=0
   ```
4. Start the server:
   ```bash
   uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
   ```
5. Open `http://<pi-ip>:8765/auth/login` in a browser and approve Spotify access. The token is saved in `.spotify_cache` and refreshes automatically.

### Optional: Auto-start on Pi Boot (`systemd`)
Create `/etc/systemd/system/kindle-deskbuddy.service`:
```ini
[Unit]
Description=Kindle Desk Buddy Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/kindle-deskbuddy
ExecStart=/home/pi/kindle-deskbuddy/.venv/bin/uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
Then enable it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kindle-deskbuddy
```

---

## 3. Kindle 10th Gen Setup (No KUAL Required)

Make sure **`fbink`** is installed on your Kindle (e.g., via your jailbreak package manager or bundled with KOReader).

1. Copy the `kindle_client/` files to `/mnt/us/deskbuddy/` on your Kindle (via USB drive mode or `scp`):
   ```bash
   # On your PC/Pi over SSH (or drag-and-drop over USB into the "deskbuddy" folder)
   scp -r kindle_client/* root@<kindle-ip>:/mnt/us/deskbuddy/
   ```
2. Create your Kindle config from the template:
   ```bash
   cp /mnt/us/deskbuddy/deskbuddy.conf.example /mnt/us/deskbuddy/deskbuddy.conf
   ```
   Edit `/mnt/us/deskbuddy/deskbuddy.conf` and set `PI_URL` to your Raspberry Pi's IP (e.g. `http://192.168.1.50:8765`).
   Set `ROTATION="90"` (USB port on the right) or `ROTATION="270"` (USB port on the left).

3. **Launch it (two ways, no KUAL needed):**
   - **As a Kindle Scriptlet** (WinterBreak / ScriptletsForKindle): copy `start_deskbuddy.sh` to `/mnt/us/documents/DeskBuddy.sh` and tap **Desk Buddy** right from your Kindle library!
   - **Over SSH:**
     ```bash
     sh /mnt/us/deskbuddy/deskbuddy.sh
     ```

### Exiting Desk Buddy
- Tap the **native top-left corner** of the Kindle screen **4 times within 4 seconds**, or send `Ctrl+C` / `SIGTERM` over SSH. Desk Buddy cleanly restores the Kindle UI and screensaver.

### Touch Calibration (if taps feel offset)
Run the built-in touch diagnostic over SSH:
```bash
sh /mnt/us/deskbuddy/deskbuddy.sh --test-touch
```
Tap the four corners of the screen. If your digitizer reports raw coordinates (e.g. `0..4095` instead of `0..600`), set `TOUCH_MAX_X`, `TOUCH_MAX_Y`, `TOUCH_SWAP_XY`, `TOUCH_INVERT_X`, or `TOUCH_INVERT_Y` in `/mnt/us/deskbuddy/deskbuddy.conf`.
