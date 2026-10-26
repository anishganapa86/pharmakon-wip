# iPhone demo over HTTPS

Pharmakon is a demo. It is educational only and not a medical device. Use made-up data only: don't enter real medical or personal information.

Safari only offers "Add to Home Screen" with a working app, and the camera, over HTTPS. A free Cloudflare quick tunnel gives the dev server an HTTPS address. Nothing here deploys anything.

## Quick start

1. Install `cloudflared` once (`winget install Cloudflare.cloudflared` on Windows, `brew install cloudflared` on Mac).
2. In `.env`, set `PHARMAKON_DEMO_EMAIL` and `PHARMAKON_DEMO_PASSWORD` (made-up values), and `GEMINI_API_KEY` if you want the chat and scan.
3. From the repo root, run `scripts/start-phone.ps1` (Windows) or `scripts/start-phone.sh` (Mac). It starts the tunnel, seeds the demo account, starts the app and prints the address to open in Safari on the iPhone. The launcher stops with a message if the email or password is missing.
4. On the iPhone, open the address, log in, tap Share, then Add to Home Screen.

The quick-tunnel address changes every run, so remove the old home-screen icon and add it again each time.

## Manual steps

Use these if the launcher doesn't work for you.

1. Install `cloudflared` once (`winget install Cloudflare.cloudflared`).
2. Put your Gemini key in `.env` as `GEMINI_API_KEY=...`. The chat and scan stay off without it.
3. In PowerShell, in the repo, set the tunnel settings. Replace the host with the one cloudflared prints in step 5:

   ```powershell
   $env:PHARMAKON_COOKIE_SECURE = "1"
   $env:VITE_API_BASE = "/api"
   $env:PHARMAKON_ALLOWED_HOSTS = ".trycloudflare.com"
   ```

4. Start the app: `.\run.ps1` (API on :8000, client on :5173).
5. In a second window run `cloudflared tunnel --url http://localhost:5173`. It prints an address like `https://something-random.trycloudflare.com`.
6. Stop `.\run.ps1`, then set the exact address and start it again, so the API accepts requests from it:

   ```powershell
   $env:PHARMAKON_CORS_ORIGINS = "https://something-random.trycloudflare.com"
   .\run.ps1
   ```

7. Create the demo account (optional): set `PHARMAKON_DEMO_EMAIL` and `PHARMAKON_DEMO_PASSWORD` in `.env`, then run `python scripts/seed_demo_user.py`.
8. On the iPhone, open the tunnel address in Safari and log in. Tap Share, then Add to Home Screen.
9. Open Pharmakon from the home screen. On the first Scan, allow the camera.

The tunnel address changes every time cloudflared restarts, so repeat steps 5 and 6 when it does, and re-add the home-screen icon.

## How it fits together

The phone only talks to the tunnel. The Vite server answers pages and forwards `/api/...` to the API on port 8000, so the session cookie is first-party and `Secure`. The API only accepts writes from the origin in `PHARMAKON_CORS_ORIGINS`.

## If something goes wrong

- **"Blocked request. This host is not allowed"** means Vite doesn't know the tunnel host. Check `PHARMAKON_ALLOWED_HOSTS` was set in the same window as `.\run.ps1`, then restart it.
- **Login works but you land back on the login page**, or every write returns 403. The cookie isn't `Secure`, or the origin doesn't match. Check `PHARMAKON_COOKIE_SECURE=1` and that `PHARMAKON_CORS_ORIGINS` is the exact tunnel address with `https://` and no trailing path.
- **The camera button does nothing or the photo isn't read.** Safari asks for camera access once per site. Reset it in Settings, Safari, Camera. Make sure `GEMINI_API_KEY` is set. A "busy" message means the free Gemini quota ran out; wait and try again.
- **An old version keeps showing.** Remove the app from the home screen and add it again, or clear website data for the tunnel address in Settings, Safari.
