# Deploying as a website

The app is a standard Streamlit app, so it becomes a real site in one click on
**Streamlit Community Cloud** (free, serves straight from this GitHub repo).

## One-click deploy (recommended)

1. Go to **[share.streamlit.io](https://share.streamlit.io)** and sign in **with GitHub**
   (the account that owns this repo).
2. Use this deploy link (or click *New app* and fill the same values):

   > **https://share.streamlit.io/deploy?repository=okhawaja1337-oss/nyc-legislation&branch=main&mainModule=app.py**

   - Repository: `okhawaja1337-oss/nyc-legislation` · Branch: `main` · Main file: `app.py`
3. Click **Deploy**. First boot takes a couple of minutes; after that you have a
   permanent URL like `https://<your-app-name>.streamlit.app`.

Every push to `main` redeploys the site automatically.

## 🔒 Keep it private (strongly recommended)

The Whip Room carries internal political intelligence (statuses, notes, staff
contacts). On Streamlit Cloud:

- In the app's **Settings → Sharing**, set *Who can view this app* to
  **Only specific people** and invite your staff's emails.
- Keep the GitHub repo private (Cloud deploys private repos fine).

## 🔑 Secrets — set the keys once for everyone

In the app's **Settings → Secrets**, paste (any subset; all optional):

```toml
ANTHROPIC_API_KEY = "sk-ant-..."     # AI briefings, law wiki, profiles, messaging
NYS_API_KEY       = "..."            # NY State Open Legislation
CONGRESS_API_KEY  = "..."            # Congress.gov
SOCRATA_APP_TOKEN = "..."            # NYC Open Data rate limit (optional)
LLM_COUNCIL_URL   = "https://..."    # LLM Council server, if you run one reachable from the cloud
```

The app reads `st.secrets` → environment variable → in-app field, so staff no
longer need to paste keys per session (the in-app fields still override).

## What persists where (honest notes)

- **Committed data** (the LD roster, all code) — permanent, versioned in git.
- **`.appstate/` data** (whip boards & history, notes, memory, pinned fiscal
  dataset) — persists across reruns and app reboots on the same machine, but
  **Streamlit Cloud containers are ephemeral**: a redeploy or platform restart
  resets them to the committed defaults. Export reports / use the Report Builder
  for anything you must keep, or ask for a hosted-database upgrade when that
  becomes limiting.
- **Caches** (Legistar pulls, AI outputs) — per-session/TTL; they rebuild.

## Alternatives

| Host | Why |
|---|---|
| **Hugging Face Spaces** (Streamlit SDK) | Free, private spaces, simple |
| **Render / Railway / Fly.io** | Custom domain, always-on, persistent disk for `.appstate/` |
| Your own box | `pip install -r requirements.txt && streamlit run app.py --server.headless true --server.port 80` behind any reverse proxy |

For a custom domain (e.g. a council.nyc-adjacent name) or persistent whip
history across redeploys, Render/Railway with a small persistent volume is the
step up from Community Cloud.
