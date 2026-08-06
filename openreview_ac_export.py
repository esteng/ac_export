#!/usr/bin/env python3
"""
Export your OpenReview area-chair assignments to a self-contained static site.

    python openreview_ac_export.py --keys .keys --out export

What it does
  1. Reads OpenReview credentials from a .keys file.
  2. Finds every submission you are assigned to as an Area Chair for the venue
     (default EMNLP/2026/Conference) and writes the id list to
     export/assignments.json + export/assignments.txt
  3. For each paper, pulls the venue-side notes AND the linked ARR forum
     discussion, and writes everything verbatim to export/papers/<number>/data.json
  4. Writes two static HTML viewers into each paper directory —
     metareview.html (the venue AC metareview page) and forum.html (the review
     discussion) — plus export/index.html listing all papers.

The viewers are plain HTML/CSS/JS with no build step, no server and no network
access at run time: open any of them straight from disk. Styling reuses
OpenReview's own stylesheets, downloaded into export/assets/ (use --no-css to
skip and fall back to a bundled look-alike).

Only requires the `requests` package.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

OR_BASE = "https://api2.openreview.net"
OR_WEB = "https://openreview.net"
DEFAULT_VENUE = "EMNLP/2026/Conference"

HERE = os.path.dirname(os.path.abspath(__file__))


# ─── credentials ────────────────────────────────────────────────────────────────
def read_keys(path):
    """Parse a .keys file of KEY=VALUE lines. Returns (username, password)."""
    if not os.path.exists(path):
        sys.exit(
            f"No keys file at {path}.\n"
            "Create one with your OpenReview login:\n\n"
            "  OPENREVIEW_USERNAME=you@example.com\n"
            "  OPENREVIEW_PASSWORD=your-password\n"
        )
    keys = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        keys[k.strip().upper()] = v.strip().strip("'\"")

    user = next((keys[k] for k in ("OPENREVIEW_USERNAME", "OPENREVIEW_EMAIL", "USERNAME", "EMAIL") if k in keys), None)
    pwd = next((keys[k] for k in ("OPENREVIEW_PASSWORD", "PASSWORD") if k in keys), None)
    if not user or not pwd:
        sys.exit(
            f"{path} must define OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD "
            f"(found: {', '.join(sorted(keys)) or 'nothing'})."
        )
    return user, pwd


# ─── API client ─────────────────────────────────────────────────────────────────
class OpenReview:
    """Thin API client that stays under OpenReview's rate limit."""

    MIN_INTERVAL = 0.35  # seconds between requests

    def __init__(self, username, password, verbose=True):
        self.session = requests.Session()
        self.verbose = verbose
        self._last = 0.0
        r = self.session.post(f"{OR_BASE}/login", json={"id": username, "password": password})
        if r.status_code != 200 or "token" not in r.json():
            sys.exit(f"OpenReview login failed for {username}: {r.text[:300]}")
        body = r.json()
        self.token = body["token"]
        self.profile_id = body.get("user", {}).get("profile", {}).get("id")
        if not self.profile_id:
            sys.exit("Logged in but could not read your profile id from the response.")

    def get(self, path, **params):
        for attempt in range(6):
            gap = time.time() - self._last
            if gap < self.MIN_INTERVAL:
                time.sleep(self.MIN_INTERVAL - gap)
            self._last = time.time()
            r = self.session.get(
                f"{OR_BASE}/{path}", params=params, headers={"Authorization": "Bearer " + self.token}
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:  # rate limited — back off and retry
                wait = 10 * (attempt + 1)
                if self.verbose:
                    print(f"    rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:300]}")
        raise RuntimeError(f"{path}: still rate limited after {attempt + 1} attempts")

    def get_all(self, path, key, **params):
        """Paginated fetch — OpenReview caps a single response at 1000 items."""
        out, offset, limit = [], 0, 1000
        while True:
            batch = self.get(path, limit=limit, offset=offset, **params).get(key, [])
            out.extend(batch)
            if len(batch) < limit:
                return out
            offset += limit


# ─── OpenReview helpers ─────────────────────────────────────────────────────────
def val(field):
    """Handle both v1 (plain value) and v2 ({"value": ...}) note fields."""
    if isinstance(field, dict):
        field = field.get("value")
    return field


FORUM_ID_RE = re.compile(r"forum\?id=([A-Za-z0-9_-]+)")


def forum_id_from_link(link):
    m = FORUM_ID_RE.search(link or "")
    return m.group(1) if m else None


def get_assignments(api, venue, role="Area_Chairs"):
    """Submission note ids this user is assigned to, newest assignment wins."""
    inv = f"{venue}/{role}/-/Assignment"
    try:
        edges = api.get_all("edges", "edges", invitation=inv, tail=api.profile_id)
    except RuntimeError as e:
        print(f"  assignment edges unavailable ({e}); falling back to group membership")
        edges = []
    if edges:
        return [e["head"] for e in edges]

    # Fallback: per-submission role groups (slower, and rate limited more often).
    groups = api.get_all("groups", "groups", member=api.profile_id, prefix=f"{venue}/Submission.*")
    pat = re.compile(re.escape(venue) + r"/Submission(\d+)/" + role[:-1] + r"_\w+$")
    numbers = sorted({int(m.group(1)) for g in groups if (m := pat.match(g["id"]))})
    if not numbers:
        return []
    ids = []
    for n in numbers:
        notes = api.get("notes", invitation=f"{venue}/-/Submission", number=n).get("notes", [])
        if notes:
            ids.append(notes[0]["id"])
    return ids


def load_submissions(api, venue, wanted_ids):
    """Map submission note id -> note, for the assigned ids."""
    wanted = set(wanted_ids)
    notes = api.get_all("notes", "notes", invitation=f"{venue}/-/Submission")
    found = {n["id"]: n for n in notes if n["id"] in wanted}
    for nid in wanted - set(found):  # not in the venue listing — fetch directly
        got = api.get("notes", id=nid).get("notes", [])
        if got:
            found[nid] = got[0]
    return found


# ─── export ─────────────────────────────────────────────────────────────────────
def export_paper(api, venue, submission, out_root):
    number = submission.get("number")
    title = val(submission.get("content", {}).get("title")) or submission["id"]
    venue_forum = submission["id"]

    venue_notes = api.get_all("notes", "notes", forum=venue_forum)

    paper_link = val(submission.get("content", {}).get("paper_link")) or ""
    arr_forum = forum_id_from_link(paper_link)
    forum_notes = []
    if arr_forum and arr_forum != venue_forum:
        forum_notes = api.get_all("notes", "notes", forum=arr_forum)

    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "venue": venue,
        "number": number,
        "title": title,
        "venue_forum_id": venue_forum,
        "venue_forum_url": f"{OR_WEB}/forum?id={venue_forum}",
        "paper_link": paper_link,
        "review_forum_id": arr_forum,
        "review_forum_url": f"{OR_WEB}/forum?id={arr_forum}" if arr_forum else None,
        "venue_notes": venue_notes,
        "forum_notes": forum_notes,
    }

    pdir = os.path.join(out_root, "papers", str(number))
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "data.json"), "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    # Browsers block fetch() on file:// URLs, so the viewers read this instead.
    with open(os.path.join(pdir, "data.js"), "w") as f:
        f.write("window.PAPER_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";\n")

    for name, fn in (("metareview.html", "renderMetareview"), ("forum.html", "renderForum")):
        with open(os.path.join(pdir, name), "w") as f:
            f.write(page_html(title, fn, depth=2))

    return data


def page_html(title, render_fn, depth):
    up = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{up}assets/openreview.css">
<link rel="stylesheet" href="{up}assets/viewer.css">
</head>
<body>
<div id="app"></div>
<script src="data.js"></script>
<script src="{up}assets/viewer.js"></script>
<script>{render_fn}(window.PAPER_DATA, document.getElementById('app'));</script>
</body>
</html>
"""


def esc(s):
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def index_html(papers, venue):
    rows = ""
    for p in papers:
        n = p["number"]
        rows += f"""<li class="note">
  <h4><a href="papers/{n}/metareview.html">#{n} &nbsp;{esc(p['title'])}</a></h4>
  <div class="note-meta-info">
    <a href="papers/{n}/metareview.html">metareview</a> &nbsp;&middot;&nbsp;
    <a href="papers/{n}/forum.html">forum ({p['n_notes']} notes)</a> &nbsp;&middot;&nbsp;
    <a href="papers/{n}/data.json">data.json</a> &nbsp;&middot;&nbsp;
    <a href="{p['venue_forum_url']}" target="_blank">OpenReview</a>
  </div>
</li>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(venue)} — AC assignments</title>
<link rel="stylesheet" href="assets/openreview.css">
<link rel="stylesheet" href="assets/viewer.css">
</head>
<body>
<div class="container">
  <div class="page-head">
    <h1>{esc(venue)}</h1>
    <div class="page-sub">{len(papers)} area-chair assignments</div>
  </div>
  <ul class="submissions-list">{rows}</ul>
</div>
</body>
</html>
"""


# ─── assets ─────────────────────────────────────────────────────────────────────
CSS_LINK_RE = re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+\.css)"')


def fetch_openreview_css():
    """Concatenate OpenReview's own stylesheets. Returns None if unavailable."""
    try:
        home = requests.get(OR_WEB, timeout=20)
        home.raise_for_status()
        hrefs = CSS_LINK_RE.findall(home.text)
        if not hrefs:
            return None
        chunks = []
        for href in hrefs:
            url = href if href.startswith("http") else OR_WEB + href
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                chunks.append(f"/* {url} */\n{r.text}")
        return "\n".join(chunks) if chunks else None
    except requests.RequestException:
        return None


def write_assets(out_root, use_remote_css=True):
    adir = os.path.join(out_root, "assets")
    os.makedirs(adir, exist_ok=True)

    css = fetch_openreview_css() if use_remote_css else None
    if css:
        print(f"  fetched OpenReview stylesheets ({len(css) // 1024} KB)")
    else:
        if use_remote_css:
            print("  could not fetch OpenReview stylesheets — using the bundled look-alike")
        css = "/* OpenReview stylesheets unavailable; see viewer.css */\n"
    with open(os.path.join(adir, "openreview.css"), "w") as f:
        f.write(css)

    with open(os.path.join(adir, "viewer.css"), "w") as f:
        f.write(VIEWER_CSS if css.strip().startswith("/* http") else FALLBACK_CSS + VIEWER_CSS)
    with open(os.path.join(adir, "viewer.js"), "w") as f:
        f.write(VIEWER_JS)


# ─── styles ─────────────────────────────────────────────────────────────────────
# Loaded on top of OpenReview's own CSS: page chrome their stylesheets do not
# cover, since we are not rendering inside their app shell.
VIEWER_CSS = """
body { background: #fff; color: #2c3a4a; margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.container { max-width: 1080px; margin: 0 auto; padding: 0 1rem 4rem; }

.topbar { background: #2c3a4a; margin-bottom: 1.5rem; }
.topbar .inner { max-width: 1080px; margin: 0 auto; padding: .6rem 1rem;
  display: flex; align-items: center; gap: 1.25rem; }
.topbar a { color: #d3dbe3; text-decoration: none; font-size: .8125rem; }
.topbar a:hover, .topbar a.active { color: #fff; }
.topbar a.active { font-weight: 700; box-shadow: inset 0 -2px 0 #3e6775; }
.topbar .venue { color: #8d9aa8; font-size: .75rem; text-transform: uppercase;
  letter-spacing: .06em; margin-right: auto; }

.page-head { border-bottom: 1px solid #ddd; padding: 1.5rem 0 .75rem; margin-bottom: 1.25rem; }
.page-head h1 { font-size: 1.75rem; font-weight: 700; margin: 0 0 .25rem; letter-spacing: -.5px; }
.page-sub { font-size: .8125rem; color: #616161; }
.page-sub a { color: #3e6775; }

.section-head { font-size: 1.125rem; font-weight: 700; color: #2c3a4a;
  border-bottom: 1px solid #ddd; padding-bottom: .35rem; margin: 2rem 0 .85rem; }

.panel-box { border: 1px solid #ddd; border-radius: 2px; padding: .75rem 1rem; margin-bottom: 1rem; }
.panel-box.empty { color: #616161; font-style: italic; }
.panel-box .panel-head { font-size: .75rem; color: #616161; margin-bottom: .5rem; }

table.scores { width: 100%; border-collapse: collapse; font-size: .8125rem; margin-bottom: 1rem; }
table.scores th { text-align: left; font-weight: 700; color: #616161; font-size: .6875rem;
  text-transform: uppercase; letter-spacing: .04em; border-bottom: 2px solid #ddd; padding: .35rem .5rem; }
table.scores td { border-top: 1px solid #eee; padding: .4rem .5rem; vertical-align: middle; }
table.scores tr.flagged td { background: #fdf3f2; }
table.scores td.num { font-weight: 700; text-align: center; width: 5.5rem; }
table.scores a { color: #3e6775; }

.issue-tag { background: #8c1b13; color: #fff; font-size: .625rem; font-weight: 700;
  text-transform: uppercase; padding: 1px 5px; border-radius: 2px; margin-left: .4rem;
  letter-spacing: .04em; white-space: nowrap; }

.controls { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap;
  font-size: .8125rem; color: #616161; margin-bottom: 1rem; }
.controls select, .controls button { font-size: .8125rem; padding: .2rem .45rem;
  border: 1px solid #ccc; border-radius: 2px; background: #fff; color: #2c3a4a; cursor: pointer; }
.controls .count { margin-left: auto; }

main.forum .note { margin-bottom: .5rem; }
main.forum .note .subheading .invitation { font-weight: 700; color: #8c1b13; }
main.forum .note .subheading .signatures { color: #616161; }
main.forum .note .subheading .date { color: #9aa4ae; }
main.forum .note.is-review { border-left: 3px solid #3e6775; }
main.forum .note.is-metareview { border-left: 3px solid #4b7a4b; }
main.forum .note.is-issue { border-left: 3px solid #8c1b13; }
main.forum .note .toggle { background: none; border: none; color: #3e6775; cursor: pointer;
  font-size: .75rem; padding: 0; }
main.forum .note.collapsed .note-content-container { display: none; }
main.forum .note-replies { margin-top: .75rem; padding-left: 2.5rem; }

details.more-fields { margin-top: .5rem; }
details.more-fields > summary { cursor: pointer; font-size: .75rem; color: #3e6775;
  list-style: none; display: inline-block; }
details.more-fields > summary::-webkit-details-marker { display: none; }
details.more-fields > summary::before { content: "\\25b8 "; }
details.more-fields[open] > summary::before { content: "\\25be "; }

ul.note-content { list-style: none; padding-left: 0; }
ul.note-content > li { padding: .125rem 0; }
.note-content strong { font-weight: 700; color: #8c1b13; padding-right: .25rem; }
.note-content .note-content-value.markdown-rendered table { border-collapse: collapse; }
.note-content .note-content-value.markdown-rendered th,
.note-content .note-content-value.markdown-rendered td { border: 1px solid #ddd; padding: .25rem .4rem; }

ul.submissions-list { list-style: none; padding-left: 0; }
ul.submissions-list .note { padding: .5rem 0; border-bottom: 1px solid #eee; }
ul.submissions-list h4 { font-size: 1.0625rem; margin: 0 0 .15rem; font-weight: 700; }
ul.submissions-list a { color: #3e6775; text-decoration: none; }
ul.submissions-list a:hover { text-decoration: underline; }
.note-meta-info { font-size: .75rem; color: #616161; }
"""

# Used only when OpenReview's stylesheets could not be downloaded. Mirrors the
# parts of their forum styling the viewers rely on.
FALLBACK_CSS = """
* { box-sizing: border-box; }
body { font-size: 16px; line-height: 1.5; }
h1,h2,h3,h4,h5 { margin: 0 0 .5rem; line-height: 1.25; }
a { color: #3e6775; }
main.forum .forum-note { padding-bottom: 1rem; border-bottom: 1px solid #ddd; margin-bottom: 1.25rem; }
main.forum .forum-note .forum-title h2 { font-size: 1.75rem; font-weight: 700; letter-spacing: -.5px; margin: 0; }
main.forum .forum-note .forum-authors h3 { font-size: 1.25rem; font-style: italic; margin: .25rem 0; }
main.forum .forum-note .forum-meta { font-size: .75rem; color: #616161; }
main.forum .forum-note .forum-meta .item { padding-right: 1rem; }
main.forum .note { position: relative; padding: .5rem; border: 1px solid #eee; border-radius: 2px; }
main.forum .note.depth-odd { background-color: #f7f6f4; }
main.forum .note.depth-even { background-color: #fffdfa; }
main.forum .note h4 { color: #2c3a4a; font-size: 1rem; line-height: 1.25rem; margin: 0 0 .125rem; }
main.forum .note .heading { display: flex; justify-content: space-between; }
main.forum .note .subheading { margin-bottom: .25rem; font-size: .75rem; }
main.forum .note .subheading > span { padding-right: .5rem; }
.note-content { font-size: .8125rem; line-height: 1.3rem; }
.note-content .note-content-value { white-space: pre-wrap; overflow-wrap: break-word; }
.note-content .note-content-value.markdown-rendered { white-space: normal; }
.note-content .note-content-value.markdown-rendered p { margin: 0 0 .5rem; }
.note-content .note-content-value.markdown-rendered ol,
.note-content .note-content-value.markdown-rendered ul { padding-left: 1.5rem; margin-bottom: .5rem; }
.note-content .note-content-value.markdown-rendered h1 { font-size: 1.375rem; font-weight: 700; }
.note-content .note-content-value.markdown-rendered h2 { font-size: 1.25rem; }
.note-content .note-content-value.markdown-rendered h3 { font-size: 1rem; }
.note-content .note-content-value.markdown-rendered code { color: #8c1b13; font-family: Menlo, monospace; }
.note-content .note-content-value.markdown-rendered pre { background: #f5f5f5; padding: .25rem .5rem; overflow-x: auto; }
.note-content .note-content-value.markdown-rendered blockquote { border-left: 3px solid #ccc;
  padding: .25rem .5rem; margin: 0 0 .5rem; color: #555; }
"""

VIEWER_JS = r"""/* Static viewer for exported OpenReview data. No dependencies. */
'use strict';

/* ── helpers ──────────────────────────────────────────────────────────────── */
function val(field) {
  if (field === null || field === undefined) return '';
  if (typeof field === 'object' && !Array.isArray(field)) {
    if ('value' in field) return val(field.value);
    return '';
  }
  if (Array.isArray(field)) return field.map(val).join(', ');
  return String(field);
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function prettyField(name) {
  return name.replace(/_/g, ' ').replace(/^\s*\w/, function (c) { return c.toUpperCase(); });
}

function shortInvitation(note) {
  var invs = note.invitations || (note.invitation ? [note.invitation] : []);
  for (var i = 0; i < invs.length; i++) {
    var tail = invs[i].split('/-/').pop();
    if (tail && tail !== 'Edit') return tail.replace(/_/g, ' ');
  }
  return 'Note';
}

function invitationPath(note) {
  var invs = note.invitations || (note.invitation ? [note.invitation] : []);
  return invs.join(' ');
}

/* "…/Submission7098/Reviewer_LQ4U" -> "Reviewer LQ4U" */
function prettySignature(sig) {
  var last = String(sig).split('/').pop();
  return last.replace(/_/g, ' ');
}

function signatures(note) {
  return (note.signatures || []).map(prettySignature).join(', ');
}

function fmtDate(ms) {
  if (!ms) return '';
  var d = new Date(ms);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function noteKind(note) {
  var p = invitationPath(note);
  if (/Review_Issue_Report/.test(p)) return 'issue';
  if (/Meta_?Review/i.test(p)) return 'metareview';
  if (/Official_Review/.test(p)) return 'review';
  if (/\/-\/(Submission|Post_Submission)/.test(p)) return 'submission';
  if (/Decision/.test(p)) return 'decision';
  return 'comment';
}

/* ── minimal markdown ─────────────────────────────────────────────────────── */
function inlineMd(text) {
  // Split on code spans so emphasis/link rules never touch code, and no
  // placeholder token can collide with real review text.
  return text.split(/(`[^`\n]+`)/).map(function (part, i) {
    if (i % 2) return '<code>' + part.slice(1, -1) + '</code>';
    part = part.replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, '<em>[image: $1]</em>');
    part = part.replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    part = part.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    part = part.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    part = part.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    part = part.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, '$1<em>$2</em>');
    part = part.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, '$1<em>$2</em>');
    return part;
  }).join('');
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(function (c) {
    return inlineMd(c.trim());
  });
}

function renderMarkdown(src) {
  if (src === null || src === undefined || src === '') return '';
  var lines = esc(String(src)).replace(/\r\n?/g, '\n').split('\n');
  var out = [], i = 0;

  function flushParagraph(buf) {
    if (buf.length) out.push('<p>' + inlineMd(buf.join('\n')).replace(/\n/g, '<br>') + '</p>');
    buf.length = 0;
  }

  var para = [];
  while (i < lines.length) {
    var line = lines[i];

    if (/^\s*```/.test(line)) {                                   // fenced code
      flushParagraph(para);
      var code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
      i++;
      out.push('<pre><code>' + code.join('\n') + '</code></pre>');
      continue;
    }
    if (/^\s*$/.test(line)) { flushParagraph(para); i++; continue; }
    if (/^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {           // hr
      flushParagraph(para); out.push('<hr>'); i++; continue;
    }
    var h = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);                // heading
    if (h) {
      flushParagraph(para);
      var lvl = Math.min(h[1].length, 6);
      out.push('<h' + lvl + '>' + inlineMd(h[2].trim()) + '</h' + lvl + '>');
      i++; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
        /^\s*\|?[\s:-]*-[-\s:|]*\|?\s*$/.test(lines[i + 1])) {     // table
      flushParagraph(para);
      var head = splitRow(line);
      i += 2;
      var body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { body.push(splitRow(lines[i])); i++; }
      var t = '<table><thead><tr>' + head.map(function (c) { return '<th>' + c + '</th>'; }).join('') +
              '</tr></thead><tbody>';
      body.forEach(function (row) {
        t += '<tr>' + row.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>';
      });
      out.push(t + '</tbody></table>');
      continue;
    }
    if (/^\s*&gt;\s?/.test(line)) {                                // blockquote
      flushParagraph(para);
      var quote = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*&gt;\s?/, '')); i++;
      }
      out.push('<blockquote>' + renderMarkdown_pre(quote.join('\n')) + '</blockquote>');
      continue;
    }
    var bullet = line.match(/^\s*([-*•]|\d+[.)])\s+/);             // list
    if (bullet) {
      flushParagraph(para);
      var ordered = /\d/.test(bullet[1]);
      var items = [];
      while (i < lines.length) {
        var m = lines[i].match(/^\s*([-*•]|\d+[.)])\s+(.*)$/);
        if (m) {
          if (/\d/.test(m[1]) !== ordered) break;
          items.push([m[2]]);
        } else if (/^\s+\S/.test(lines[i]) && items.length) {
          items[items.length - 1].push(lines[i].trim());           // continuation
        } else if (/^\s*$/.test(lines[i]) && items.length &&
                   i + 1 < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i + 1])) {
          // blank line inside a list
        } else break;
        i++;
      }
      var tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.map(function (parts) {
        return '<li>' + inlineMd(parts.join(' ')) + '</li>';
      }).join('') + '</' + tag + '>');
      continue;
    }
    para.push(line);
    i++;
  }
  flushParagraph(para);
  return out.join('\n');
}

/* blockquote bodies are already escaped, so re-render without double escaping */
function renderMarkdown_pre(escaped) {
  return '<p>' + inlineMd(escaped).replace(/\n/g, '<br>') + '</p>';
}

/* ── note content ─────────────────────────────────────────────────────────── */
var HIDDEN_FIELDS = { title: 1, venue: 1, venueid: 1, pdf: 1, html: 1 };

/* Submission fields worth showing up front; the rest (checklist answers and
   other boilerplate) go behind a toggle so the discussion stays reachable. */
var PRIMARY_SUBMISSION_FIELDS = [
  'TLDR', 'abstract', 'keywords', 'paper_type', 'track',
  'research_area', 'research_area_keywords', 'primary_area', 'subject_areas'
];

function contentList(note, opts) {
  opts = opts || {};
  var content = note.content || {};
  var items = [];
  Object.keys(content).forEach(function (key) {
    if (HIDDEN_FIELDS[key] && !opts.keepAll) return;
    if (opts.only && opts.only.indexOf(key) === -1) return;
    if (opts.exclude && opts.exclude.indexOf(key) !== -1) return;
    var v = val(content[key]);
    if (v === '' || v === null) return;
    items.push('<li><strong>' + esc(prettyField(key)) + ':</strong> ' +
      '<span class="note-content-value markdown-rendered">' + renderMarkdown(v) + '</span></li>');
  });
  if (!items.length) return '';
  return '<ul class="note-content">' + items.join('') + '</ul>';
}

function submissionContent(note) {
  var primary = contentList(note, { only: PRIMARY_SUBMISSION_FIELDS });
  var rest = contentList(note, { exclude: PRIMARY_SUBMISSION_FIELDS });
  if (!rest) return primary;
  return primary +
    '<details class="more-fields"><summary>all submission fields</summary>' + rest + '</details>';
}

/* ── forum page ───────────────────────────────────────────────────────────── */
function buildTree(notes, rootId) {
  var children = {};
  notes.forEach(function (n) {
    if (n.id === rootId) return;
    var parent = n.replyto || rootId;
    (children[parent] = children[parent] || []).push(n);
  });
  Object.keys(children).forEach(function (k) {
    children[k].sort(function (a, b) { return (a.cdate || 0) - (b.cdate || 0); });
  });
  // Notes whose parent is missing (e.g. hidden from us) attach to the root.
  var known = {};
  notes.forEach(function (n) { known[n.id] = 1; });
  Object.keys(children).forEach(function (k) {
    if (k !== rootId && !known[k]) {
      children[rootId] = (children[rootId] || []).concat(children[k]);
      delete children[k];
    }
  });
  return children;
}

function renderNote(note, children, depth) {
  var kind = noteKind(note);
  var kids = children[note.id] || [];
  var title = val((note.content || {}).title) || shortInvitation(note);
  var html = '<div class="note depth-' + (depth % 2 ? 'odd' : 'even') + ' is-' + kind +
    '" id="note-' + esc(note.id) + '" data-kind="' + kind + '">' +
    '<div class="heading"><h4>' + esc(title) + '</h4>' +
    '<button class="toggle" onclick="toggleNote(this)">collapse</button></div>' +
    '<div class="subheading">' +
      '<span class="invitation">' + esc(shortInvitation(note)) + '</span>' +
      '<span class="signatures">' + esc(signatures(note)) + '</span>' +
      '<span class="date">' + esc(fmtDate(note.cdate || note.tcdate)) + '</span>' +
    '</div>' +
    '<div class="note-content-container">' + contentList(note) + '</div>';
  if (kids.length) {
    html += '<div class="note-replies">' +
      kids.map(function (k) { return renderNote(k, children, depth + 1); }).join('') + '</div>';
  }
  return html + '</div>';
}

function toggleNote(btn) {
  var note = btn.closest('.note');
  var collapsed = note.classList.toggle('collapsed');
  btn.textContent = collapsed ? 'expand' : 'collapse';
}

function setAllCollapsed(collapsed) {
  document.querySelectorAll('main.forum .note').forEach(function (n) {
    n.classList.toggle('collapsed', collapsed);
  });
  document.querySelectorAll('main.forum .note .toggle').forEach(function (b) {
    b.textContent = collapsed ? 'expand' : 'collapse';
  });
}

/* Keep a note visible if it matches, or if it contains a match further down the
   thread — issue reports and rebuttals hang off the review they answer. */
function filterNotes(kind) {
  document.querySelectorAll('main.forum .note').forEach(function (n) {
    var match = kind === 'all' || n.dataset.kind === kind ||
      n.querySelector('.note[data-kind="' + kind + '"]');
    n.style.display = match ? '' : 'none';
  });
}

function renderForum(data, root) {
  var notes = data.forum_notes && data.forum_notes.length ? data.forum_notes : data.venue_notes;
  var rootId = (data.forum_notes && data.forum_notes.length) ? data.review_forum_id : data.venue_forum_id;
  var submission = notes.filter(function (n) { return n.id === rootId; })[0] ||
                   notes.filter(function (n) { return noteKind(n) === 'submission'; })[0];
  var children = buildTree(notes, rootId);
  var top = children[rootId] || [];

  var counts = {};
  notes.forEach(function (n) {
    if (n.id === rootId) return;
    counts[noteKind(n)] = (counts[noteKind(n)] || 0) + 1;
  });
  var options = ['<option value="all">all replies (' + (notes.length - 1) + ')</option>'];
  Object.keys(counts).sort().forEach(function (k) {
    options.push('<option value="' + k + '">' + k + ' (' + counts[k] + ')</option>');
  });

  var c = submission ? (submission.content || {}) : {};
  var meta = [];
  if (data.number) meta.push('Submission ' + data.number);
  if (val(c.venue)) meta.push(esc(val(c.venue)));
  if (submission) meta.push(fmtDate(submission.cdate || submission.tcdate));
  if (data.review_forum_url) {
    meta.push('<a href="' + esc(data.review_forum_url) + '" target="_blank">open on OpenReview</a>');
  }

  root.innerHTML = topbar(data, 'forum') +
    '<main class="forum"><div class="container">' +
      '<div class="forum-note">' +
        '<div class="forum-title"><h2>' + esc(data.title) + '</h2></div>' +
        '<div class="forum-authors"><h3>' +
          esc(val(c.authors) || 'Authors anonymous') + '</h3></div>' +
        '<div class="forum-meta">' + meta.map(function (m) {
          return '<span class="item">' + m + '</span>';
        }).join('') + '</div>' +
        '<div style="clear:both"></div>' +
        (submission ? submissionContent(submission) : '') +
      '</div>' +
      '<div class="controls">' +
        '<label>Show <select onchange="filterNotes(this.value)">' + options.join('') + '</select></label>' +
        '<button onclick="setAllCollapsed(true)">collapse all</button>' +
        '<button onclick="setAllCollapsed(false)">expand all</button>' +
        '<span class="count">' + top.length + ' top-level replies</span>' +
      '</div>' +
      '<div class="forum-replies-container"><div id="forum-replies">' +
        (top.length
          ? top.map(function (n) { return renderNote(n, children, 1); }).join('')
          : '<p class="panel-box empty">No replies in this forum.</p>') +
      '</div></div>' +
    '</div></main>';
}

/* ── metareview page ──────────────────────────────────────────────────────── */
var SCORE_FIELDS = [
  ['overall_assessment', 'Overall'],
  ['confidence', 'Conf'],
  ['soundness', 'Sound'],
  ['excitement', 'Excite']
];

function firstNum(s) {
  var m = String(s).match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

function reviewRows(forumNotes) {
  var reviews = forumNotes.filter(function (n) { return noteKind(n) === 'review'; });
  var issues = forumNotes.filter(function (n) { return noteKind(n) === 'issue'; });

  reviews.sort(function (a, b) {
    var av = firstNum(val((a.content || {}).overall_assessment));
    var bv = firstNum(val((b.content || {}).overall_assessment));
    return (bv === null ? -1 : bv) - (av === null ? -1 : av);
  });

  return reviews.map(function (r) {
    var mine = issues.filter(function (iss) {
      if (iss.replyto === r.id) return true;
      var m = invitationPath(iss).match(/\/Official_Review(\d+)\//);
      return m && String(r.number) === m[1];
    });
    return { note: r, issues: mine };
  });
}

function scoreTable(rows) {
  if (!rows.length) return '<p class="panel-box empty">No reviews found.</p>';
  var head = '<tr><th>Reviewer</th>' + SCORE_FIELDS.map(function (f) {
    return '<th style="text-align:center">' + f[1] + '</th>';
  }).join('') + '<th>Review</th></tr>';
  var body = rows.map(function (row) {
    var c = row.note.content || {};
    var cells = SCORE_FIELDS.map(function (f) {
      var v = firstNum(val(c[f[0]]));
      return '<td class="num">' + (v === null ? '&ndash;' : v) + '</td>';
    }).join('');
    var codes = [];
    row.issues.forEach(function (iss) {
      Object.keys(iss.content || {}).forEach(function (k) {
        var m = k.match(/^I(\d+)/);
        if (m) codes.push('I' + m[1]);
      });
    });
    var flag = row.issues.length
      ? '<span class="issue-tag">issue report ' + esc(codes.join(', ')) + '</span>' : '';
    return '<tr class="' + (row.issues.length ? 'flagged' : '') + '">' +
      '<td>' + esc(signatures(row.note)) + flag + '</td>' + cells +
      '<td><a href="forum.html#note-' + esc(row.note.id) + '">read &rarr;</a></td></tr>';
  }).join('');
  return '<table class="scores"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
}

function notePanel(note, label) {
  if (!note) return '<p class="panel-box empty">' + esc(label) + ' has not been posted.</p>';
  return '<div class="panel-box">' +
    '<div class="panel-head">' + esc(shortInvitation(note)) + ' &middot; ' +
      esc(signatures(note)) + ' &middot; ' + esc(fmtDate(note.cdate || note.tcdate)) + '</div>' +
    contentList(note) + '</div>';
}

function renderMetareview(data, root) {
  var venueNotes = data.venue_notes || [];
  var forumNotes = data.forum_notes || [];

  var submission = venueNotes.filter(function (n) { return n.id === data.venue_forum_id; })[0];
  var venueMeta = venueNotes.filter(function (n) { return noteKind(n) === 'metareview'; })[0];
  var arrMeta = forumNotes.filter(function (n) { return noteKind(n) === 'metareview'; })[0];
  var c = submission ? (submission.content || {}) : {};

  var sub = [];
  if (data.number) sub.push('Submission ' + data.number);
  if (val(c.track)) sub.push(esc(val(c.track)));
  sub.push('<a href="' + esc(data.venue_forum_url) + '" target="_blank">venue forum</a>');
  if (data.review_forum_url) {
    sub.push('<a href="' + esc(data.review_forum_url) + '" target="_blank">review forum</a>');
  }

  var response = val(c.response_to_metareview);

  root.innerHTML = topbar(data, 'metareview') +
    '<div class="container">' +
      '<div class="page-head">' +
        '<h1>' + esc(data.title) + '</h1>' +
        '<div class="page-sub">' + sub.join(' &nbsp;&middot;&nbsp; ') + '</div>' +
      '</div>' +

      '<div class="section-head">Metareview</div>' +
      notePanel(venueMeta, 'The metareview') +

      (response
        ? '<div class="section-head">Author response to the metareview</div>' +
          '<div class="panel-box"><div class="note-content">' +
          '<span class="note-content-value markdown-rendered">' + renderMarkdown(response) +
          '</span></div></div>'
        : '') +

      '<div class="section-head">Reviews</div>' +
      scoreTable(reviewRows(forumNotes)) +

      '<div class="section-head">Review-forum metareview</div>' +
      notePanel(arrMeta, 'The review-forum metareview') +

      (submission
        ? '<div class="section-head">Submission</div>' +
          '<div class="panel-box">' + contentList(submission) + '</div>'
        : '') +
    '</div>';
}

function topbar(data, active) {
  function link(href, label, name) {
    return '<a href="' + href + '" class="' + (active === name ? 'active' : '') + '">' + label + '</a>';
  }
  return '<div class="topbar"><div class="inner">' +
    '<span class="venue">' + esc(data.venue || '') +
      (data.number ? ' &middot; #' + data.number : '') + '</span>' +
    link('metareview.html', 'Metareview', 'metareview') +
    link('forum.html', 'Forum', 'forum') +
    link('data.json', 'data.json', 'data') +
    link('../../index.html', 'All papers', 'index') +
  '</div></div>';
}
"""


# ─── main ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Export your OpenReview AC assignments to a static site",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--keys", default=os.path.join(HERE, ".keys"), help="file with OpenReview credentials")
    p.add_argument("--out", default=os.path.join(HERE, "export"), help="output directory")
    p.add_argument("--venue", default=DEFAULT_VENUE, help=f"venue id (default {DEFAULT_VENUE})")
    p.add_argument("--role", default="Area_Chairs", help="assignment role (default Area_Chairs)")
    p.add_argument("--only", help="comma-separated submission numbers to export")
    p.add_argument("--limit", type=int, help="export at most N papers (for testing)")
    p.add_argument("--skip-existing", action="store_true", help="skip papers that already have data.json")
    p.add_argument("--no-css", action="store_true", help="do not download OpenReview's stylesheets")
    args = p.parse_args()

    user, pwd = read_keys(args.keys)
    print(f"Logging in as {user}...")
    api = OpenReview(user, pwd)
    print(f"  profile: {api.profile_id}")

    print(f"Fetching {args.role} assignments for {args.venue}...")
    ids = get_assignments(api, args.venue, args.role)
    if not ids:
        sys.exit(f"No {args.role} assignments found for {args.venue}.")
    submissions = load_submissions(api, args.venue, ids)
    ordered = sorted(submissions.values(), key=lambda n: n.get("number") or 0)
    print(f"  {len(ordered)} assignments")

    # The id list always covers every assignment; --only/--limit narrow the export.
    os.makedirs(args.out, exist_ok=True)
    assignments = [
        {
            "number": n.get("number"),
            "id": n["id"],
            "title": val(n.get("content", {}).get("title")),
            "forum_url": f"{OR_WEB}/forum?id={n['id']}",
        }
        for n in ordered
    ]
    with open(os.path.join(args.out, "assignments.json"), "w") as f:
        json.dump(assignments, f, indent=1, ensure_ascii=False)
    with open(os.path.join(args.out, "assignments.txt"), "w") as f:
        f.write("\n".join(str(a["number"]) for a in assignments) + "\n")
    print("  wrote assignments.json / assignments.txt")

    if args.only:
        keep = {int(x) for x in args.only.replace(" ", "").split(",") if x}
        ordered = [n for n in ordered if n.get("number") in keep]
    if args.limit:
        ordered = ordered[: args.limit]

    print("Writing shared assets...")
    write_assets(args.out, use_remote_css=not args.no_css)

    index_rows = []
    for i, sub in enumerate(ordered, 1):
        number = sub.get("number")
        pdir = os.path.join(args.out, "papers", str(number))
        if args.skip_existing and os.path.exists(os.path.join(pdir, "data.json")):
            data = json.load(open(os.path.join(pdir, "data.json")))
            print(f"  [{i}/{len(ordered)}] #{number} (cached)")
        else:
            print(f"  [{i}/{len(ordered)}] #{number} {val(sub.get('content', {}).get('title'))[:60]}")
            data = export_paper(api, args.venue, sub, args.out)
        index_rows.append(
            {
                "number": number,
                "title": data["title"],
                "venue_forum_url": data["venue_forum_url"],
                "n_notes": len(data["forum_notes"]) or len(data["venue_notes"]),
            }
        )

    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(index_html(index_rows, args.venue))

    print(f"\nDone -> {os.path.abspath(args.out)}")
    print(f"Open {os.path.join(os.path.abspath(args.out), 'index.html')} in a browser.")


if __name__ == "__main__":
    main()
