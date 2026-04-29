# Thrivo v10 — Production Deploy Guide

**End state:** Public URL anyone can visit, real Postgres database (data survives restarts), daily price scraper running automatically, PWA-installable on iPhone, public price ticker visible without login.

**Time:** ~25 minutes first time. ~5 minutes if you've used Supabase + Streamlit before.

**Cost:** $0 on free tiers (Supabase + Streamlit Cloud + GitHub Actions).

---

## 📦 What's in this package

```
thrivo-v10/
├── Thrivo_v10.py              ← main app (Postgres-backed, public landing)
├── db.py                      ← database layer (auto: Postgres or JSON)
├── pwa_support.py             ← iOS home-screen install
├── deploy.sh                  ← guided deploy script
├── requirements.txt
├── .gitignore                 ← excludes user data + secrets
├── .streamlit/
│   └── config.toml
├── .github/workflows/
│   └── scrape-prices.yml      ← daily cron job
├── scripts/
│   └── scrape_prices.py       ← gold/USD/BTC/EGX scraper
└── static/                    ← PWA icons + manifest
    ├── manifest.json
    ├── sw.js
    ├── icon-180.png
    ├── icon-192.png
    ├── icon-512.png
    ├── icon-512-maskable.png
    └── favicon-32.png
```

---

## 🚀 Quick path (TL;DR)

```bash
# Unzip the package, then:
cd thrivo-v10
./deploy.sh           # follows prompts, creates GitHub repo, commits, pushes
# Click the Streamlit Cloud URL it prints
# Add DATABASE_URL secret in Streamlit Cloud
# Add DATABASE_URL secret in GitHub repo settings
# Done.
```

If `deploy.sh` doesn't work for you (Windows, no `gh`, etc), use the manual steps below.

---

## ✅ Step 1 — Get a free Postgres database

This is the most important step. Without this, your data is wiped on every restart.

### Recommended: Supabase

1. Go to **https://supabase.com** → sign up (free, no credit card).
2. Click **New project**:
   - Name: `thrivo`
   - DB password: generate a strong one, **save it somewhere**
   - Region: Frankfurt (closest free region to Egypt)
   - Plan: Free
3. Wait 1–2 minutes for the project to provision.
4. Click **Connect** (top right) → **Connection string** tab → **URI** → **Direct connection**.
5. Copy the URI, looks like:
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxxxxxxx.supabase.co:5432/postgres
   ```
6. Replace `[YOUR-PASSWORD]` with the password you saved.
7. **Save this string — this is your `DATABASE_URL`.** You'll need it twice.

### Alternative: Neon

Same idea — https://neon.tech → New Project → grab the connection string. Slightly faster cold starts than Supabase.

---

## ✅ Step 2 — Push to GitHub

### Option A: Run the deploy script
```bash
cd thrivo-v10
./deploy.sh
```
Answer the prompts. The script handles git init, commit, GitHub repo creation (via `gh` CLI), and push.

### Option B: Manual
```bash
cd thrivo-v10
git init -b main
git add .
git commit -m "chore: initial deploy"
# Create empty private repo on github.com/new (don't add README)
git remote add origin git@github.com:YOUR_USER/thrivo-app.git
git push -u origin main
```

---

## ✅ Step 3 — Deploy to Streamlit Cloud

1. Go to **https://share.streamlit.io** → sign in with GitHub.
2. Click **New app** → select repo `thrivo-app`, branch `main`, main file `Thrivo_v10.py`.
3. Click **Advanced settings**:
   - Python version: `3.11`
   - Add **Secrets** (paste this, replacing values):
     ```toml
     DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@db.xxx.supabase.co:5432/postgres"
     THRIVO_ADMIN_EMAIL = "your@email.com"
     THRIVO_PAYMENT_PHONE = "01XXXXXXXXX"

     # Optional — only if you want admin notification emails
     SMTP_USER = "your-gmail@gmail.com"
     SMTP_PASS = "your-gmail-app-password"
     ```
4. Click **Deploy**. First boot takes 2–3 minutes.
5. You'll get a URL like `https://your-thrivo-app.streamlit.app`. **This is your public URL.**

### What happens on first boot
- `db.py` connects to Postgres → creates the schema (5 tables)
- `_load_users()` finds an empty users table → creates default admin: `admin` / `admin1234`
- **Sign in as admin and immediately change the password!**

---

## ✅ Step 4 — Set up the daily price scraper

The scraper lives in your GitHub repo (`.github/workflows/scrape-prices.yml`) and runs daily at 04:00 UTC. It needs your `DATABASE_URL` to write to your DB.

1. Go to **github.com/YOUR_USER/thrivo-app** → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
2. Name: `DATABASE_URL` · Value: same Postgres URL from Step 1.
3. Save.
4. Go to the **Actions** tab → click **Daily Price Scraper** → **Run workflow** → **Run workflow** (manual first run to verify).
5. Wait ~1 minute. Refresh. Should see green ✓.
6. After it succeeds, your repo will have a `public_prices.json` file committed by the bot.

The scraper now runs every day at 06:00 Cairo automatically. EGX trades from 10:00, so morning data is fresh by the time anyone opens the app.

---

## ✅ Step 5 — Visit your site

1. Open your `*.streamlit.app` URL in any browser.
2. **Without logging in**, you should see:
   - Thrivo hero
   - Live prices ticker (Gold 21k, USD/EGP, BTC, EGX COMI)
   - Source line: "📡 source: database (Postgres) · updated 2h ago"
3. Sign in as `admin` / `admin1234` → change your password under settings.
4. Add yourself a real account if you want a non-admin daily-driver login.

---

## 📱 Step 6 — Install on iPhone (optional)

1. Open the `*.streamlit.app` URL in **Safari** (must be Safari, not Chrome).
2. Tap **Share** → **Add to Home Screen** → **Add**.
3. Thrivo green icon appears on home screen.
4. Tap to launch full-screen — feels like a native app.

See `static/manifest.json` and `pwa_support.py` for the iOS setup details.

---

## 🔧 Verifying everything works

After deploy, run through this checklist:

- [ ] `*.streamlit.app` loads in 5–10 seconds
- [ ] Public landing shows price tiles WITHOUT login
- [ ] Sign in as `admin` / `admin1234` works
- [ ] Sign up creates a "pending" account (admin must approve)
- [ ] Admin panel shows pending signups for approval
- [ ] After approval, user can log in
- [ ] User data persists across redeploys (try editing something, then push a new commit, redeploy, log back in — data should be there)
- [ ] GitHub Action `Daily Price Scraper` succeeded once
- [ ] After a successful scrape, `public_prices.json` exists in the repo

---

## 🐛 Troubleshooting

### "Error connecting to database" on first load
- Check `DATABASE_URL` is set correctly in Streamlit Cloud secrets
- Supabase: ensure you used the **direct connection** URI, not pgbouncer
- The URL must contain `?sslmode=require` (or db.py adds it automatically)

### Public price ticker shows "—" for everything
- Cron hasn't run yet → trigger manually from GitHub Actions tab
- If cron fails: check the Actions log for which sources are dead today
- The on-visit cache will fall back to live scrape — first-visitor pays 5–10s latency

### Streamlit Cloud says "your app is over its resource limits"
- Streamlit Cloud free tier has a 1GB memory cap. Your app uses ~400MB normally.
- If it OOMs: switch to Render Hobby ($7/mo) or upgrade Streamlit Cloud.

### GitHub Action fails with "psycopg2.OperationalError"
- DATABASE_URL secret in **GitHub repo** (separate from Streamlit) wasn't set
- Re-do Step 4 #1-3

### Users see Streamlit error tracebacks publicly
- Already prevented: `.streamlit/config.toml` has `showErrorDetails = false`
- If you DO want them while debugging, flip that to true temporarily

### Default admin password
- **Change `admin1234` immediately on first login.** It's only there to bootstrap.

---

## 🔒 Security notes

- All passwords stored as SHA-256 hashes (db.py, `users.password_hash` column)
- Postgres connection uses `sslmode=require`
- Streamlit secrets are encrypted at rest (Streamlit Cloud / Render / Railway all do this)
- `users.json`, `data_*.json`, `subscriptions.json` are in `.gitignore` — they're for local dev only; production uses DB
- If you suspect a breach: rotate `DATABASE_URL` (regenerate password in Supabase) and revoke any exposed Streamlit secrets

---

## 🎯 What's automated vs manual

| Task | Automated | Manual |
|---|---|---|
| User auth, sessions, approval queue | ✅ | — |
| Per-user data persistence (Postgres) | ✅ | — |
| Daily price scrape (cron) | ✅ | — |
| Public landing with live prices | ✅ | — |
| Live scraper fallback when cron stale | ✅ | — |
| GitHub repo creation | ✅ via `deploy.sh` | or do via gh.com |
| Streamlit Cloud deploy | — | One click |
| Setting DATABASE_URL secret | — | Twice (Streamlit + GitHub) |
| First admin password change | — | **Important — do it!** |

---

## 🚀 Upgrades for later

- **Custom domain** — buy `thrivo.app` (~$15/yr), add it in Streamlit Cloud → Settings → Custom domain
- **iOS push notifications** — needs a push service + iOS 16.4+ users; ask when ready
- **App Store distribution** — Capacitor wrapper around the same web app
- **Stripe billing** — paid plans currently use manual approval; add Stripe Checkout when you have paying customers
