"""Știri — builds the site.

Reads settings.yml, fetches every feed (with a Google News fallback per
source), applies each lane's rules, and writes site/ with data.json.
Run by GitHub Actions every 15 minutes. You don't need to edit this file.
"""

from __future__ import annotations

import hashlib
import html
import html.entities
import json
import re
import shutil
import sys
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

import requests
import yaml

ROOT = Path(__file__).parent
OUT = ROOT / "site"
NOW = datetime.now(timezone.utc)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 StiriReader/1.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
}

GNEWS_LOCALE = {
    "ro": "hl=ro&gl=RO&ceid=RO:ro",
    "en": "hl=en-GB&gl=GB&ceid=GB:en",
}


# ----------------------------------------------------------------- text utils

def fold(text: str) -> str:
    """Lowercase and strip diacritics (handles both ș/ş and ț/ţ forms)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = TAG_RE.sub(" ", value)
    text = html.unescape(html.unescape(text))
    return WS_RE.sub(" ", text).strip()


def shorten(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:–-")
    return cut + "…"


def parse_duration(value, fallback: timedelta) -> timedelta:
    if value is None:
        return fallback
    m = re.fullmatch(r"\s*(\d+)\s*([hd])\s*", str(value).lower())
    if not m:
        print(f"  ! could not read max_age '{value}', using default")
        return fallback
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def keyword_matcher(words):
    if not words:
        return None
    parts = [re.escape(fold(str(w)).strip()) for w in words if str(w).strip()]
    return re.compile(r"\b(?:" + "|".join(parts) + r")") if parts else None


# ----------------------------------------------------------------- XML parsing

XML_OK_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
BARE_AMP_RE = re.compile(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
DECL_RE = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9_\-]+)[\"']")


def _fix_entity(m: re.Match) -> str:
    name = m.group(1)
    if name in XML_OK_ENTITIES:
        return m.group(0)
    code = html.entities.name2codepoint.get(name)
    return f"&#{code};" if code else f"&amp;{name};"


def parse_xml(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        pass
    # Repair common problems: HTML entities, stray &, control chars, junk before <?xml
    m = DECL_RE.match(content)
    encoding = m.group(1).decode() if m else "utf-8"
    try:
        text = content.decode(encoding, errors="replace")
    except LookupError:
        text = content.decode("utf-8", errors="replace")
    text = text.lstrip("\ufeff \t\r\n")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text)
    text = CTRL_RE.sub("", text)
    text = ENTITY_RE.sub(_fix_entity, text)
    text = BARE_AMP_RE.sub("&amp;", text)
    return ET.fromstring(text.encode("utf-8"))


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child(el: ET.Element, name: str):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el: ET.Element, name: str):
    return [c for c in el if local(c.tag) == name]


def text_of(el: ET.Element, *names: str) -> str:
    for name in names:
        c = child(el, name)
        if c is not None:
            # content may contain child elements (e.g. xhtml in Atom)
            value = "".join(c.itertext()).strip()
            if value:
                return value
    return ""


def parse_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return min(dt, NOW)  # sites with wrong time zones sometimes post "in the future"


IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def fix_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def find_image(item: ET.Element, raw_html: str) -> str:
    for el in item.iter():
        name = local(el.tag)
        if name in ("content", "thumbnail") and el.get("url"):
            medium = (el.get("medium") or "").lower()
            mtype = (el.get("type") or "").lower()
            url = el.get("url")
            if name == "thumbnail" or medium == "image" or mtype.startswith("image") or re.search(
                r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I
            ):
                return fix_url(url)
        if name == "enclosure" and (el.get("type") or "").startswith("image") and el.get("url"):
            return fix_url(el.get("url"))
    m = IMG_RE.search(raw_html or "")
    if m and "pixel" not in m.group(1) and "feeds.feedburner" not in m.group(1):
        return fix_url(m.group(1))
    return ""


def parse_feed(content: bytes) -> list[dict]:
    root = parse_xml(content)
    rootname = local(root.tag)
    items = []
    if rootname == "feed":  # Atom
        entries = children(root, "entry")
    else:  # RSS 2.0 / RSS 1.0 (RDF)
        channel = child(root, "channel")
        entries = children(channel, "item") if channel is not None else []
        if not entries:
            entries = children(root, "item")
    for e in entries:
        title = clean_text(text_of(e, "title"))
        link = ""
        for l in children(e, "link"):
            href = l.get("href")
            if href and (l.get("rel") in (None, "alternate")):
                link = href
                break
            if not href and (l.text or "").strip():
                link = l.text.strip()
                break
        if not link:
            guid = child(e, "guid")
            if guid is not None and (guid.text or "").startswith("http"):
                link = guid.text.strip()
        if not title or not link:
            continue
        raw_summary = text_of(e, "description", "summary", "encoded", "content")
        raw_html = text_of(e, "encoded", "content", "description", "summary")
        src = child(e, "source")
        items.append(
            {
                "title": title,
                "link": fix_url(link),
                "time": parse_date(text_of(e, "pubDate", "published", "updated", "date", "issued")),
                "snippet": shorten(clean_text(raw_summary)),
                "image": find_image(e, raw_html),
                "publisher": clean_text(src.text) if src is not None and src.text else "",
            }
        )
    return items


# ----------------------------------------------------------------- fetching

def fetch(url: str) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.content


def gnews_url(query: str, language: str, age: timedelta) -> str:
    when = f"{max(1, age.days)}d" if age >= timedelta(days=1) else f"{max(1, int(age.total_seconds() // 3600))}h"
    q = quote_plus(f"{query} when:{when}")
    return f"https://news.google.com/rss/search?q={q}&{GNEWS_LOCALE.get(language, GNEWS_LOCALE['en'])}"


def strip_publisher(title: str, publisher: str) -> str:
    if publisher and title.endswith(" - " + publisher):
        return title[: -len(publisher) - 3].rstrip()
    return title


def fresh(items, age):
    cutoff = NOW - age
    return [i for i in items if i["time"] is None or i["time"] >= cutoff]


def load_source(job: dict) -> dict:
    """Fetch one source (feed, then Google News fallback) or one search."""
    result = {"items": [], "status": "failed", "via": "", "error": ""}
    age, lang = job["age"], job["language"]

    if job["kind"] == "search":
        try:
            items = parse_feed(fetch(gnews_url(job["query"], lang, age)))
            for i in items:
                i["title"] = strip_publisher(i["title"], i["publisher"])
                i["source"] = i["publisher"] or "Google News"
                i["snippet"] = ""  # Google's summaries just repeat the title
            result.update(items=fresh(items, age), status="ok", via="search")
        except Exception as exc:  # noqa: BLE001
            result["error"] = short_error(exc)
        return result

    src = job["source"]
    name = src["name"]
    if src.get("feed"):
        try:
            parsed = parse_feed(fetch(src["feed"]))
            items = fresh(parsed, age)
            if items:
                for i in items:
                    i["source"] = name
                result.update(items=items, status="ok", via="feed")
                return result
            result["error"] = "feed has no recent headlines" if parsed else "not a valid feed"
        except Exception as exc:  # noqa: BLE001
            result["error"] = short_error(exc)
    if src.get("site"):
        try:
            items = parse_feed(fetch(gnews_url(f"site:{src['site']}", lang, age)))
            for i in items:
                i["title"] = strip_publisher(i["title"], i["publisher"])
                i["source"] = name
                i["snippet"] = ""
            result.update(items=fresh(items, age), status="fallback" if src.get("feed") else "ok", via="google")
            return result
        except Exception as exc:  # noqa: BLE001
            result["error"] = (result["error"] + "; " if result["error"] else "") + "backup: " + short_error(exc)
    return result


def short_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "could not connect"
    if isinstance(exc, ET.ParseError):
        return "not a valid feed"
    return type(exc).__name__


# ----------------------------------------------------------------- lanes

def norm_link(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/"), "", ""))


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(title))[:90]


def build_lane(lane_cfg: dict, results: list[dict], defaults: dict) -> dict:
    per_source = int(lane_cfg.get("per_source", defaults.get("per_source", 4)))
    max_items = int(lane_cfg.get("max_items", defaults.get("max_items", 40)))
    include = keyword_matcher(lane_cfg.get("include"))
    exclude = keyword_matcher(lane_cfg.get("exclude"))
    blocked = [fold(str(s)) for s in lane_cfg.get("exclude_sources") or []]

    seen_links, seen_titles, pool = set(), set(), []
    for res in results:
        for item in res["items"]:
            if any(b in fold(item["source"]) for b in blocked):
                continue
            lk, tk = norm_link(item["link"]), title_key(item["title"])
            if lk in seen_links or (tk and tk in seen_titles):
                continue
            hay = fold(item["title"] + " " + item["snippet"])
            if include and not include.search(hay):
                continue
            if exclude and exclude.search(hay):
                continue
            seen_links.add(lk)
            seen_titles.add(tk)
            pool.append(item)

    # Group by outlet, newest first inside each group
    groups: dict[str, list] = {}
    for item in pool:
        groups.setdefault(item["source"], []).append(item)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for g in groups.values():
        g.sort(key=lambda i: i["time"] or oldest, reverse=True)
        del g[per_source:]

    has_sources = bool(lane_cfg.get("sources"))
    order = lane_cfg.get("order", "mix" if has_sources else "newest")
    if order == "newest":
        ordered = sorted((i for g in groups.values() for i in g), key=lambda i: i["time"] or oldest, reverse=True)
    else:  # alternate between outlets so no single site floods the row
        queues = sorted(groups.values(), key=lambda g: g[0]["time"] or oldest, reverse=True)
        ordered = []
        while any(queues):
            for q in queues:
                if q:
                    ordered.append(q.pop(0))
    ordered = ordered[:max_items]

    return {
        "name": lane_cfg["name"],
        "items": [
            {
                "id": hashlib.sha1(norm_link(i["link"]).encode()).hexdigest()[:12],
                "title": i["title"],
                "link": i["link"],
                "source": i["source"],
                "time": i["time"].isoformat(timespec="seconds").replace("+00:00", "Z") if i["time"] else None,
                "snippet": i["snippet"],
                "image": i["image"],
            }
            for i in ordered
        ],
    }


# ----------------------------------------------------------------- main

def main() -> int:
    cfg = yaml.safe_load((ROOT / "settings.yml").read_text(encoding="utf-8"))
    defaults = cfg.get("defaults") or {}
    default_age = parse_duration(defaults.get("max_age"), timedelta(hours=24))

    jobs = []  # (page_idx, lane_idx, job)
    for p, page in enumerate(cfg["pages"]):
        lang = page.get("language", "en")
        for l, lane in enumerate(page.get("lanes") or []):
            age = parse_duration(lane.get("max_age"), default_age)
            for src in lane.get("sources") or []:
                jobs.append((p, l, {"kind": "source", "source": src, "age": age, "language": lang}))
            for q in lane.get("searches") or []:
                jobs.append((p, l, {"kind": "search", "query": str(q), "age": age, "language": lang}))

    print(f"Fetching {len(jobs)} feeds…")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda j: load_source(j[2]), jobs))

    pages_out, status, total = [], [], 0
    for p, page in enumerate(cfg["pages"]):
        lanes_out = []
        for l, lane in enumerate(page.get("lanes") or []):
            lane_results = [r for (jp, jl, _), r in zip(jobs, results) if jp == p and jl == l]
            built = build_lane(lane, lane_results, defaults)
            total += len(built["items"])
            lanes_out.append(built)
        pages_out.append({"name": page["name"], "lanes": lanes_out})

    for (p, l, job), res in zip(jobs, results):
        label = job["source"]["name"] if job["kind"] == "source" else f"Căutare: {job['query']}"
        entry = {
            "page": cfg["pages"][p]["name"],
            "lane": cfg["pages"][p]["lanes"][l]["name"],
            "name": label,
            "status": res["status"],
            "count": len(res["items"]),
            "error": res["error"],
        }
        status.append(entry)
        mark = {"ok": "ok  ", "fallback": "BKUP", "failed": "FAIL"}[res["status"]]
        print(f"  [{mark}] {entry['page']} / {entry['lane']} / {label}: {entry['count']} {res['error']}")

    if total == 0:
        print("No headlines at all — keeping the previous version of the site.")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / "web", OUT)
    data = {"generated": NOW.isoformat(timespec="seconds").replace("+00:00", "Z"), "pages": pages_out, "status": status}
    (OUT / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Done: {total} headlines written to site/data.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
