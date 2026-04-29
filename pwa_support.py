"""
═══════════════════════════════════════════════════════════════════════
  Thrivo PWA Support — iOS Home Screen Install
  ─────────────────────────────────────────────────────────────────────
  Adds the meta tags, manifest link, service-worker registration, and
  iOS-specific touch-icon/splash hints needed for Safari "Add to Home
  Screen" to produce a proper app-like experience.

  Usage:
    from pwa_support import inject_pwa
    st.set_page_config(...)
    inject_pwa()          # ← add this ONE line right after set_page_config

  Files that must be reachable at runtime:
    /static/manifest.json
    /static/sw.js
    /static/icon-180.png    (iOS touch icon)
    /static/icon-192.png    (Android/PWA standard)
    /static/icon-512.png    (PWA splash source)
    /static/apple-splash-*.png (optional, iOS splash screens)

  On Streamlit Cloud / Render / Railway, a folder named `static/` in
  your repo root is served at `/static/*` automatically.
═══════════════════════════════════════════════════════════════════════
"""

import streamlit as st
import streamlit.components.v1 as components


# ── Customize these to match your branding ──
PWA_CONFIG = {
    "name":             "Thrivo",
    "short_name":       "Thrivo",
    "description":      "The Personal Growth Operating System",
    "theme_color":      "#22c55e",  # brand green
    "bg_color_dark":    "#080c14",
    "bg_color_light":   "#f8fafc",
    "start_url":        "/",
    "display":          "standalone",
    "orientation":      "portrait",
}


def _manifest_link_and_meta_html() -> str:
    """
    Returns the HTML that injects manifest + iOS meta tags into <head>.
    We use a hidden iframe trick because Streamlit doesn't expose <head>
    directly — the script below writes the tags into the parent document.
    """
    return f"""
<script>
(function() {{
    // Only inject once per page load
    if (window.__thrivo_pwa_injected) return;
    window.__thrivo_pwa_injected = true;

    const parentDoc = window.parent.document;
    const head = parentDoc.head;

    // Helper to add a tag if one with the same key doesn't exist
    function addTag(tagName, attrs) {{
        const existing = head.querySelector(
            tagName + Object.keys(attrs).map(k => `[${{k}}="${{attrs[k]}}"]`).join("")
        );
        if (existing) return;
        const el = parentDoc.createElement(tagName);
        Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
        head.appendChild(el);
    }}

    // ── Manifest (the PWA spec) ──
    addTag("link", {{ rel: "manifest", href: "/static/manifest.json" }});

    // ── Theme color (iOS status bar, Android chrome) ──
    addTag("meta", {{ name: "theme-color", content: "{PWA_CONFIG['theme_color']}" }});

    // ── iOS specific: enable home-screen install as standalone app ──
    addTag("meta", {{ name: "apple-mobile-web-app-capable", content: "yes" }});
    addTag("meta", {{ name: "mobile-web-app-capable", content: "yes" }});
    addTag("meta", {{ name: "apple-mobile-web-app-status-bar-style", content: "black-translucent" }});
    addTag("meta", {{ name: "apple-mobile-web-app-title", content: "{PWA_CONFIG['short_name']}" }});

    // ── Touch icon (what iOS uses for home screen icon) ──
    addTag("link", {{ rel: "apple-touch-icon", href: "/static/icon-180.png" }});
    addTag("link", {{ rel: "apple-touch-icon", sizes: "180x180", href: "/static/icon-180.png" }});
    addTag("link", {{ rel: "icon", sizes: "192x192", href: "/static/icon-192.png" }});
    addTag("link", {{ rel: "icon", sizes: "512x512", href: "/static/icon-512.png" }});

    // ── Viewport: critical for iOS full-screen PWA ──
    let vp = head.querySelector('meta[name="viewport"]');
    if (vp) {{
        vp.setAttribute("content",
            "width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no");
    }} else {{
        addTag("meta", {{
            name: "viewport",
            content: "width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no"
        }});
    }}

    // ── Register service worker (offline shell + faster loads) ──
    if ("serviceWorker" in window.parent.navigator) {{
        window.parent.navigator.serviceWorker
            .register("/static/sw.js", {{ scope: "/" }})
            .catch(function(err) {{ console.warn("Thrivo SW registration failed:", err); }});
    }}

    // ── iOS safe-area insets (so content isn't hidden under notch) ──
    const safeAreaStyle = parentDoc.createElement("style");
    safeAreaStyle.textContent = `
        @supports (padding: env(safe-area-inset-top)) {{
            .stApp {{
                padding-top: env(safe-area-inset-top) !important;
                padding-bottom: env(safe-area-inset-bottom) !important;
            }}
        }}
        /* Hide Streamlit's "Deploy" / hamburger / footer in standalone mode */
        @media (display-mode: standalone) {{
            header[data-testid="stHeader"] {{ display: none !important; }}
            footer {{ display: none !important; }}
            #MainMenu {{ visibility: hidden !important; }}
        }}
    `;
    head.appendChild(safeAreaStyle);
}})();
</script>
"""


def inject_pwa():
    """
    Call this ONCE, right after st.set_page_config(), to enable PWA support.
    Zero effect on existing app behavior — purely additive.
    """
    # The height=0 invisible iframe is the reliable Streamlit-safe way to run
    # DOM mutation JS that targets the parent document.
    components.html(_manifest_link_and_meta_html(), height=0, width=0)


def pwa_install_banner():
    """
    Optional: show a subtle banner on iOS prompting users to install.
    Call this inside any Streamlit page (e.g., after login) if you want
    to nudge users toward installing.
    """
    st.markdown("""
    <div id="thrivo-install-hint" style="
        background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
        color: white; padding: 12px 16px; border-radius: 10px;
        font-size: 0.88rem; margin-bottom: 12px; display: none;">
        📲 <b>Install Thrivo:</b> Tap <b>Share</b> → <b>Add to Home Screen</b>
    </div>
    <script>
    (function() {
      // Show only on iOS Safari, and only if not already standalone
      const isIOS = /iPad|iPhone|iPod/.test(window.parent.navigator.userAgent);
      const isStandalone = window.parent.navigator.standalone === true ||
                           window.parent.matchMedia("(display-mode: standalone)").matches;
      if (isIOS && !isStandalone) {
        const hint = window.parent.document.getElementById("thrivo-install-hint");
        if (hint) hint.style.display = "block";
      }
    })();
    </script>
    """, unsafe_allow_html=True)
