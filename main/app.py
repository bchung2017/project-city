#!/usr/bin/env python3
# PROJECT CITY — PROJECTS. Flask + SQLite. python app.py -> http://127.0.0.1:5000
# Views: /  project list (CRUD, list.html)   /city  city visualizer (city.html).
# The right-side nav is injected at serve time so the source files stay pristine.
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.abspath(__file__))
# Persistence lives in Supabase Postgres, not on Render's ephemeral disk. Use the
# Supabase *Session pooler* URI (port 5432 on *.pooler.supabase.com): it is
# IPv4-compatible, whereas the direct db.*.supabase.co host is IPv6-only and
# Render has no IPv6 egress. Required in every environment — set it locally too.
DATABASE_URL = os.environ["DATABASE_URL"]
LIST_FILE = os.path.join(BASE, "list.html")
CITY_FILE = os.path.join(BASE, "city.html")
app = Flask(__name__)

# ---- auth ------------------------------------------------------------------
# Single-user login: one password, checked against a scrypt hash held in the
# environment, and a Flask signed-session cookie. Render sets RENDER=true itself,
# so no hand-rolled prod flag is needed.
IS_PROD = os.environ.get("RENDER") == "true"

if IS_PROD:
    # Fail at boot rather than fall back to a known key — a default secret means
    # anyone who reads this source can forge a session cookie.
    SECRET_KEY = os.environ["SECRET_KEY"]
    PW_HASH = os.environ["PW_HASH"]
else:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-a-secret")
    # Dev default password: "city". Override with PW_HASH to test the real thing.
    PW_HASH = os.environ.get(
        "PW_HASH",
        "scrypt:32768:8:1$ChhdePne5Duyg4rd$6f95dc643299c02eb6a962cc6ccfa6cd6903382f99b364a0"
        "2873146c0cea2205d623b0f61a2681be13324510b553b72cadc926f6ff4761022eb0d0e37030b345",
    )

app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # Must be conditional: a Secure cookie is refused over http://localhost, so
    # hardcoding True makes local login silently bounce back to the form.
    SESSION_COOKIE_SECURE=IS_PROD,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

if IS_PROD:
    # Render terminates TLS at its edge and forwards plain HTTP, so without this
    # Flask thinks every request is insecure and emits http:// external URLs.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# One user means brute force is the only attack surface worth caring about.
LOGIN_MAX_TRIES = 10
LOGIN_WINDOW = 60.0
_login_hits = {}


def rate_limited(ip):
    now = time.time()
    hits = [t for t in _login_hits.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_hits[ip] = hits
    return len(hits) >= LOGIN_MAX_TRIES


def record_attempt(ip):
    _login_hits.setdefault(ip, []).append(time.time())


LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<title>PROJECT CITY — LOGIN</title>
<style>
  body{margin:0;height:100vh;display:grid;place-items:center;background:#0E7C9B;
       font:14px/1.4 ui-monospace,Menlo,Consolas,monospace;color:#08313F}
  form{background:#F4EFE2;border:4px solid #08313F;box-shadow:6px 6px 0 #08313F;
       padding:28px;min-width:280px}
  h1{margin:0 0 18px;font-size:15px;letter-spacing:.14em}
  input{width:100%;box-sizing:border-box;padding:10px;font:inherit;
        border:3px solid #08313F;background:#fff;margin-bottom:14px}
  button{width:100%;padding:10px;font:inherit;font-weight:700;letter-spacing:.1em;
         border:3px solid #08313F;background:#FFCC0F;cursor:pointer}
  .err{background:#F02D0E;color:#fff;padding:8px;margin-bottom:14px;font-size:12px}
</style>
<form method=post>
  <h1>■ PROJECT CITY</h1>
  {% if error %}<div class=err>{{ error }}</div>{% endif %}
  <input type=password name=pw autofocus autocomplete=current-password placeholder=PASSWORD>
  <button>ENTER</button>
</form>"""


LOGIN_ERRORS = {"pw": "WRONG PASSWORD", "rate": "TOO MANY TRIES — WAIT A MINUTE"}


def _login_bounce(nxt, err):
    # Post-Redirect-GET: a failed login answers with a redirect to the GET form
    # (reason in ?err=), never an inline body on the POST. So reloading the page
    # is a plain GET — no browser "confirm form resubmission" prompt / ERR_CACHE_MISS,
    # and the form comes back cleanly with the error shown.
    args = {"err": err}
    if nxt:
        args["next"] = nxt
    return redirect(url_for("login", **args))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        nxt = request.args.get("next", "")
        if rate_limited(ip):
            return _login_bounce(nxt, "rate")
        if check_password_hash(PW_HASH, request.form.get("pw", "")):
            session.clear()
            session.permanent = True
            session["ok"] = True
            return redirect(request.args.get("next") or url_for("index"))
        record_attempt(ip)
        return _login_bounce(nxt, "pw")
    return render_template_string(LOGIN_HTML, error=LOGIN_ERRORS.get(request.args.get("err")))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.before_request
def require_login():
    if session.get("ok") or request.endpoint in ("login", "static", "healthz"):
        return
    # The API answers 401 so fetch() sees a real failure instead of parsing a
    # login page as JSON.
    if request.path.startswith("/api/"):
        abort(401)
    return redirect(url_for("login", next=request.path))

# Postgres port of the original SQLite schema:
#   INTEGER PRIMARY KEY  -> SERIAL PRIMARY KEY (auto-increment identity)
#   REAL                 -> DOUBLE PRECISION. SQLite's REAL is an 8-byte double;
#                          Postgres REAL is only 4-byte, which would round a
#                          completion like 0.3 to 0.30000001 on the wire. DOUBLE
#                          PRECISION keeps the exact value the UI stored.
#   done stays INTEGER 0/1 so the 1/0 writes and bool() reads are untouched.
# The tag name keeps its case-insensitive uniqueness via the citext extension
# (Postgres has no per-column COLLATE NOCASE): a CITEXT column compares and
# unique-checks case-insensitively, so `name = %s` and UNIQUE(project_id, name)
# behave exactly as they did under NOCASE — "Bug" and "bug" still collide.
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS citext;
CREATE TABLE IF NOT EXISTS projects (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  pos INTEGER NOT NULL DEFAULT 0,
  render_progress INTEGER NOT NULL DEFAULT 1,
  logo TEXT NOT NULL DEFAULT '',
  logo_src TEXT NOT NULL DEFAULT '',
  logo_size INTEGER NOT NULL DEFAULT 32,
  profile TEXT NOT NULL DEFAULT 'personal'
);
CREATE TABLE IF NOT EXISTS items (
  id SERIAL PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  completion DOUBLE PRECISION NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  pos INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT '',
  start_at TEXT NOT NULL DEFAULT '',
  due_at TEXT NOT NULL DEFAULT '',
  depends_on INTEGER REFERENCES items(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS tags (
  id SERIAL PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name CITEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#00B7B9',
  pos INTEGER NOT NULL DEFAULT 0,
  UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS item_tags (
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  PRIMARY KEY (item_id, tag_id)
);
"""

# An item is a (text, completion) pair; completion runs 0.00 -> 1.00 and an item
# only counts as built (a new story) at 1.0. `done` is kept as the boolean
# mirror of completion >= 1 so both stay usable from either end.
#
# Scheduling: an item carries the whole Gantt tuple — start_at, due_at, the
# completion it already had, and a single predecessor. A project's Gantt chart is
# therefore a *derived view* of its items (see /api/projects/<id>/gantt), never a
# second table that could disagree with the todo list. Bars are created, moved,
# and deleted through ordinary item CRUD.
#   created_at  instant, ISO-8601 UTC ('2026-07-12T01:39:00Z') — stamped once, immutable
#   start_at    calendar date, 'YYYY-MM-DD' — bar's left edge; '' means "unscheduled"
#   due_at      calendar date, 'YYYY-MM-DD' — bar's right edge; '' means "no target"
#   depends_on  item id in the same project, or NULL — the finish-to-start arrow
# Dates are days, not instants: a Gantt row is a span of calendar days, and
# storing them as dates keeps them free of timezone drift.
#
# Tags: a tag is *owned by a project*, not global — two projects may both define
# "blocked" and they are different tags with their own colors. `item_tags` is the
# join, and every write checks that the tag and the item belong to the same
# project, so a tag can never leak across the wall. Names are unique per project
# and case-insensitive (COLLATE NOCASE), so "Bug" and "bug" cannot coexist.
# Deleting a tag unapplies it everywhere (cascade); it does not touch the items.
#
# Logo: a project can wear a logo, painted GIANT on its tower's two street-facing
# walls. The image is *never* uploaded. The browser rasters it down to an N x N
# grid of solid cells (N = logo_size, the "pixel size" setting) and sends only
# that grid, so this server needs no image library — which is the point: it
# decodes nothing an attacker controls, and the city paints cells rather than
# decoding an Image at bake time.
#   logo       JSON {"size": N, "cells": [...]} — row-major, '#rrggbb' or null
#              (null = transparent, the wall shows through). This is what the
#              city renders and what /api/projects ships.
#   logo_src   a bounded PNG data-URL thumbnail (<= LOGO_SRC_MAX chars), kept
#              only so the *size* setting can be changed without re-uploading:
#              the client re-rasters from it. Not sent in the project list.
#   logo_size  N, clamped to [LOGO_MIN, LOGO_MAX]; 32 is the default. The tower
#              wall is 48px wide, so N up to 48 renders at 1px per cell.
MIGRATIONS = [
    ("items", "completion", "DOUBLE PRECISION NOT NULL DEFAULT 0", "UPDATE items SET completion = done"),
    ("projects", "render_progress", "INTEGER NOT NULL DEFAULT 1", None),
    ("items", "created_at", "TEXT NOT NULL DEFAULT ''", None),
    ("items", "start_at", "TEXT NOT NULL DEFAULT ''", None),
    ("items", "due_at", "TEXT NOT NULL DEFAULT ''", None),
    ("items", "depends_on", "INTEGER REFERENCES items(id) ON DELETE SET NULL", None),
    ("projects", "logo", "TEXT NOT NULL DEFAULT ''", None),
    ("projects", "logo_src", "TEXT NOT NULL DEFAULT ''", None),
    ("projects", "logo_size", "INTEGER NOT NULL DEFAULT 32", None),
    # Baked profiles: a project belongs to one profile. Existing rows backfill to
    # 'personal', so all pre-existing data becomes the Personal profile; the Work
    # profile simply starts with no rows (blank city) until projects are created
    # while it is active. Items/tags cascade from projects, so scoping the project
    # by profile scopes the whole tree — the two profiles' data never mix.
    ("projects", "profile", "TEXT NOT NULL DEFAULT 'personal'", None),
]

# ---- profiles (baked; no CRUD yet) -----------------------------------------
# Two fixed profiles the user switches between from the right-side nav. The active
# one lives in the signed session cookie, so it survives reloads and is known both
# to the API (which scopes every project query by it) and to the nav renderer.
PROFILES = [
    {"key": "personal", "label": "Personal"},
    {"key": "work", "label": "Work"},
]
PROFILE_KEYS = {p["key"] for p in PROFILES}
DEFAULT_PROFILE = "personal"


def current_profile():
    p = session.get("profile", DEFAULT_PROFILE)
    return p if p in PROFILE_KEYS else DEFAULT_PROFILE


NAV_CSS = """
#pcnav { position: fixed; top: 34px; right: 14px; z-index: 50;
  display: flex; flex-direction: column; min-width: 168px;
  background: #073A3B; border: 4px solid #073A3B;
  box-shadow: 6px 6px 0 rgba(7,58,59,0.45); }
#pcnav .pcnav-cap { font: 700 10px/1 monospace; letter-spacing: 3px;
  text-transform: uppercase; color: #00B7B9; padding: 8px 12px 7px; user-select: none; }
#pcnav a { display: flex; align-items: center; gap: 9px;
  font: 700 15px/1 monospace; letter-spacing: 3px;
  text-transform: uppercase; text-decoration: none; color: #FFFFFF;
  text-shadow: 2px 2px 0 #073A3B; padding: 14px 14px;
  border-top: 4px solid #073A3B; background: #474B50; transition: none; }
#pcnav a::before { content: "\\25B8"; font-size: 13px; color: #00B7B9; text-shadow: none; }
#pcnav a:hover { background: #FFCC0F; color: #073A3B; text-shadow: none; }
#pcnav a:hover::before { color: #073A3B; }
#pcnav a.active { background: #FFCC0F; color: #073A3B; text-shadow: none;
  box-shadow: inset 6px 0 0 #F02D0E; }
#pcnav a.active::before { content: "\\25A0"; color: #F02D0E; }
"""


def nav_snippet(active):
    def cls(name):
        return ' class="active"' if name == active else ""
    return (
        f"<style>{NAV_CSS}</style>\n"
        f'<div id="pcnav">'
        f'<div class="pcnav-cap">Project City</div>'
        f'<a href="/"{cls("projects")}>Projects</a>'
        f'<a href="/city"{cls("city")}>City</a>'
        f'<a href="/profiles"{cls("profiles")}>Profiles</a>'
        f"</div>\n"
    )


# ---- loading overlay + per-session projects cache -------------------------
# Injected at serve time (like the nav) so list.html / city.html stay pristine.
# Two problems it solves:
#   1. Both pages paint an empty UI, then ~1s later the /api/projects fetch lands
#      and the DOM is populated all at once — a jarring flash. An opaque overlay
#      covers the page until that first populate has rendered.
#   2. Every page load re-fetches /api/projects over the network. A per-session
#      (sessionStorage, NON-persistent) cache makes the first paint of a
#      subsequent load in the same session instant.
#
# Staleness is made structurally impossible, not merely unlikely, by the order of
# operations — this is a single-writer app (one user, this browser), so the cache
# can only go stale from THIS client's own writes:
#   * A write (any non-GET /api/*) removes the cached snapshot BEFORE the request
#     is even sent, and increments a mutation generation. So there is never a
#     moment where a committed write coexists with an older cache.
#   * A GET /api/projects records the generation when it starts and only writes
#     its body to the cache if the generation is unchanged when it returns — so a
#     GET that was already in flight when a write happened can never repopulate
#     the cache with pre-write data.
# Therefore a present cache is provably no older than the last write; since no
# other writer exists, it equals current server state. The city's 4s poll and the
# background refresh keep it current thereafter.
LOADING_HEAD = """<style>
#pcload{position:fixed;inset:0;z-index:100000;display:flex;align-items:center;
  justify-content:center;background:#0E7C9B;color:#F4EFE2;
  font:14px/1.4 ui-monospace,Menlo,Consolas,monospace;transition:opacity .38s ease}
#pcload.pc-hide{opacity:0;pointer-events:none}
#pcload .pc-box{display:flex;flex-direction:column;align-items:center;gap:18px}
#pcload .pc-title{font-weight:700;letter-spacing:.28em;font-size:15px;
  text-shadow:2px 2px 0 #08313F}
#pcload .pc-spin{width:34px;height:34px;border:5px solid rgba(8,49,63,.35);
  border-top-color:#FFCC0F;border-radius:50%;animation:pcspin .8s linear infinite}
#pcload .pc-sub{font-size:11px;letter-spacing:.2em;color:#08313F;opacity:.85}
@keyframes pcspin{to{transform:rotate(360deg)}}
</style>
<script>
(function(){
  "use strict";
  var SC="pc:projects:v1:__PROFILE__";  // key is per-profile so switching profiles never serves the other's snapshot
  var gen=0;                    // mutation generation (bumped on every write INITIATE)
  var inflight=0;              // writes currently open — a GET overlapping any is never cached
  var primed=false;            // has the first /api/projects GET been served?
  var revealed=false;          // has the overlay been dismissed?
  var orig=window.fetch.bind(window);
  var urlOf=function(i){return typeof i==="string"?i:(i&&i.url)||"";};
  var methOf=function(i,init){return ((init&&init.method)||(i&&i.method)||"GET").toUpperCase();};
  var isProjects=function(u){return /\\/api\\/projects(?:\\?.*)?$/.test(u.split("#")[0]);};
  var isApi=function(u){return u.indexOf("/api/")!==-1;};

  function reveal(){
    if(revealed) return; revealed=true;
    // Wait for the app's fetch .then (a microtask) to render, then paint, THEN
    // fade — so the user never sees the empty UI underneath.
    requestAnimationFrame(function(){requestAnimationFrame(function(){
      var o=document.getElementById("pcload"); if(!o) return;
      o.classList.add("pc-hide");
      setTimeout(function(){if(o&&o.parentNode)o.parentNode.removeChild(o);},450);
    });});
  }

  window.fetch=function(input,init){
    var u=urlOf(input), m=methOf(input,init);
    if(isApi(u) && m!=="GET"){
      // WRITE: drop the snapshot before the request goes out; bump gen and mark a
      // write in flight so any GET that overlaps it refuses to cache. Drop again
      // and clear the mark on completion — success OR failure (never leak inflight).
      try{sessionStorage.removeItem(SC);}catch(e){}
      gen++; inflight++;
      var settle=function(){inflight--; try{sessionStorage.removeItem(SC);}catch(e){}};
      return orig(input,init).then(function(r){settle(); return r;},function(e){settle(); throw e;});
    }
    if(m==="GET" && isProjects(u)){
      var g=gen, inf=inflight;           // snapshot the write state when this GET begins
      var net=orig(input,init).then(function(r){
        if(r&&r.ok){ r.clone().text().then(function(t){
          // Cache ONLY if NO write overlapped this GET's whole life: none in flight
          // when it started (inf), none in flight now, none initiated meanwhile
          // (g===gen). This closes the read/write-commit race that a gen check alone
          // leaves open, so a snapshot can never hold pre-write or half-applied data.
          if(inf===0 && inflight===0 && g===gen){try{sessionStorage.setItem(SC,t);}catch(e){}}
        },function(){}); }
        return r;
      });
      net.then(reveal,reveal);           // network landed -> dismiss overlay (cold load)
      if(!primed){
        primed=true;
        // Serve the first load from cache only when clean (no write in flight; any
        // prior write already deleted it, so a present snapshot is fresh this session).
        var cached=null; try{cached=(inf===0?sessionStorage.getItem(SC):null);}catch(e){}
        if(cached){
          reveal();                      // cache paints instantly -> dismiss now, do NOT wait for net
          net.catch(function(){});       // network refreshes the cache in the background
          return Promise.resolve(new Response(cached,{status:200,headers:{"Content-Type":"application/json"}}));
        }
      }
      return net;
    }
    return orig(input,init);
  };

  // Fallback: if a page never issues a populate fetch (e.g. served standalone),
  // don't leave the overlay stuck.
  setTimeout(reveal, 8000);
})();
</script>
"""

LOADING_BODY = (
    '<div id="pcload" role="status" aria-live="polite"><div class="pc-box">'
    '<div class="pc-title">■ PROJECT CITY</div>'
    '<div class="pc-spin"></div>'
    '<div class="pc-sub">LOADING…</div>'
    "</div></div>\n"
)


def serve_page(filename, active):
    # Read a pristine frontend file and inject the right-side nav at serve time.
    # Prefer a {{ nav|safe }} placeholder if present; otherwise drop the nav in
    # just before </body> so navigation is available on every page.
    with open(filename, encoding="utf-8") as f:
        html = f.read()
    nav = nav_snippet(active)
    # Loading overlay + cache: the <script> must install before the app's own
    # scripts run, so it goes in <head>; the overlay div goes first in <body> so it
    # paints over everything from the very first frame.
    loading_head = LOADING_HEAD.replace("__PROFILE__", current_profile())
    html = html.replace("<head>", "<head>\n" + loading_head, 1)
    body_m = re.search(r"<body[^>]*>", html)
    if body_m:
        html = html[: body_m.end()] + "\n" + LOADING_BODY + html[body_m.end():]
    if "{{ nav|safe }}" in html:
        return html.replace("{{ nav|safe }}", nav)
    idx = html.rfind("</body>")
    return html + nav if idx == -1 else html[:idx] + nav + html[idx:]


def db():
    if "db" not in g:
        # dict_row gives rows that index by column name (row["title"]), matching
        # the old sqlite3.Row access. Foreign keys are always enforced in
        # Postgres, so there is no PRAGMA to enable. psycopg's Connection.execute()
        # returns a cursor just like sqlite3, so db().execute(...) calls carry over.
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    # psycopg runs the whole multi-statement SCHEMA in one execute() as long as it
    # carries no parameters (it does not). The migration decls are plain SQL types
    # that Postgres also understands, and information_schema replaces PRAGMA
    # table_info for the "is this column already here?" check.
    with psycopg.connect(DATABASE_URL) as con:
        con.execute(SCHEMA)
        for table, col, decl, backfill in MIGRATIONS:
            cols = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = %s",
                    (table,),
                ).fetchall()
            }
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                if backfill:
                    con.execute(backfill)
        con.commit()


def clamp01(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_day(v):
    # A calendar date, or '' / None to clear it. Returns ('', None) for cleared,
    # (day, None) when valid, ('', reason) when the client sent something bogus.
    if v is None or (isinstance(v, str) and not v.strip()):
        return "", None
    if not isinstance(v, str):
        return "", "must be a 'YYYY-MM-DD' date string"
    v = v.strip()
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return "", "must be a 'YYYY-MM-DD' date string"
    return v, None


def day_index(day):
    # Whole days since the epoch — the natural x-axis unit for a Gantt bar.
    return (datetime.strptime(day, "%Y-%m-%d").date() - datetime(1970, 1, 1).date()).days


HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TAG_DEFAULT = "#00B7B9"


def parse_tag_name(v):
    if not isinstance(v, str) or not v.strip():
        return "", "name required"
    v = " ".join(v.split())  # collapse inner whitespace; a tag is a label, not prose
    if len(v) > 24:
        return "", "name must be 24 characters or fewer"
    return v, None


def parse_color(v, fallback=TAG_DEFAULT):
    if v is None or (isinstance(v, str) and not v.strip()):
        return fallback, None
    if not isinstance(v, str) or not HEX_RE.match(v.strip()):
        return "", "color must be a '#rrggbb' hex string"
    return v.strip().lower(), None


# ---- logo limits -----------------------------------------------------------
# These are the *server's* limits and they are enforced here, independently of
# the browser. The client applies its own (stricter, file-level) limits before
# it rasters — see RASTER LIMITS in list.html — but a hand-rolled request never
# reaches SQLite without passing these.
LOGO_MIN, LOGO_MAX, LOGO_DEFAULT = 4, 38, 32
LOGO_SRC_MAX = 262144  # chars of data-URL; a 256x256 PNG thumbnail fits easily
LOGO_SRC_PREFIX = "data:image/png;base64,"
# The body carries a data-URL thumbnail and up to 16*16 hex cells, so 1 MiB is
# generous; it also caps every other endpoint, none of which send anything big.
MAX_BODY_BYTES = 1048576
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES


@app.errorhandler(413)
def too_large(_e):
    # Without this the client would get Flask's HTML error page and the fetch
    # wrapper would report a bare status code.
    return jsonify({"error": f"request body must be {MAX_BODY_BYTES} bytes or fewer"}), 413


def parse_logo_size(v):
    if v is None:
        return LOGO_DEFAULT, None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0, f"size must be an integer {LOGO_MIN}-{LOGO_MAX}"
    if not LOGO_MIN <= n <= LOGO_MAX:
        return 0, f"size must be {LOGO_MIN}-{LOGO_MAX}"
    return n, None


def parse_logo_cells(cells, n):
    # Exactly n*n entries, each a '#rrggbb' or null. Anything else is a bad grid,
    # not something to coerce — a half-valid logo would bake into the skyline.
    if not isinstance(cells, list):
        return None, "cells must be a list"
    if len(cells) != n * n:
        return None, f"cells must hold exactly {n * n} entries for size {n}"
    out = []
    for c in cells:
        if c is None:
            out.append(None)
            continue
        if not isinstance(c, str) or not HEX_RE.match(c.strip()):
            return None, "each cell must be a '#rrggbb' hex string or null"
        out.append(c.strip().lower())
    if not any(out):
        return None, "logo is fully transparent"
    return out, None


def parse_logo_src(v):
    # A PNG data-URL the client produced from its own canvas. We never decode it;
    # we bound it, check it is the shape we hand out, and store it so the size
    # setting can be re-rastered without a re-upload.
    if not isinstance(v, str) or not v.startswith(LOGO_SRC_PREFIX):
        return "", "src must be a 'data:image/png;base64,' data URL"
    if len(v) > LOGO_SRC_MAX:
        return "", f"src must be {LOGO_SRC_MAX} characters or fewer"
    b64 = v[len(LOGO_SRC_PREFIX):]
    if not b64 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", b64):
        return "", "src is not valid base64"
    return v, None


def logo_json(row):
    # The grid only. logo_src is deliberately withheld from the project payload:
    # the city does not need it, and shipping a thumbnail per project on every
    # poll would dwarf everything else on the wire.
    if not row["logo"]:
        return None
    try:
        g = json.loads(row["logo"])
    except ValueError:
        return None
    return {"size": g.get("size"), "cells": g.get("cells")}


def tag_json(t, count=0):
    return {
        "id": t["id"],
        "project_id": t["project_id"],
        "name": t["name"],
        "color": t["color"],
        "pos": t["pos"],
        # How many of the project's items currently wear this tag. Derived, so the
        # manager can show usage before you delete something.
        "items": count,
    }


def project_tags(d, pid):
    rows = d.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM item_tags it WHERE it.tag_id = t.id) AS n"
        " FROM tags t WHERE t.project_id = %s ORDER BY t.pos, t.id",
        (pid,),
    ).fetchall()
    return [tag_json(r, r["n"]) for r in rows]


def item_tag_ids(d, iid):
    rows = d.execute(
        "SELECT t.id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        " WHERE it.item_id = %s ORDER BY t.pos, t.id",
        (iid,),
    ).fetchall()
    return [r["id"] for r in rows]


def project_item_tags(d, pid):
    # One query for the whole project rather than one per item.
    rows = d.execute(
        "SELECT it.item_id, it.tag_id FROM item_tags it"
        " JOIN items i ON i.id = it.item_id"
        " JOIN tags  t ON t.id = it.tag_id"
        " WHERE i.project_id = %s ORDER BY t.pos, t.id",
        (pid,),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append(r["tag_id"])
    return out


def item_json(i, tags=()):
    c = float(i["completion"])
    # created_at is '' for items that predate scheduling; it is left empty rather
    # than backfilled, because inventing a creation date would be a lie the UI
    # could not distinguish from a real one.
    created = i["created_at"] or None
    start, due = i["start_at"] or None, i["due_at"] or None
    today = day_index(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return {
        "id": i["id"],
        "text": i["text"],
        "completion": c,
        "done": c >= 1.0,
        "notes": i["notes"],
        "pos": i["pos"],
        # Tag ids only — the tag bodies live once on the project, so a rename or
        # recolor cannot leave stale copies scattered across the items.
        "tags": list(tags),
        # --- schedule: the Gantt tuple, carried on the item itself ---
        "created_at": created,
        "start_at": start,
        "due_at": due,
        "depends_on": i["depends_on"],
        # Derived, never stored. The client recomputes age on its own clock so it
        # stays live between loads; these are here for API consumers (and the
        # future chart) that want the numbers without doing date math.
        "age_days": None if not created else max(
            0, today - day_index(created[:10])
        ),
        "days_left": None if not due else day_index(due) - today,
        "overdue": bool(due) and c < 1.0 and day_index(due) < today,
    }


def depends_cycles(d, iid, dep):
    # Would making `iid` depend on `dep` close a loop? Walk dep's own chain of
    # predecessors; if it leads back to iid, the edge is illegal. Chains are at
    # most one item long per hop, so this is a walk, not a search.
    seen = set()
    cur = dep
    while cur is not None and cur not in seen:
        if cur == iid:
            return True
        seen.add(cur)
        r = d.execute("SELECT depends_on FROM items WHERE id = %s", (cur,)).fetchone()
        cur = r["depends_on"] if r else None
    return False


def project_json(row, items, tags=None, item_tags=None):
    # tags / item_tags may be passed in pre-fetched (bulk list path) to avoid a
    # per-project query. When omitted (single-project callers after a mutation)
    # they're fetched here as before.
    d = db()
    tmap = item_tags if item_tags is not None else project_item_tags(d, row["id"])
    ptags = tags if tags is not None else project_tags(d, row["id"])
    out = [item_json(i, tmap.get(i["id"], ())) for i in items]
    built = sum(1 for i in out if i["done"])
    days = [day_index(i["start_at"]) for i in out if i["start_at"]]
    days += [day_index(i["due_at"]) for i in out if i["due_at"]]
    return {
        "id": row["id"],
        "title": row["title"],
        "pos": row["pos"],
        # The city only renders a project's progress when this is enabled.
        "render_progress": bool(row["render_progress"]),
        # The project's whole tag vocabulary, sent once. Items reference it by id.
        "tags": ptags,
        # The rastered logo grid the city paints onto the tower walls, or null.
        "logo": logo_json(row),
        "logo_size": row["logo_size"],
        # A fresh project is a 1-story office; every completed item adds a story.
        "stories": 1 + built,
        "open": len(out) - built,
        # The project's scheduled envelope — the Gantt chart's x-axis extent.
        # None when nothing in the project has been given dates yet.
        "span": None if not days else {
            "start": min(
                (i["start_at"] for i in out if i["start_at"]),
                key=day_index, default=None,
            ),
            "end": max(
                (i["due_at"] for i in out if i["due_at"]),
                key=day_index, default=None,
            ),
            "days": max(days) - min(days) + 1,
        },
        "items": out,
    }


@app.get("/")
def index():
    return serve_page(LIST_FILE, "projects")


@app.get("/city")
def city():
    return serve_page(CITY_FILE, "city")


# ---- profiles page + switcher ----------------------------------------------
# Baked demo: two fixed profiles, no CRUD. The Profiles tab lands here; picking a
# profile sets it active in the session and drops you back into the Projects view,
# now scoped to that profile's data (the city, which polls /api/projects, follows).
PROFILES_HTML = """<!doctype html><meta charset=utf-8>
<title>PROJECT CITY — PROFILES</title>
<style>
  body{margin:0;min-height:100vh;background:#0E7C9B;color:#08313F;
       font:14px/1.4 ui-monospace,Menlo,Consolas,monospace;
       display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;padding:40px}
  h1{margin:0;font-size:15px;letter-spacing:.18em;color:#F4EFE2;text-shadow:2px 2px 0 #08313F}
  .cards{display:flex;gap:20px;flex-wrap:wrap;justify-content:center}
  a.card{display:flex;flex-direction:column;gap:8px;min-width:200px;
         background:#F4EFE2;border:4px solid #08313F;box-shadow:6px 6px 0 #08313F;
         padding:22px;text-decoration:none;color:#08313F}
  a.card:hover{background:#FFCC0F}
  a.card.active{background:#FFCC0F;box-shadow:inset 6px 0 0 #F02D0E,6px 6px 0 #08313F}
  .name{font-weight:700;letter-spacing:.14em;font-size:15px}
  .state{font-size:11px;letter-spacing:.16em;color:#08313F;opacity:.75}
  .active .state{color:#F02D0E;opacity:1;font-weight:700}
  .hint{font-size:11px;letter-spacing:.14em;color:#F4EFE2;opacity:.85}
</style>
<h1>■ PROFILES</h1>
<div class=cards>
  {% for p in profiles %}
  <a class="card{{ ' active' if p.key == current else '' }}" href="/profile/{{ p.key }}">
    <span class=name>{{ p.label }}</span>
    <span class=state>{{ 'ACTIVE' if p.key == current else 'SWITCH' }}</span>
  </a>
  {% endfor %}
</div>
<div class=hint>PICK A PROFILE — ITS PROJECTS AND CITY ARE KEPT SEPARATE</div>
{{ nav|safe }}"""


@app.get("/profiles")
def profiles_page():
    return render_template_string(
        PROFILES_HTML,
        profiles=PROFILES,
        current=current_profile(),
        nav=nav_snippet("profiles"),
    )


@app.get("/profile/<name>")
def switch_profile(name):
    # Set the active profile (ignoring anything not baked) and return to the app.
    if name in PROFILE_KEYS:
        session["profile"] = name
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    # Unauthenticated liveness probe (exempted in require_login). The one trivial
    # query is deliberate: a scheduled ping to this endpoint keeps BOTH Render's
    # free web service and the free Supabase project from going idle — hitting
    # /login would wake Render but never touch Postgres. Leaks nothing.
    db().execute("SELECT 1")
    return jsonify({"ok": True})


@app.get("/api/projects")
def list_projects():
    # Bulk-loaded to stay flat in the project count: a profile with dozens of
    # projects (the baked Work city has ~85) once ran 1 + 3*N queries here — items,
    # tags and item_tags per project — which, over a remote pooled connection, took
    # seconds and made the city's 4s poll fall behind and hang. Now it's a constant
    # 4 queries regardless of N; assembly happens in Python.
    d = db()
    projects = d.execute(
        "SELECT * FROM projects WHERE profile = %s ORDER BY pos, id", (current_profile(),)
    ).fetchall()
    if not projects:
        return jsonify([])
    pids = [p["id"] for p in projects]

    items_by_p = {}
    for it in d.execute(
        "SELECT * FROM items WHERE project_id = ANY(%s) ORDER BY project_id, pos, id", (pids,)
    ).fetchall():
        items_by_p.setdefault(it["project_id"], []).append(it)

    tags_by_p = {}
    for t in d.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM item_tags it WHERE it.tag_id = t.id) AS n"
        " FROM tags t WHERE t.project_id = ANY(%s) ORDER BY t.pos, t.id", (pids,)
    ).fetchall():
        tags_by_p.setdefault(t["project_id"], []).append(tag_json(t, t["n"]))

    # (project_id, item_id, tag_id) for every applied tag, ordered by tag pos so the
    # per-item tag lists match the single-project path exactly.
    itags_by_p = {}
    for r in d.execute(
        "SELECT i.project_id, it.item_id, it.tag_id FROM item_tags it"
        " JOIN items i ON i.id = it.item_id"
        " JOIN tags  t ON t.id = it.tag_id"
        " WHERE i.project_id = ANY(%s) ORDER BY t.pos, t.id", (pids,)
    ).fetchall():
        itags_by_p.setdefault(r["project_id"], {}).setdefault(r["item_id"], []).append(r["tag_id"])

    out = [
        project_json(
            p,
            items_by_p.get(p["id"], []),
            tags=tags_by_p.get(p["id"], []),
            item_tags=itags_by_p.get(p["id"], {}),
        )
        for p in projects
    ]
    return jsonify(out)


@app.post("/api/projects")
def create_project():
    title = (request.json or {}).get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    d = db()
    prof = current_profile()
    # pos is per-profile so each profile's ordering is independent.
    pos = d.execute(
        "SELECT COALESCE(MAX(pos), -1) + 1 AS pos FROM projects WHERE profile = %s", (prof,)
    ).fetchone()["pos"]
    # dict_row rows index by name, and Postgres has no lastrowid — RETURNING hands
    # back the freshly assigned SERIAL id in the same round trip.
    row = d.execute(
        "INSERT INTO projects (title, pos, profile) VALUES (%s, %s, %s) RETURNING *",
        (title, pos, prof),
    ).fetchone()
    d.commit()
    return jsonify(project_json(row, [])), 201


@app.patch("/api/projects/<int:pid>")
def update_project(pid):
    body = request.json or {}
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        d.execute("UPDATE projects SET title = %s WHERE id = %s", (title, pid))
    if "render_progress" in body:
        d.execute(
            "UPDATE projects SET render_progress = %s WHERE id = %s",
            (1 if body["render_progress"] else 0, pid),
        )
    d.commit()
    row = d.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone()
    items = d.execute(
        "SELECT * FROM items WHERE project_id = %s ORDER BY pos, id", (pid,)
    ).fetchall()
    return jsonify(project_json(row, items))


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    d = db()
    cur = d.execute("DELETE FROM projects WHERE id = %s", (pid,))
    d.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return "", 204


# =====================================================================
# LOGO — see the note by MIGRATIONS. The client rasters; we validate and store.
# =====================================================================
def project_payload(d, pid):
    row = d.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone()
    items = d.execute(
        "SELECT * FROM items WHERE project_id = %s ORDER BY pos, id", (pid,)
    ).fetchall()
    return project_json(row, items)


@app.get("/api/projects/<int:pid>/logo")
def get_logo(pid):
    # The only endpoint that hands back logo_src — the list page needs it to
    # re-raster at a new size without asking for the file again.
    d = db()
    row = d.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "logo": logo_json(row),
        "src": row["logo_src"] or None,
        "size": row["logo_size"],
    })


@app.put("/api/projects/<int:pid>/logo")
def set_logo(pid):
    body = request.json or {}
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    n, err = parse_logo_size(body.get("size"))
    if err:
        return jsonify({"error": err}), 400
    cells, err = parse_logo_cells(body.get("cells"), n)
    if err:
        return jsonify({"error": err}), 400
    src, err = parse_logo_src(body.get("src"))
    if err:
        return jsonify({"error": err}), 400
    d.execute(
        "UPDATE projects SET logo = %s, logo_src = %s, logo_size = %s WHERE id = %s",
        (json.dumps({"size": n, "cells": cells}, separators=(",", ":")), src, n, pid),
    )
    d.commit()
    return jsonify(project_payload(d, pid))


@app.delete("/api/projects/<int:pid>/logo")
def clear_logo(pid):
    # Clears the thumbnail too — a removed logo should leave nothing behind.
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    d.execute(
        "UPDATE projects SET logo = '', logo_src = '', logo_size = %s WHERE id = %s",
        (LOGO_DEFAULT, pid),
    )
    d.commit()
    return jsonify(project_payload(d, pid))


@app.post("/api/projects/<int:pid>/items")
def create_item(pid):
    body = request.json or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    comp = clamp01(body.get("completion", 0)) or 0.0
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    start, err = parse_day(body.get("start_at"))
    if err:
        return jsonify({"error": "start_at " + err}), 400
    due, err = parse_day(body.get("due_at"))
    if err:
        return jsonify({"error": "due_at " + err}), 400
    if start and due and day_index(due) < day_index(start):
        return jsonify({"error": "due_at is before start_at"}), 400
    pos = d.execute(
        "SELECT COALESCE(MAX(pos), -1) + 1 AS pos FROM items WHERE project_id = %s", (pid,)
    ).fetchone()["pos"]
    row = d.execute(
        "INSERT INTO items (project_id, text, completion, done, pos, created_at, start_at, due_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (pid, text, comp, 1 if comp >= 1.0 else 0, pos, now_iso(), start, due),
    ).fetchone()
    d.commit()
    return jsonify(item_json(row)), 201


@app.patch("/api/items/<int:iid>")
def update_item(iid):
    body = request.json or {}
    d = db()
    row = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    if "text" in body:
        text = str(body["text"]).strip()
        if not text:
            return jsonify({"error": "text required"}), 400
        d.execute("UPDATE items SET text = %s WHERE id = %s", (text, iid))
    # completion and done are two views of one value; writing either syncs both.
    if "completion" in body:
        comp = clamp01(body["completion"])
        if comp is None:
            return jsonify({"error": "completion must be a number 0.0 - 1.0"}), 400
        d.execute(
            "UPDATE items SET completion = %s, done = %s WHERE id = %s",
            (comp, 1 if comp >= 1.0 else 0, iid),
        )
    elif "done" in body:
        done = 1 if body["done"] else 0
        d.execute(
            "UPDATE items SET done = %s, completion = %s WHERE id = %s",
            (done, float(done), iid),
        )
    if "notes" in body:
        d.execute("UPDATE items SET notes = %s WHERE id = %s", (str(body["notes"]), iid))

    # --- schedule ---
    # Validate against the item's *resulting* state, not just the payload: moving
    # only start_at still has to stay consistent with the due_at already stored.
    if "start_at" in body or "due_at" in body:
        start, due = row["start_at"], row["due_at"]
        if "start_at" in body:
            start, err = parse_day(body["start_at"])
            if err:
                return jsonify({"error": "start_at " + err}), 400
        if "due_at" in body:
            due, err = parse_day(body["due_at"])
            if err:
                return jsonify({"error": "due_at " + err}), 400
        if start and due and day_index(due) < day_index(start):
            return jsonify({"error": "due_at is before start_at"}), 400
        d.execute(
            "UPDATE items SET start_at = %s, due_at = %s WHERE id = %s", (start, due, iid)
        )
    if "depends_on" in body:
        dep = body["depends_on"]
        if dep in (None, "", 0):
            d.execute("UPDATE items SET depends_on = NULL WHERE id = %s", (iid,))
        else:
            try:
                dep = int(dep)
            except (TypeError, ValueError):
                return jsonify({"error": "depends_on must be an item id or null"}), 400
            other = d.execute("SELECT * FROM items WHERE id = %s", (dep,)).fetchone()
            if other is None or other["project_id"] != row["project_id"]:
                return jsonify({"error": "depends_on must be an item in the same project"}), 400
            if dep == iid or depends_cycles(d, iid, dep):
                return jsonify({"error": "depends_on would create a cycle"}), 400
            d.execute("UPDATE items SET depends_on = %s WHERE id = %s", (dep, iid))

    d.commit()
    row = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    return jsonify(item_json(row, item_tag_ids(d, iid)))


# =====================================================================
# TAGS — the vocabulary is per project; item_tags applies it to items.
# =====================================================================
@app.get("/api/projects/<int:pid>/tags")
def list_tags(pid):
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(project_tags(d, pid))


@app.post("/api/projects/<int:pid>/tags")
def create_tag(pid):
    body = request.json or {}
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE id = %s", (pid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    name, err = parse_tag_name(body.get("name"))
    if err:
        return jsonify({"error": err}), 400
    color, err = parse_color(body.get("color"))
    if err:
        return jsonify({"error": err}), 400
    # Report the collision *with the existing tag*, so a client that raced (or that
    # typed a name it already owns) can just apply that one instead of failing.
    dupe = d.execute(
        "SELECT * FROM tags WHERE project_id = %s AND name = %s", (pid, name)
    ).fetchone()
    if dupe is not None:
        return jsonify({"error": "tag already exists", "tag": tag_json(dupe)}), 409
    pos = d.execute(
        "SELECT COALESCE(MAX(pos), -1) + 1 AS pos FROM tags WHERE project_id = %s", (pid,)
    ).fetchone()["pos"]
    row = d.execute(
        "INSERT INTO tags (project_id, name, color, pos) VALUES (%s, %s, %s, %s) RETURNING *",
        (pid, name, color, pos),
    ).fetchone()
    d.commit()
    return jsonify(tag_json(row)), 201


@app.patch("/api/tags/<int:tid>")
def update_tag(tid):
    body = request.json or {}
    d = db()
    row = d.execute("SELECT * FROM tags WHERE id = %s", (tid,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    if "name" in body:
        name, err = parse_tag_name(body["name"])
        if err:
            return jsonify({"error": err}), 400
        dupe = d.execute(
            "SELECT id FROM tags WHERE project_id = %s AND name = %s AND id <> %s",
            (row["project_id"], name, tid),
        ).fetchone()
        if dupe is not None:
            return jsonify({"error": "another tag in this project already has that name"}), 409
        d.execute("UPDATE tags SET name = %s WHERE id = %s", (name, tid))
    if "color" in body:
        color, err = parse_color(body["color"], row["color"])
        if err:
            return jsonify({"error": err}), 400
        d.execute("UPDATE tags SET color = %s WHERE id = %s", (color, tid))
    d.commit()
    row = d.execute("SELECT * FROM tags WHERE id = %s", (tid,)).fetchone()
    n = d.execute("SELECT COUNT(*) AS n FROM item_tags WHERE tag_id = %s", (tid,)).fetchone()["n"]
    return jsonify(tag_json(row, n))


@app.delete("/api/tags/<int:tid>")
def delete_tag(tid):
    # Deleting the tag unapplies it from every item (item_tags cascades). The
    # items themselves are untouched.
    d = db()
    cur = d.execute("DELETE FROM tags WHERE id = %s", (tid,))
    d.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return "", 204


def usable_tags(d, iid, ids):
    # Resolve tag ids for an item, refusing any tag that belongs to a different
    # project. Returns (item_row, ids, error).
    item = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    if item is None:
        return None, None, ("not found", 404)
    out = []
    for t in ids:
        try:
            t = int(t)
        except (TypeError, ValueError):
            return None, None, ("tag ids must be integers", 400)
        row = d.execute("SELECT project_id FROM tags WHERE id = %s", (t,)).fetchone()
        if row is None:
            return None, None, (f"tag {t} does not exist", 400)
        if row["project_id"] != item["project_id"]:
            return None, None, (f"tag {t} belongs to another project", 400)
        out.append(t)
    return item, out, None


@app.put("/api/items/<int:iid>/tags")
def set_item_tags(iid):
    # Replace the item's whole tag set — this is "change the tags on this item".
    body = request.json or {}
    ids = body.get("tags", body.get("tag_ids"))
    if not isinstance(ids, list):
        return jsonify({"error": "tags must be a list of tag ids"}), 400
    d = db()
    _item, ids, err = usable_tags(d, iid, ids)
    if err:
        return jsonify({"error": err[0]}), err[1]
    d.execute("DELETE FROM item_tags WHERE item_id = %s", (iid,))
    for t in set(ids):
        d.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (%s, %s)", (iid, t))
    d.commit()
    row = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    return jsonify(item_json(row, item_tag_ids(d, iid)))


@app.post("/api/items/<int:iid>/tags")
def apply_item_tag(iid):
    # Apply one tag. Idempotent: applying a tag the item already wears is a no-op,
    # not a 409, so a double-click cannot fail.
    body = request.json or {}
    d = db()
    _item, ids, err = usable_tags(d, iid, [body.get("tag_id")])
    if err:
        return jsonify({"error": err[0]}), err[1]
    d.execute(
        "INSERT INTO item_tags (item_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (iid, ids[0]),
    )
    d.commit()
    row = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    return jsonify(item_json(row, item_tag_ids(d, iid)))


@app.delete("/api/items/<int:iid>/tags/<int:tid>")
def remove_item_tag(iid, tid):
    d = db()
    if d.execute("SELECT 1 FROM items WHERE id = %s", (iid,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    d.execute("DELETE FROM item_tags WHERE item_id = %s AND tag_id = %s", (iid, tid))
    d.commit()
    row = d.execute("SELECT * FROM items WHERE id = %s", (iid,)).fetchone()
    return jsonify(item_json(row, item_tag_ids(d, iid)))


@app.get("/api/projects/<int:pid>/gantt")
def project_gantt(pid):
    # The Gantt chart is a *derived view* of the item table, not a stored entity:
    # a bar IS an item. So the chart needs no create/update/delete of its own —
    # POST/PATCH/DELETE on items already are those operations, and the chart can
    # never drift out of sync with the todo list. This endpoint just resolves the
    # rows into pixel-ready bars so a renderer doesn't repeat the date math.
    d = db()
    p = d.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone()
    if p is None:
        return jsonify({"error": "not found"}), 404
    items = d.execute(
        "SELECT * FROM items WHERE project_id = %s ORDER BY pos, id", (pid,)
    ).fetchall()
    proj = project_json(p, items)
    span = proj["span"]
    origin = day_index(span["start"]) if span and span["start"] else None

    bars = []
    for i in proj["items"]:
        # Unscheduled items are reported, not dropped — a chart that silently
        # omits rows reads as "nothing left to plan".
        scheduled = bool(i["start_at"] and i["due_at"])
        bar = {
            "id": i["id"],
            "text": i["text"],
            "scheduled": scheduled,
            "start_at": i["start_at"],
            "due_at": i["due_at"],
            "completion": i["completion"],
            "done": i["done"],
            "overdue": i["overdue"],
            "depends_on": i["depends_on"],
            "offset_days": None,
            "length_days": None,
        }
        if scheduled and origin is not None:
            bar["offset_days"] = day_index(i["start_at"]) - origin
            # Inclusive of both endpoints: a task that starts and ends on the same
            # day is one day long, not zero.
            bar["length_days"] = day_index(i["due_at"]) - day_index(i["start_at"]) + 1
        bars.append(bar)

    return jsonify({
        "project_id": p["id"],
        "title": p["title"],
        "span": span,
        "unscheduled": sum(1 for b in bars if not b["scheduled"]),
        "bars": bars,
    })


@app.delete("/api/items/<int:iid>")
def delete_item(iid):
    d = db()
    cur = d.execute("DELETE FROM items WHERE id = %s", (iid,))
    d.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return "", 204


init_db()

if __name__ == "__main__":
    # host="0.0.0.0" so the server is reachable from the Windows browser across
    # WSL2's network boundary (localhost forwarding is unreliable). Local dev only
    # — in prod Render runs this under gunicorn (see render.yaml), not app.run.
    app.run(host="0.0.0.0", port=5000, debug=True)
