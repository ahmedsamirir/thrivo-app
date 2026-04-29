
#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  Thrivo Guided Deploy
#  ─────────────────────────────────────────────────────────────────────
#  One-time interactive script. Walks you through:
#    1. Initializing the local git repo
#    2. Creating a private GitHub repo via `gh` CLI (or guided manual)
#    3. Pushing all files
#    4. Printing the Streamlit Cloud / Render setup URLs (you click once)
#    5. Reminding you to set DATABASE_URL secret
#
#  Run this once from the project folder. Re-running is safe (idempotent
#  for git operations).
# ═══════════════════════════════════════════════════════════════════════

set -e

# ── Colors ──
G='\033[0;32m'; B='\033[0;34m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'

print_step() { echo -e "\n${B}━━━ $1 ━━━${N}\n"; }
print_ok()   { echo -e "${G}✓ $1${N}"; }
print_warn() { echo -e "${Y}⚠ $1${N}"; }
print_err()  { echo -e "${R}✗ $1${N}"; }

# ── 0. Sanity ──
if [ ! -f "Thrivo_v10.py" ]; then
  print_err "Thrivo_v10.py not found in current directory."
  echo    "  Run this script from the project root (where Thrivo_v10.py lives)."
  exit 1
fi

if [ ! -f "db.py" ]; then
  print_err "db.py not found. Did you unzip the full v10 package here?"
  exit 1
fi

print_step "Thrivo Deploy Wizard"
echo "This will set up your repo and walk you to the deploy URL."
echo "It will NOT push anything until you confirm."

# ── 1. Collect inputs ──
read -p "GitHub username: " GH_USER
read -p "Repo name (default: thrivo-app): " REPO_NAME
REPO_NAME=${REPO_NAME:-thrivo-app}

echo
read -p "Make repo private? [Y/n]: " PRIV_ANSWER
PRIV_ANSWER=${PRIV_ANSWER:-Y}
if [[ "$PRIV_ANSWER" =~ ^[Yy]$ ]]; then
  VISIBILITY="--private"
else
  VISIBILITY="--public"
fi

# ── 2. Git init ──
print_step "Step 1 — Git setup"
if [ ! -d ".git" ]; then
  git init -b main
  print_ok "Initialized git repo"
else
  print_ok "Git repo already exists"
fi

# Create .gitignore if missing (safety net — package should ship one)
if [ ! -f ".gitignore" ]; then
  print_warn ".gitignore missing — copying default"
  cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
venv/
data_*.json
users.json
subscriptions.json
.streamlit/secrets.toml
.env
EOF
fi

# Stage and commit
git add -A
if git diff --cached --quiet; then
  print_ok "Nothing new to commit"
else
  read -p "Commit message [chore: initial Thrivo v10 deploy]: " COMMIT_MSG
  COMMIT_MSG=${COMMIT_MSG:-"chore: initial Thrivo v10 deploy"}
  git commit -m "$COMMIT_MSG"
  print_ok "Committed"
fi

# ── 3. GitHub repo creation ──
print_step "Step 2 — GitHub repo"
if command -v gh &>/dev/null; then
  if gh auth status &>/dev/null; then
    if gh repo view "$GH_USER/$REPO_NAME" &>/dev/null; then
      print_ok "Repo already exists on GitHub"
    else
      gh repo create "$GH_USER/$REPO_NAME" $VISIBILITY --source=. --remote=origin --push
      print_ok "Created and pushed to github.com/$GH_USER/$REPO_NAME"
    fi
  else
    print_warn "gh CLI installed but not logged in. Run: gh auth login"
    echo
    echo "Manual steps:"
    echo "  1. Visit https://github.com/new"
    echo "  2. Name: $REPO_NAME · ${VISIBILITY:1}"
    echo "  3. Create (don't add README/license)"
    echo "  4. Then run:"
    echo "     git remote add origin git@github.com:$GH_USER/$REPO_NAME.git"
    echo "     git branch -M main && git push -u origin main"
    exit 0
  fi
else
  print_warn "GitHub CLI (gh) not installed."
  echo "Install: https://cli.github.com/  OR do manually:"
  echo "  1. Create empty repo at https://github.com/new"
  echo "  2. git remote add origin git@github.com:$GH_USER/$REPO_NAME.git"
  echo "  3. git push -u origin main"
fi

# ── 4. Deploy URLs ──
print_step "Step 3 — Deploy"
echo "Click ONE of the following to deploy:"
echo
echo -e "  ${G}Streamlit Cloud (recommended):${N}"
echo    "    https://share.streamlit.io/deploy?repository=$GH_USER/$REPO_NAME&branch=main&mainModule=Thrivo_v10.py"
echo
echo -e "  ${G}Render:${N}"
echo    "    https://dashboard.render.com/select-repo?type=web"
echo    "    (then pick $GH_USER/$REPO_NAME)"
echo
echo -e "  ${G}Railway:${N}"
echo    "    https://railway.app/new/github?template=$GH_USER/$REPO_NAME"
echo

# ── 5. Secrets reminder ──
print_step "Step 4 — Secrets to set in your hosting dashboard"
cat <<'EOF'
After deploy starts, add these as secrets/env vars in your dashboard:

  DATABASE_URL          postgres://...        ← Supabase or Neon free tier
                                                (https://supabase.com/database)
  THRIVO_ADMIN_EMAIL    your@email.com
  THRIVO_PAYMENT_PHONE  01XXXXXXXXX
  SMTP_USER             optional — for admin email notifications
  SMTP_PASS             optional — Gmail app password

Also add the SAME DATABASE_URL to your GitHub repo:
  Settings → Secrets and variables → Actions → New repository secret
  Name: DATABASE_URL · Value: <same postgres URL>
  → this lets the daily price scraper write to your DB.
EOF

print_step "Done"
echo "Open the Streamlit Cloud link above to finish deploy."
echo "First boot takes ~3 min. After that, your app is live at:"
echo "  https://$GH_USER-$REPO_NAME-thrivo-v10.streamlit.app"
echo "(URL pattern varies — Streamlit will show you the exact one.)"
