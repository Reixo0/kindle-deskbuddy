# Kindle Desk Buddy

A lightweight, landscape e-ink **Spotify controller, synced lyrics viewer, and desk clock** for a jailbroken **Kindle 10th Generation**, powered by a **Raspberry Pi** over Wi-Fi. No KUAL required.

## Features
- **Now Playing (Album Art + Big Clock + Synced Lyrics):**
  - Prominent **Clock + Date** above large Floyd-Steinberg dithered **Album Art**.
  - Complete playback controls: **Play/Pause**, **Previous**, **Next**, **Volume −/+**, **Shuffle**, **Like/Unlike**, and **tap/slide seeking** directly on the progress bar.
  - **Live Synced Lyrics** powered by [LRCLIB](https://lrclib.net/) (free, no API key needed), showing previous, current, and upcoming lines with automatic **Japanese / CJK font support**.
- **Kindle Frontlight Brightness Controls:** Tap the **Sun (`☀`)** button in the top bar for `−`/`+` controls and **Off / Low / Med / High / Max** presets wired directly to the Kindle's native frontlight.
- **Idle Clock Mode (5-minute auto-switch):** When no music has played for 5 minutes, switches to a giant desk clock with date, Kindle battery, and a **quick Playlists / Albums / Liked Songs selector** right below the clock.
- **Full Library Browser:** Browse **Playlists** (first by default), **Albums**, **Liked Songs**, and **Recently Played**. Tap any playlist or album to browse and play individual songs, or tap **Play** to start it immediately.
- **Queue & Device Selector:** Inspect your **Up Next** queue and switch playback between any active **Spotify Connect** device (speakers, PC, phone, Raspotify, etc.).
- **Kindle Battery & Fast E-Ink Refresh:** Displays live Kindle battery % and charging status, updates every **2 seconds** while playing, skips unchanged redraws, and triggers periodic full-flashing refreshes to prevent ghosting.
- **1-Tap Exit & Offline Failsafe:** Tap **`X`** in the top-right corner to exit cleanly—or **double-tap the top-right corner** at any time to exit locally even if the Pi is offline.
- **Interactive Browser Preview (`/preview`):** Test and click through the entire UI from your PC or phone browser before deploying to the Kindle.

> **Default port:** `8765` (chosen to avoid conflicting with Pi-hole or other services on port `8080`).

---

## 1. Raspberry Pi Setup

Clone the repository onto your Raspberry Pi and create a Python virtual environment:

```bash
git clone <your-repo-url> kindle-deskbuddy
cd kindle-deskbuddy
python3 -m venv .venv
```

Activate the virtual environment and install dependencies:
- **Bash / Zsh (default on Raspberry Pi OS):**
  ```bash
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- **Fish shell:**
  ```fish
  source .venv/bin/activate.fish
  pip install -r requirements.txt
  ```

*(Recommended for Japanese / Korean / Chinese song & lyric titles)* Install Noto CJK fonts on the Pi:
```bash
sudo apt update && sudo apt install -y fonts-noto-cjk
```

Create your local `.env` file from the template:
```bash
cp .env.example .env
```

### Quick Test (Demo Mode — no Spotify account needed yet)
```bash
DEMO_MODE=1 uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
```
Open `http://<pi-ip>:8765/preview` in your browser to click through **Now Playing**, **Idle Clock**, **Library**, **Queue**, **Devices**, and **Brightness**. Press `Ctrl+C` to stop when you're ready to connect Spotify.

---

## 2. Connect Your Spotify Account

> Requires a **Spotify Premium** account (Spotify restricts remote playback control APIs to Premium).

### Step A: Create a Spotify Developer App
1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and click **Create app**.
2. Open your app's **Settings** → **Redirect URIs**, add this exact URI:
   ```
   http://127.0.0.1:8765/auth/callback
   ```
   Click **Add**, then scroll down and click **Save**.
   > **Important:** Spotify does **not** allow `http://192.168.x.x:...` or `http://localhost:...` for non-HTTPS redirect URIs — it must be `http://127.0.0.1:8765/auth/callback`.
3. Note your **Client ID** and **Client Secret**.

### Step B: Edit `.env` on the Raspberry Pi
Open `.env` with `nano .env` and fill in the existing lines at the top of the file (don't duplicate them at the bottom):

```env
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8765/auth/callback
DEMO_MODE=0
```

Save (`Ctrl+O`, `Enter`) and exit (`Ctrl+X`).

### Step C: Authorize Once
1. Start the server on the Pi:
   ```bash
   uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
   ```
2. On your PC/phone browser, open:
   ```
   http://<pi-ip>:8765/auth/login
   ```
   Log in and approve Spotify access.
3. Spotify will redirect your browser to `http://127.0.0.1:8765/auth/callback?code=...`.
   - If you are on a different computer than the Pi, your browser will show "can't connect" — that's expected!
   - Simply replace `127.0.0.1` in the address bar with `<pi-ip>` (keeping `?code=...` intact) and press **Enter**.
4. You should see **"Spotify connected!"**. Your token is now saved in `.spotify_cache` and refreshes automatically.

*(Alternative to step 3: forward the port with `ssh -L 8765:127.0.0.1:8765 <pi-user>@<pi-ip>` and open `http://127.0.0.1:8765/auth/login` directly.)*

### Optional: Auto-Start on Pi Boot (`systemd`)
Create `/etc/systemd/system/kindle-deskbuddy.service` (adjust `User` and paths if your username isn't `pi`):
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
Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kindle-deskbuddy
```

---

## 3. Kindle 10th Gen Setup (No KUAL Required)

Desk Buddy uses the Kindle's built-in `eips` for PNG rendering and KOReader's bundled `luajit` (when present, with a shell fallback) for zero-latency, auto-calibrated touch input.

1. **Copy `kindle_client/` to `/mnt/us/deskbuddy/` on your Kindle:**
   - **Over USB:** Plug the Kindle into your PC, create a `deskbuddy` folder at the root of the Kindle drive, and copy `deskbuddy.sh`, `touch_reader.sh`, and `deskbuddy.conf.example` into it.
   - **Or over SSH:**
     ```bash
     ssh root@<kindle-ip> "mkdir -p /mnt/us/deskbuddy"
     scp kindle_client/deskbuddy.sh kindle_client/touch_reader.sh kindle_client/deskbuddy.conf.example root@<kindle-ip>:/mnt/us/deskbuddy/
     ```

2. **Configure your Pi's IP on the Kindle:**
   Copy `deskbuddy.conf.example` to `deskbuddy.conf` inside `/mnt/us/deskbuddy/` and edit it:
   ```sh
   PI_URL="http://<pi-ip>:8765"
   ROTATION="90"   # 90 = USB port on the right | 270 = USB port on the left
   ```

3. **Launch Desk Buddy (choose either method):**
   - **As a Kindle Scriptlet** (WinterBreak / ScriptletsForKindle): copy `kindle_client/start_deskbuddy.sh` to `/mnt/us/documents/DeskBuddy.sh` and tap **Desk Buddy** right from your Kindle library.
   - **Over SSH:**
     ```bash
     sh /mnt/us/deskbuddy/deskbuddy.sh
     ```

### Exiting Desk Buddy
- Tap the **`X`** button in the top-right corner of the screen.
- **Offline failsafe:** If the Pi is unreachable or returning an error, **double-tap the top-right corner** within 3 seconds to exit locally without rebooting.

### Troubleshooting & Logs
- The Kindle client writes a log to `/mnt/us/deskbuddy/deskbuddy.log` (readable over USB).
- If taps feel offset on a different Kindle model, run `sh /mnt/us/deskbuddy/deskbuddy.sh --test-touch` over SSH and adjust `TOUCH_MAX_X`, `TOUCH_MAX_Y`, `TOUCH_SWAP_XY`, `TOUCH_INVERT_X`, or `TOUCH_INVERT_Y` in `deskbuddy.conf`.

---

## 4. Updating & Uninstalling

### Updating on the Raspberry Pi
```bash
cd ~/kindle-deskbuddy
git pull
```
Then restart `uvicorn` (or `sudo systemctl restart kindle-deskbuddy` if using `systemd`). Your `.env` and `.spotify_cache` are ignored by Git and stay intact.

### Removing / Reinstalling on the Raspberry Pi
If you want a fresh clone or want to completely uninstall Desk Buddy from your Pi:

```bash
# 1. Stop and remove the systemd service (if you created one)
sudo systemctl disable --now kindle-deskbuddy 2>/dev/null || true
sudo rm -f /etc/systemd/system/kindle-deskbuddy.service
sudo systemctl daemon-reload

# 2. (Optional) Back up your Spotify config & login token if you plan to reinstall
cp ~/kindle-deskbuddy/.env ~/deskbuddy.env.bak 2>/dev/null || true
cp ~/kindle-deskbuddy/.spotify_cache ~/deskbuddy.spotify_cache.bak 2>/dev/null || true

# 3. Delete the project folder (including .venv)
rm -rf ~/kindle-deskbuddy
```
*(If reinstalling, copy `~/deskbuddy.env.bak` and `~/deskbuddy.spotify_cache.bak` back into the new `~/kindle-deskbuddy/` folder as `.env` and `.spotify_cache` so you don't have to re-authorize Spotify.)*

### Removing from the Kindle
Delete the `deskbuddy` folder and `documents/DeskBuddy.sh` over USB (or via SSH: `rm -rf /mnt/us/deskbuddy /mnt/us/documents/DeskBuddy.sh`).
