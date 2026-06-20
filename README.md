# FPP Custom Web UI

A mobile-friendly, password-protected control panel for [Falcon Pi Player](https://www.falconchristmas.com/). Runs on the same Raspberry Pi as FPP and gives you a clean, easy-to-use interface for managing your light show from any device on your network.

---

## Features

- **Controls** — one-click play buttons for all FPP playlists and sequences
- **Custom Color** — per-zone color picker across up to 15 pixel overlay zones
- **Scenes** — save named color combinations across multiple zones and recall them instantly
- **Scheduler** — view, create, and edit FPP schedule entries including solar-time events
- **Settings** — customize site name, colors, background, and zone display names
- **Password management** — change your login password from the Settings page
- **Port 80 access** — available at `http://<pi-ip>/CustomUI` alongside the standard FPP UI

---

## Requirements

- Raspberry Pi running **FPP 8.x or later** (tested on FPP 9.5)
- Python 3.9 or higher (pre-installed on all FPP OS images)
- Must run **on the same Pi as FPP**

---

## Installation via FPP Plugin Manager (recommended)

1. In FPP go to **Content → Plugins**
2. In the search box, paste the URL below and click **Get Plugin Info**:
   ```
   https://raw.githubusercontent.com/BLDSB/fpp-CustomUI/main/pluginInfo.json
   ```
3. Click **Install**
4. Watch the progress popup — your **initial login password** will be displayed there
5. Navigate to `http://<pi-ip>/CustomUI` and log in

> Change the auto-generated password immediately after first login via **Settings → Security**.

---

## Manual Installation

```bash
cd /home/fpp
git clone https://github.com/BLDSB/fpp-CustomUI.git fpp-CustomUI
cd fpp-CustomUI
bash scripts/setup.sh
```

The setup script will:
- Create a Python virtual environment and install dependencies
- Generate a `.env` file with random secrets
- Prompt you to set an admin password
- Install and start the `fpp-ui` systemd service
- Configure the Apache reverse proxy at `/CustomUI`

---

## Accessing the UI

| URL | Notes |
|-----|-------|
| `http://<pi-ip>/CustomUI` | Port 80, alongside the FPP UI (recommended) |
| `http://<pi-ip>:5000` | Direct Flask port (always available) |

---

## Environment Variables

All settings live in `.env` (created automatically during install). The `.env.example` file documents each variable.

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Flask session secret — auto-generated |
| `ADMIN_PASSWORD_HASH` | Bcrypt hash of the admin password — set during install |
| `FPP_BASE_URL` | FPP REST API base URL (default: `http://localhost/api`) |
| `INTERNAL_TOKEN` | Token for internal FPP playlist callbacks — auto-generated |

---

## FPP Setup: Pixel Overlay Models

The Custom Color and Scenes pages work with FPP's **Pixel Overlay Models**. Create models named `Zone 1` through `Zone 15` (and optionally `All`) in FPP under **Content Setup → Pixel Overlay Models**. The Settings page lets you rename or hide zones that don't apply to your setup.

---

## Updating

**Via plugin:** go to FPP → Content → Plugins → Installed Plugins → Check for Updates.

**Manually:**
```bash
cd /home/fpp/fpp-CustomUI
git pull
sudo bash fpp_install.sh
```

---

## Troubleshooting

**View logs:**
```bash
sudo journalctl -u fpp-ui -f
# or
tail -f /home/fpp/fpp-CustomUI/fpp-ui.log
```

**Restart the service:**
```bash
sudo systemctl restart fpp-ui
```

---

## Compatibility

Tested on FPP 9.5.3, Raspberry Pi 4, Raspberry Pi OS (Debian 12). The FPP REST API used is stable across FPP 8.x and 9.x.
