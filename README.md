# Kindle Desk Buddy

A lightweight, landscape e-ink **Spotify controller + desk clock** for a jailbroken **Kindle 10th Generation**, powered by a **Raspberry Pi** over Wi-Fi. No KUAL required.

## Features
- **Album Art Centerpiece (Now Playing):** Large Floyd-Steinberg dithered album art in landscape, with full playback controls (**Play/Pause**, **Previous**, **Next**, **Volume −/+**, **Shuffle**, and **Like/Unlike**).
- **Idle Clock Mode (5-minute auto-switch):** When no music has played for 5 minutes, the screen switches to a giant desk clock with date, Kindle battery, and a **quick Albums / Liked Songs / Playlists selector** right below the clock so you can start music with one tap.
- **Full Library Browser:** Browse your **Saved Albums** (tap to play the album or open its tracklist to pick a specific song), **Liked Songs**, **Playlists**, and **Recently Played**.
- **Queue & Device Selector:** Inspect your **Up Next** queue and switch playback between any active **Spotify Connect** device (speakers, PC, phone, Raspotify, etc.).
- **Kindle Battery & Smart E-Ink Refresh:** Displays live Kindle battery % and charging status, skips redraws when nothing changed, and triggers full flashing refreshes automatically to prevent ghosting.
- **Interactive Browser Preview (`/preview`):** Test the entire UI and click every button from your desktop or phone browser before deploying to the Kindle.

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

Create your local `.env` file from the template:
```bash
cp .env.example .env
```

### Quick Test (Demo Mode — no Spotify account needed yet)
```bash
DEMO_MODE=1 uvicorn pi_server.app:app --host 0.0.0.0 --port 8765
```
Open `http://<pi-ip>:8765/preview` in your browser to click through **Now Playing**, **Idle Clock**, **Library**, **Queue**, and **Devices**. Press `Ctrl+C` to stop when you're ready to connect Spotify.

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

Make sure **`fbink`** is installed on your jailbroken Kindle (via your jailbreak package manager or bundled with KOReader).

1. **Copy `kindle_client/` to `/mnt/us/deskbuddy/` on your Kindle:**
   - **Over USB:** Plug the Kindle into your PC, create a `deskbuddy` folder at the root of the Kindle drive, and copy all files from `kindle_client/` into it.
   - **Or over SSH:**
     ```bash
     ssh root@<kindle-ip> "mkdir -p /mnt/us/deskbuddy"
     scp kindle_client/* root@<kindle-ip>:/mnt/us/deskbuddy/
     ```

2. **Configure your Pi's IP on the Kindle:**
   Copy `deskbuddy.conf.example` to `deskbuddy.conf` inside `/mnt/us/deskbuddy/` and edit it:
   ```sh
   PI_URL="http://<pi-ip>:8765"
   ROTATION="90"   # 90 = USB port on the right | 270 = USB port on the left
   ```

3. **Launch Desk Buddy (choose either method):**
   - **As a Kindle Scriptlet** (WinterBreak / ScriptletsForKindle): copy `start_deskbuddy.sh` to `/mnt/us/documents/DeskBuddy.sh` and tap **Desk Buddy** right from your Kindle library.
   - **Over SSH:**
     ```bash
     sh /mnt/us/deskbuddy/deskbuddy.sh
     ```

### Exiting Desk Buddy
Tap the **native top-left corner** of the Kindle screen **4 times within 4 seconds** (or press `Ctrl+C` over SSH). Desk Buddy will cleanly restore the Kindle UI and screensaver.

### Touch Calibration (if taps feel offset)
Run the built-in touch test over SSH:
```bash
sh /mnt/us/deskbuddy/deskbuddy.sh --test-touch
```
Tap the four corners of the screen. If your digitizer reports raw coordinates (e.g. `0..4095` instead of `0..600`), set `TOUCH_MAX_X`, `TOUCH_MAX_Y`, `TOUCH_SWAP_XY`, `TOUCH_INVERT_X`, or `TOUCH_INVERT_Y` in `/mnt/us/deskbuddy/deskbuddy.conf`.
