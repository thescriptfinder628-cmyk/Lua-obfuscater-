# Publishing Forsakenis4

You have three ways to put this on the internet. **Option 1 is the one to pick** — it takes
about three minutes and gives you a permanent `https://…onrender.com` URL, free.

Everything needed is already in the repo (`render.yaml`, `Dockerfile`, `fly.toml`,
`railway.json`). You do not need to edit any code.

---

## Option 1 — Render (free, permanent URL, ~3 minutes)

1. Go to <https://render.com> and sign up with **GitHub**.
2. Click **New +** → **Blueprint**.
3. Pick the repository `Lua-obfuscater-`.
4. Render reads `render.yaml` and shows a service called `forsakenis4-obfuscator`.
   Click **Apply** / **Create**.
5. Wait for the first build (~2–4 minutes). When it turns green, your site is live at:

   ```
   https://forsakenis4-obfuscator.onrender.com
   ```

   (the exact name is shown at the top of the service page — use **Copy URL**)

**Notes**

* Free instances sleep after ~15 minutes of inactivity; the first request afterwards takes
  ~30 seconds to wake up. Everything still works.
* Every `git push` to `main` redeploys automatically (`autoDeploy: true`).
* The `verify` checkbox needs a Lua interpreter; the blueprint installs `lupa`, so it works.

### Custom domain (e.g. `obfuscator.yourname.com`)

In Render: your service → **Settings** → **Custom Domains** → **Add**.
Then at your domain registrar add the CNAME record Render shows you. HTTPS is automatic.

---

## Option 2 — Fly.io (free allowance, faster cold starts)

```bash
# once
curl -L https://fly.io/install.sh | sh
fly auth signup            # or: fly auth login

# in the repo
fly launch --no-deploy     # accepts fly.toml, pick a region if asked
fly deploy
```

When it finishes, Fly prints your URL:

```
https://forsakenis4-obfuscator.fly.dev
```

Change the `app` name in `fly.toml` if that name is taken.

---

## Option 3 — Railway / any Docker host

**Railway**

1. <https://railway.app> → **New Project** → **Deploy from GitHub repo** → pick the repo.
2. Railway reads `railway.json` and builds the `Dockerfile` automatically.
3. **Settings → Networking → Generate Domain** → you get `https://…up.railway.app`.

**Any VPS with Docker**

```bash
git clone https://github.com/thescriptfinder628-cmyk/Lua-obfuscater-.git
cd Lua-obfuscater-
docker build -t forsakenis4 .
docker run -d --restart=always -p 80:8000 --name forsakenis4 forsakenis4
```

**Without Docker** (any box with Python 3.9+)

```bash
pip install . && pip install lupa
PORT=8000 python tools/playground.py
```

Put nginx/Caddy in front for TLS, or use `caddy reverse-proxy --from yourdomain.com --to 127.0.0.1:8000`.

---

## The command line tool (no hosting at all)

If you only need to protect files, you do not need to publish anything:

```bash
pip install -e .
lua-obfuscator script.lua --level nightmare --verify -o protected.lua
```

On Windows the command is the same once Python is installed (`py -m luaobf …` also works).

---

## Checking a deployment

Every deployment exposes a health endpoint:

```bash
curl https://your-url/healthz
# {"ok": true}
```

A quick end-to-end check:

```bash
curl -X POST https://your-url/api/obfuscate \
  -H 'Content-Type: application/json' \
  -d '{"code":"print(\"hi\", 1+1)","level":"heavy","seed":1,"verify":true}'
```

You should get back JSON with a `code` field and `"verified": true`.

---

## What I could not do from inside this session

* **Deploy it for you.** The sandbox has no route to Render/Fly/Railway/Cloudflare, so no
  deploy command can be executed here.
* **Give you a permanent link.** The URL you may have seen earlier
  (`https://8000-….e2b.app`) is a preview of this sandbox and dies with it — it is not hosting.

That is why this document exists: the repo is deploy-ready, and the three steps above are all
that is left.

---

## Costs and limits, honestly

| Host | Free tier | Catch |
|------|-----------|-------|
| Render | yes | sleeps after ~15 min idle; ~30 s cold start |
| Fly.io | yes (allowance) | needs a card on file for some accounts |
| Railway | trial credit | then usage-based |
| VPS | no | you manage it |

The app is a single Python process with no database, no uploads and no persistence, so any
512 MB instance is plenty. Request bodies are capped at 2 MB of Lua.
