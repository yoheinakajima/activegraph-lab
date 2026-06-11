"""The public blog — server-rendered projection of published artifacts.

PLUMBING (ADR-012, ADR-013). The blog is the front door: GET / lists
published posts (artifacts with kind=blog_draft, status=published) newest
first; GET /posts/<slug> renders one post FROM THE GRAPH (never the drafts/
mirror file) plus its provenance subgraph — the "Show the work" section.
GET /feed.xml is RSS over published posts.

No framework, no build step, no JS required to read posts. Markdown is
rendered server-side by the minimal pure-Python renderer below (headings,
paragraphs, emphasis, code, links, lists, blockquotes, hr, and the draft
contract's footnotes). No new dependency — stdlib only.

Everything here is a pure read over graph state; all writes stay behind
the gate and the operator token.
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional

SITE_NAME = "activegraph-lab"
MISSION_ONE_LINER = (
    "A self-hosted research agent growing activegraph.ai's evidence base — "
    "everything it does is an event you can read."
)

EMPTY_STATE = (
    "This is the public notebook of an autonomous research lab built on "
    "ActiveGraph: it reads activegraph.ai, finds claims without evidence, and "
    "writes up what it learns. Nothing is published here until a human "
    "approves it through a gated decision recorded in the lab's event log. "
    "Nothing is published yet — but the work is already visible live in the "
    "open notebook."
)


# ---------------------------------------------------------------- markdown

_INLINE_RULES = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)"),
     r'<a href="\2">\1</a>'),
]
_FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\]")
_FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")


def _inline(text: str) -> str:
    """Inline markup over already-escaped text. Footnote refs become
    superscript anchors into the footnotes list."""
    out = text
    for pat, repl in _INLINE_RULES:
        out = pat.sub(repl, out)
    out = _FOOTNOTE_REF.sub(
        lambda m: (f'<sup id="fnref-{html.escape(m.group(1))}">'
                   f'<a href="#fn-{html.escape(m.group(1))}">{html.escape(m.group(1))}</a></sup>'),
        out)
    return out


def render_markdown(text: str) -> str:
    """Minimal, deterministic markdown → HTML. Escapes everything first, so
    graph-derived content can never inject markup."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    footnotes: list[tuple[str, str]] = []
    para: list[str] = []
    in_code = False
    code: list[str] = []
    list_tag: Optional[str] = None

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in lines:
        line = raw.rstrip()
        if in_code:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + "\n".join(code) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                code.append(html.escape(raw))
            continue
        if line.strip().startswith("```"):
            flush_para()
            close_list()
            in_code = True
            continue

        esc = html.escape(line)
        m = _FOOTNOTE_DEF.match(line.strip())
        if m:
            flush_para()
            footnotes.append((m.group(1), html.escape(m.group(2))))
            continue
        if not line.strip():
            flush_para()
            close_list()
            continue
        if line.startswith("#"):
            flush_para()
            close_list()
            level = min(len(line) - len(line.lstrip("#")), 4)
            out.append(f"<h{level}>" + _inline(html.escape(line.lstrip('#').strip()))
                       + f"</h{level}>")
            continue
        if line.strip() in ("---", "***", "___"):
            flush_para()
            close_list()
            out.append("<hr>")
            continue
        if line.lstrip().startswith(">"):
            flush_para()
            close_list()
            out.append("<blockquote>"
                       + _inline(html.escape(line.lstrip().lstrip(">").strip()))
                       + "</blockquote>")
            continue
        lm = re.match(r"^\s*[-*]\s+(.*)$", line)
        om = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if lm or om:
            flush_para()
            want = "ul" if lm else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            out.append("<li>" + _inline(html.escape((lm or om).group(1))) + "</li>")
            continue
        close_list()
        para.append(esc)

    flush_para()
    close_list()
    if in_code and code:
        out.append("<pre><code>" + "\n".join(code) + "</code></pre>")
    if footnotes:
        items = "".join(
            f'<li id="fn-{html.escape(k)}">{_inline(v)} '
            f'<a href="#fnref-{html.escape(k)}">↩</a></li>'
            for k, v in footnotes)
        out.append(f'<section class="footnotes"><hr><ol>{items}</ol></section>')
    return "\n".join(out)


def strip_markdown(text: str, words: int = 40) -> str:
    """First ~N words of a markdown body, markup stripped (the index teaser)."""
    body = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    body = re.sub(r"^#+\s.*$", " ", body, flags=re.M)
    body = re.sub(r"\[\^[^\]]+\]:?\s*\S*", " ", body)
    body = re.sub(r"[*_`>#]|\[(.*?)\]\([^)]*\)", r"\1", body)
    toks = body.split()
    teaser = " ".join(toks[:words])
    return teaser + ("…" if len(toks) > words else "")


# ---------------------------------------------------------------- projections


def published_posts(graph) -> list:
    """Published blog posts, newest first by published_at."""
    posts = [a for a in graph.objects(type="artifact")
             if a.data.get("kind") == "blog_draft"
             and a.data.get("status") == "published"]
    posts.sort(key=lambda a: ((a.data.get("metadata") or {}).get("published_at") or "",
                              str(a.id)),
               reverse=True)
    return posts


def post_by_slug(graph, slug: str):
    for a in published_posts(graph):
        if (a.data.get("metadata") or {}).get("slug") == slug:
            return a
    return None


def _obj_id_order(obj_id: str) -> int:
    try:
        return int(str(obj_id).rsplit("#", 1)[-1])
    except ValueError:
        return 0


def provenance(graph, artifact) -> dict[str, Any]:
    """The post's provenance subgraph (ADR-013): originating branch, evidence
    observations, chat on that branch, the publish decision, prior drafts."""
    meta = artifact.data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    branch = graph.get_object(branch_id) if branch_id else None

    evidence = []
    for ref in artifact.data.get("observation_ids") or []:
        o = graph.get_object(ref)
        if o is not None:
            evidence.append({
                "id": str(o.id), "type": str(o.type),
                "text": (o.data.get("text") or o.data.get("title")
                         or o.data.get("rationale") or "")[:400],
            })

    chat = []
    if branch_id:
        for m in graph.objects(type="comm_message"):
            if (m.data.get("metadata") or {}).get("lab_branch_id") == branch_id:
                chat.append({"id": str(m.id), "who": m.data.get("sender_ref") or "operator",
                             "text": (m.data.get("content") or "")[:400]})
        for r in graph.objects(type="comm_response_candidate"):
            if (r.data.get("created_by_behavior") == "lab.answer"
                    and ((r.data.get("metadata") or {}).get("provenance") or {})
                    .get("branch_id") == branch_id):
                chat.append({"id": str(r.id), "who": "lab",
                             "text": (r.data.get("content") or "")[:400]})
        chat.sort(key=lambda x: _obj_id_order(x["id"]))

    decision = next(
        (d for d in graph.objects(type="decision")
         if d.data.get("kind") == "publish"
         and d.data.get("subject_ref") == str(artifact.id)
         and d.data.get("status") == "approved"), None)

    prior = []
    finding_id = meta.get("finding_id")
    if finding_id:
        for a in graph.objects(type="artifact"):
            if (a.id != artifact.id and a.data.get("kind") == "blog_draft"
                    and (a.data.get("metadata") or {}).get("finding_id") == finding_id):
                prior.append({"id": str(a.id), "title": a.data.get("title"),
                              "status": a.data.get("status"),
                              "slug": (a.data.get("metadata") or {}).get("slug")})

    return {
        "branch": ({"id": str(branch.id), "title": branch.data.get("title"),
                    "status": branch.data.get("status")} if branch is not None else None),
        "evidence": evidence,
        "chat": chat,
        "decision": ({"id": str(decision.id), "rationale": decision.data.get("rationale")}
                     if decision is not None else None),
        "prior_drafts": prior,
    }


# ---------------------------------------------------------------- pages

_CSS = """
:root { --ink:#1c1c1c; --dim:#6b6b6b; --line:#e4e1da; --bg:#faf9f6; --acc:#2b5e8c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:17px/1.65 Georgia, 'Times New Roman', serif; }
main { max-width: 42rem; margin: 0 auto; padding: 1.2rem 1.2rem 4rem; }
header.site { border-bottom:1px solid var(--line); padding:1.1rem 1.2rem; }
header.site .name { font-weight:bold; font-size:1.05rem; letter-spacing:.02em; }
header.site .name a { color:var(--ink); text-decoration:none; }
header.site .tag { color:var(--dim); font-size:.85rem; margin-top:.15rem;
  font-family: ui-sans-serif, system-ui, sans-serif; }
header.site .nav { float:right; font-family: ui-sans-serif, system-ui, sans-serif;
  font-size:.85rem; }
a { color: var(--acc); }
h1 { font-size:1.7rem; line-height:1.25; margin:1.6rem 0 .4rem; }
h2 { font-size:1.2rem; margin:1.8rem 0 .5rem; }
.post-meta { color:var(--dim); font-size:.85rem;
  font-family: ui-sans-serif, system-ui, sans-serif; }
.kind { display:inline-block; padding:.05rem .5rem; border:1px solid var(--line);
  border-radius:9px; font-size:.74rem; text-transform:uppercase; letter-spacing:.06em;
  font-family: ui-sans-serif, system-ui, sans-serif; color:var(--dim); }
.kind.research { color:#7a4a14; border-color:#e0c9a6; }
.kind.build { color:#3d5a3d; border-color:#bfd4bf; }
ul.posts { list-style:none; padding:0; }
ul.posts li { border-bottom:1px solid var(--line); padding:1.1rem 0; }
ul.posts .title { font-size:1.18rem; }
ul.posts .title a { color:var(--ink); text-decoration:none; }
ul.posts .title a:hover { color:var(--acc); }
ul.posts .teaser { margin:.35rem 0 0; color:#3a3a3a; }
pre { background:#f1efe9; padding: .8rem; overflow-x:auto; border-radius:4px;
  font-size:.82rem; }
code { background:#f1efe9; padding:.05rem .25rem; border-radius:3px; font-size:.85em; }
pre code { background:none; padding:0; }
blockquote { border-left:3px solid var(--line); margin:1rem 0; padding:.1rem 1rem;
  color:var(--dim); }
.footnotes { font-size:.85rem; color:var(--dim); }
section.work { margin-top:3rem; border-top:2px solid var(--line); padding-top:1rem;
  font-family: ui-sans-serif, system-ui, sans-serif; font-size:.88rem; }
section.work h2 { font-size:1rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--dim); }
section.work .item { border-left:3px solid var(--line); padding:.3rem .8rem;
  margin:.6rem 0; }
section.work .who { color:var(--dim); }
section.work .ref { color:var(--dim); font-size:.78rem; }
footer.site { border-top:1px solid var(--line); color:var(--dim);
  font:.8rem ui-sans-serif, system-ui, sans-serif; padding:1rem 1.2rem;
  margin-top:2rem; }
.empty { color:var(--dim); margin-top:2.5rem; }
"""


def _layout(title: str, body: str, *, status_line: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="/feed.xml">
<style>{_CSS}</style>
</head>
<body>
<header class="site">
  <span class="nav"><a href="/lab">open notebook →</a></span>
  <div class="name"><a href="/">{SITE_NAME}</a></div>
  <div class="tag">{html.escape(MISSION_ONE_LINER)}</div>
</header>
<main>
{body}
</main>
<footer class="site">{status_line}
  every word here was approved through a gated decision in a public event log
  · <a href="/lab">the notebook</a> · <a href="/feed.xml">rss</a></footer>
</body>
</html>"""


def _date_of(artifact) -> str:
    ts = (artifact.data.get("metadata") or {}).get("published_at") or ""
    return str(ts)[:10]


def _kind_of(artifact) -> str:
    k = (artifact.data.get("metadata") or {}).get("post_kind") or "note"
    return k if k in ("note", "research", "build") else "note"


def index_page(graph, *, status_line: str = "") -> str:
    posts = published_posts(graph)
    if not posts:
        body = (f'<div class="empty"><p>{html.escape(EMPTY_STATE)}</p>'
                f'<p><a href="/lab">Watch the lab work, live →</a></p></div>')
        return _layout(SITE_NAME, body, status_line=status_line)
    items = []
    for a in posts:
        meta = a.data.get("metadata") or {}
        slug = html.escape(meta.get("slug") or "")
        kind = _kind_of(a)
        items.append(
            f'<li><div class="post-meta"><span class="kind {kind}">{kind}</span> '
            f'{_date_of(a)}</div>'
            f'<div class="title"><a href="/posts/{slug}">'
            f'{html.escape(a.data.get("title") or slug)}</a></div>'
            f'<p class="teaser">{html.escape(strip_markdown(a.data.get("content") or ""))}</p></li>')
    return _layout(SITE_NAME, f'<ul class="posts">{"".join(items)}</ul>',
                   status_line=status_line)


def _entity_link(entity_id: str, label: Optional[str] = None) -> str:
    """An id rendered on the blog links into the notebook's inspector — the
    provenance chain is walkable by click, not just readable."""
    eid = html.escape(str(entity_id))
    return f'<a href="/lab#entity={eid}">{html.escape(label or str(entity_id))}</a>'


def _work_section(prov: dict, artifact_id: Optional[str] = None) -> str:
    """2b: 'Show the work' — rendered from the provenance subgraph. Public,
    server-rendered, no token needed. Every id is a link into the live
    notebook's inspector (/lab#entity=<id>)."""
    parts = ['<section class="work"><h2>Show the work</h2>']
    if artifact_id:
        parts.append(
            f'<p class="ref">This post is artifact {_entity_link(artifact_id)} '
            'in the lab’s public event log; every reference below opens '
            'in the live notebook.</p>')
    b = prov.get("branch")
    if b:
        parts.append(
            f'<p>Originating branch: <a href="/lab#branch={html.escape(b["id"])}">'
            f'{html.escape(b["title"] or b["id"])}</a> '
            f'<span class="ref">({html.escape(b["status"] or "")} · '
            f'{_entity_link(b["id"], "inspect " + b["id"])})</span></p>')
    if prov.get("evidence"):
        parts.append("<h3>Evidence</h3>")
        for e in prov["evidence"]:
            parts.append(f'<div class="item">{html.escape(e["text"])}'
                         f'<div class="ref">{html.escape(e["type"])} '
                         f'{_entity_link(e["id"])}</div></div>')
    if prov.get("chat"):
        parts.append("<h3>Conversation on this branch</h3>")
        for m in prov["chat"]:
            parts.append(f'<div class="item"><span class="who">{html.escape(m["who"])}:</span> '
                         f'{html.escape(m["text"])}'
                         f'<div class="ref">{_entity_link(m["id"])}</div></div>')
    d = prov.get("decision")
    if d:
        parts.append(f'<h3>The publish decision</h3><div class="item">'
                     f'{html.escape(d["rationale"] or "")}'
                     f'<div class="ref">{_entity_link(d["id"])}</div></div>')
    if prov.get("prior_drafts"):
        parts.append("<h3>Prior draft versions</h3>")
        for p in prov["prior_drafts"]:
            parts.append(f'<div class="item">{html.escape(p["title"] or p["id"])} '
                         f'<span class="ref">({html.escape(p["status"] or "")} · '
                         f'{_entity_link(p["id"])})</span></div>')
    parts.append("</section>")
    return "\n".join(parts)


def post_page(graph, slug: str, *, status_line: str = "") -> Optional[str]:
    artifact = post_by_slug(graph, slug)
    if artifact is None:
        return None
    meta = artifact.data.get("metadata") or {}
    kind = _kind_of(artifact)
    content = artifact.data.get("content") or ""
    # The artifact content starts with "# title" — drop it; the page owns <h1>.
    content = re.sub(r"^#\s+.*\n+", "", content, count=1)
    body = (
        f'<article><div class="post-meta"><span class="kind {kind}">{kind}</span> '
        f'{_date_of(artifact)}</div>'
        f'<h1>{html.escape(artifact.data.get("title") or slug)}</h1>'
        + render_markdown(content)
        + "</article>"
        + _work_section(provenance(graph, artifact), artifact_id=str(artifact.id)))
    return _layout(artifact.data.get("title") or slug, body, status_line=status_line)


# ---------------------------------------------------------------- RSS


def rss(graph, base_url: str) -> str:
    """RSS 2.0 over published posts only (2d)."""
    items = []
    for a in published_posts(graph):
        meta = a.data.get("metadata") or {}
        slug = meta.get("slug") or ""
        link = f"{base_url}/posts/{slug}"
        items.append(
            "<item>"
            f"<title>{html.escape(a.data.get('title') or slug)}</title>"
            f"<link>{html.escape(link)}</link>"
            f"<guid isPermaLink=\"true\">{html.escape(link)}</guid>"
            f"<pubDate>{html.escape(str(meta.get('published_at') or ''))}</pubDate>"
            f"<description>{html.escape(strip_markdown(a.data.get('content') or '', 60))}</description>"
            "</item>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{SITE_NAME}</title>"
        f"<link>{html.escape(base_url or '/')}</link>"
        f"<description>{html.escape(MISSION_ONE_LINER)}</description>"
        + "".join(items)
        + "</channel></rss>")
