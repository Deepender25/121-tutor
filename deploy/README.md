# Hosting Voice Tutor: frontend on Vercel, API on a VPS

> **Status (2026-10-22): the hosted copy is switched off.** The Vercel project is paused and
> `frontend/vercel.json` turns off deploys from GitHub (`"git": {"deploymentEnabled": false}`); the API service on
> the VPS is stopped and disabled, and the VPS doesn't pull from GitHub. Run the app locally with `run.bat`. To host
> it again: remove that `git` setting, unpause the Vercel project, then on the VPS run
> `sudo systemctl enable --now voice-tutor-api voice-tutor-backup.timer` and `sudo voice-tutor update`.

The same code runs locally (`run.bat`, no login, unchanged) and hosted. Hosting is switched on only by settings:

```
 Browser ──HTTPS──▶ tutor.edgedrop.app        (Vercel: the Next.js frontend, Mumbai region)
    │                    │ first page load asks the API, with your session cookie
    │                    ▼
    └──HTTPS / WSS──▶ api.tutor.edgedrop.app   (the VPS: Caddy :443 ──▶ FastAPI on 127.0.0.1:8766)
                                                 data on the VPS disk; Gemini + Tavily keys only here
```

- The browser calls the API directly (REST, PDF uploads, page images, the voice WebSocket), so none of it passes
  through Vercel's limits (4.5 MB requests, 5-minute connections).
- One account, email + password, created on the server. Every API route and the voice connection need a session.
  The session cookie (HttpOnly, Secure, SameSite=Lax) belongs to `tutor.edgedrop.app` and its subdomains only.
- Only the frontend's exact origin may call the API (CORS + Origin checks); pages carry a strict Content Security
  Policy; the API listens on 127.0.0.1 behind Caddy, as an unprivileged, sandboxed service.
- The server uses online embeddings (Gemini Embedding 2): a 1 GB machine can't run the local model.

| File | Where it goes |
|---|---|
| `vps/setup.sh` | Run once on the VPS (safe to re-run): swap, Python 3.13, dependencies, service, HTTPS, firewall, backups |
| `vps/api.env.example` | `/etc/voice-tutor/api.env`: the server's settings (keys, domains, limits) |
| `vps/voice-tutor-api.service` | systemd: starts on boot, restarts on failure, memory-capped, sandboxed |
| `vps/Caddyfile` | `/etc/caddy/Caddyfile`: HTTPS for the API (automatic Let's Encrypt certificates) |
| `vps/voice-tutor` | `/usr/local/bin/voice-tutor`: status, logs, restart, update, backup, account, data |
| `vps/update.sh` | `sudo voice-tutor update`: pull the latest code, reinstall, restart, health-check |
| `vps/voice-tutor-backup.*` | Nightly backup at 03:00 IST, newest 7 kept in `/var/backups/voice-tutor` |
| `vps/requirements-server.txt` | The server's dependencies: no torch (about 1.5 GB less), versions from `requirements.lock` |
| `../frontend/vercel.json` | Vercel: run the frontend's server code in Mumbai (`bom1`), next to the VPS |

## 1. DNS (Cloudflare dashboard → edgedrop.app → DNS)

| Type | Name | Content | Proxy |
|---|---|---|---|
| A | `api.tutor` | `80.225.203.19` | **DNS only** (grey cloud) |
| CNAME | `tutor` | the value Vercel shows in step 5 (e.g. `cname.vercel-dns.com`) | **DNS only** (grey cloud) |

Grey cloud matters: Caddy gets its own certificate for `api.tutor.edgedrop.app` (Cloudflare's free certificate
doesn't cover names two levels deep), and Vercel recommends DNS-only for its domains.
Keep the VPS's public IP from changing: in Oracle Cloud, make it a reserved public IP.

## 2. Oracle Cloud: open ports 80 and 443

Console → Networking → Virtual cloud networks → (the VM's VCN) → Subnet → Security list → Add ingress rules:
source `0.0.0.0/0`, TCP, destination ports `80` and `443`. (The VM's own firewall is opened by `setup.sh`;
both are needed.)

## 3. The VPS

```bash
ssh -i <your key> ubuntu@80.225.203.19
sudo git clone https://github.com/Deepender25/121-tutor.git /opt/voice-tutor/app
sudo bash /opt/voice-tutor/app/deploy/vps/setup.sh api.tutor.edgedrop.app
sudo nano /etc/voice-tutor/api.env           # add GEMINI_API_KEY (and TAVILY_API_KEY if you have one)
sudo voice-tutor user create you@example.com # asks for the password twice (10+ characters)
sudo voice-tutor restart
sudo voice-tutor status
curl https://api.tutor.edgedrop.app/api/health   # {"ok":true,"app":"voice-tutor"} once DNS + ports are open
```

The old `~/oracle-freetier-instance-creation` folder holds an Oracle Cloud API key that can control the whole
account. It isn't needed any more: delete the folder, and delete that API key in the Oracle console (your user →
API keys) if nothing else uses it.

## 4. Optional: bring your current notebooks

Copy your local library (Windows: `%LOCALAPPDATA%\voice-tutor`) to the server, then let it re-embed online:

```powershell
# on your PC (PowerShell): pack the library without caches
tar -czf voice-tutor-data.tgz -C "$env:LOCALAPPDATA\voice-tutor" --exclude=pages --exclude=voice-previews notebooks docs files settings.json
scp -i <your key> voice-tutor-data.tgz ubuntu@80.225.203.19:/tmp/
```
```bash
# on the VPS
sudo systemctl stop voice-tutor-api
sudo tar -xzf /tmp/voice-tutor-data.tgz -C /var/lib/voice-tutor/data --no-same-owner
sudo chown -R voicetutor:voicetutor /var/lib/voice-tutor/data && rm /tmp/voice-tutor-data.tgz
sudo systemctl start voice-tutor-api   # sources indexed offline get online vectors in the background
```

## 5. Vercel

1. New Project → import `Deepender25/121-tutor` → **Root Directory: `frontend`** (framework: Next.js).
2. Environment variables (Production):
   - `NEXT_PUBLIC_API_URL` = `https://api.tutor.edgedrop.app`
3. Deploy. Then Settings → Domains → add `tutor.edgedrop.app`, and create the CNAME from step 1 with the value
   Vercel shows.
4. Open https://tutor.edgedrop.app → sign in.

`NEXT_PUBLIC_API_URL` is built into the page code, so after changing it, redeploy. The region comes from
`frontend/vercel.json`.

## Everyday

```bash
sudo voice-tutor status | logs | restart
sudo voice-tutor update                       # after pushing to GitHub (Vercel redeploys the frontend by itself)
sudo voice-tutor user password you@example.com   # forgot it: also signs out every browser
sudo voice-tutor sessions end-all             # sign out every browser
sudo voice-tutor backup                       # also nightly; restore: see below
sudo voice-tutor data info                    # sizes; `data prune` removes leftovers (stops the API meanwhile)
```

Restore a backup: `sudo systemctl stop voice-tutor-api`, move `/var/lib/voice-tutor/data` aside, then
`sudo mkdir -p /var/lib/voice-tutor/data && sudo tar -xzf /var/backups/voice-tutor/<file> -C /var/lib/voice-tutor/data --no-same-owner`,
`sudo chown -R voicetutor:voicetutor /var/lib/voice-tutor/data`, and start the API. Backups stay on the same disk:
copy one off the server now and then (`scp`).

## Settings reference

API (`/etc/voice-tutor/api.env`): `VOICE_TUTOR_AUTH=on`, `VOICE_TUTOR_PUBLIC_ORIGINS`, `VOICE_TUTOR_COOKIE_DOMAIN`,
`EMBEDDING_MODEL=online`, `VOICE_TUTOR_DATA`, `VOICE_TUTOR_LOGS`, `VOICE_TUTOR_MAX_UPLOAD_MB`,
`VOICE_TUTOR_MAX_LIVE_SESSIONS`. The API refuses to start if it would be reachable without login.
Frontend (Vercel): `NEXT_PUBLIC_API_URL` (and optionally `VOICE_TUTOR_API_URL` for server-side requests, default
the same). Locally none of these are set, and everything works as before.
