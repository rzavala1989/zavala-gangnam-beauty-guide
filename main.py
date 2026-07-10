# Gangnam Beauty Guide - review syndication pipeline
# deps: requests, beautifulsoup4   |   run: python main.py --dry-run
#
# Judgment that matters more than the code:
#  - The slug from the SSR index IS clinic identity. We never fuzzy-merge clinics
#    on romanized names (the data proved the trap: "OZ" collisions, names that
#    parse as "(2,767)"). Deterministic key first; the model only ever proposes.
#  - Idempotent on (url, content_hash); review identity is (slug, text hash),
#    so metadata drift (a relative date going absolute) never re-publishes.
#  - Gated by reversibility: a translation is cheap to undo -> auto-publish.
#    A "verified" badge or an entity merge is not -> needs registry proof and audit.

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://gangnambeautyguide.com"
INDEX = f"{BASE}/en/clinics/"
SESSION = requests.Session()                                   # keep-alive: one host, ~140 requests
SESSION.headers["User-Agent"] = "gbg-syndication/1.0 (+contact@example.com)"

# ---- storage / idempotency (sqlite: durable, safe to re-run) -----------------
def db(path="gbg.db") -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS checkpoint(url TEXT PRIMARY KEY, hash TEXT);
      CREATE TABLE IF NOT EXISTS clinic(slug TEXT PRIMARY KEY, name TEXT, rating REAL,
        reviews INTEGER, district TEXT, verified INT, name_needs_review INT);
      CREATE TABLE IF NOT EXISTS review(id TEXT PRIMARY KEY, slug TEXT, surgeon TEXT,
        procedure TEXT, dt TEXT, text_ko TEXT, text_en TEXT, verified_surgeon INT);
      CREATE INDEX IF NOT EXISTS review_slug ON review(slug);
      CREATE TABLE IF NOT EXISTS hitl(kind TEXT, slug TEXT, payload TEXT, ts REAL,
        PRIMARY KEY(kind, slug, payload));
      CREATE TABLE IF NOT EXISTS audit(action TEXT, slug TEXT, detail TEXT, ts REAL);
    """)
    return c

def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()

def unchanged(c, url, body):
    row = c.execute("SELECT hash FROM checkpoint WHERE url=?", (url,)).fetchone()
    return bool(row and row[0] == body_hash(body))

def checkpoint(c, url, body):
    c.execute("INSERT INTO checkpoint VALUES(?,?) ON CONFLICT(url) DO UPDATE SET hash=excluded.hash",
              (url, body_hash(body)))
    c.commit()

def flag(c, kind, slug, payload):
    # hitl's primary key makes flagging the same fact twice a no-op, so callers
    # never track "did I already queue this".
    c.execute("INSERT OR IGNORE INTO hitl VALUES(?,?,?,?)", (kind, slug, payload, time.time()))
    c.commit()

def audit(c, action, slug, detail):
    c.execute("INSERT INTO audit VALUES(?,?,?,?)", (action, slug, detail, time.time()))

# ---- models -----------------------------------------------------------------
@dataclass
class Clinic:
    slug: str
    name: str
    rating: float | None
    reviews: int | None
    district: str | None
    verified: bool
    name_needs_review: bool

@dataclass
class RawReview:
    surgeon: str
    procedure: str
    dt: str
    text_ko: str

@dataclass
class Verdict:
    confirmed: bool
    site_flag: bool

# ---- step 1: parse the SSR index (no API, no pagination, 139 rows) ----------
NUMERIC = re.compile(r"^[\d,.()]+$")   # count/rating fragments: "(", "2,767", ")", "4.5"

def plausible_name(ln: str) -> bool:
    # NUMERIC is the "(2,767)" name-bug defense: an off-layout card must fall
    # through to the slug fallback and a hitl flag, never publish a count.
    return (not NUMERIC.match(ln) and "★" not in ln
            and "verified" not in ln.lower() and "photo via" not in ln.lower())

def parse_index(html: bytes) -> list[Clinic]:
    soup, seen, out = BeautifulSoup(html, "html.parser"), set(), []
    for a in soup.select('a[href*="/clinics/"]'):
        parts = [p for p in urlparse(a.get("href", "")).path.split("/") if p]
        i = parts.index("clinics") if "clinics" in parts else -1
        slug = parts[i + 1] if 0 <= i < len(parts) - 1 else ""  # fragment/subpath-proof
        if not slug or slug in seen:
            continue
        seen.add(slug)
        lines = [ln.strip() for ln in a.get_text("\n").split("\n") if ln.strip()]
        raw = next((ln for ln in lines if plausible_name(ln)), "")
        name = raw or slug.replace("-", " ").title()
        t = " ".join(lines)
        rating = float(m.group(1)) if (m := re.search(r"★\s*([1-5]\.\d)\b", t)) else None
        reviews = int(m.group(1).replace(",", "")) if (m := re.search(r"\(\s*([\d,]+)\s*\)", t)) else None
        district = m.group(1) if (m := re.search(r"([A-Za-z]+-gu)", t)) else None
        out.append(Clinic(slug, name, rating, reviews, district, "verified" in t.lower(), not raw))
    return out

# ---- steps 2-7: per-clinic crawl (idempotent, retry-safe unit) --------------
def fetch(url: str) -> bytes:
    r = SESSION.get(url, timeout=20)
    r.raise_for_status()
    return r.content

def process_clinic(c, slug, extract, translate, verify):
    url = INDEX + slug
    body = fetch(url)
    if unchanged(c, url, body):                                # page unchanged -> skip
        return
    for r in extract(body):                                    # step 3: LLM structured extract
        # Identity is (slug, normalized text): the body is the one field the
        # extractor can never leave blank, so two anonymous reviews can't
        # collide and metadata drift (a relative date going absolute, a surgeon
        # name appearing later) can't mint a duplicate. An edited review is a
        # new review by design; \x00 can't occur in page text, so the join
        # can't be gamed the way "|" inside a name could.
        text_norm = " ".join(r.text_ko.split())
        rid = hashlib.sha256(f"{slug}\x00{text_norm}".encode()).hexdigest()
        if c.execute("SELECT 1 FROM review WHERE id=?", (rid,)).fetchone():
            continue                                           # step 5: skip before paying for LLM calls
        en = translate(r.text_ko)                              # step 4: KO->EN, reversible -> auto
        v: Verdict = verify(r.surgeon, slug) if r.surgeon else Verdict(False, False)
        if v.site_flag and not v.confirmed:                    # step 6: site says verified,
            flag(c, "verify_surgeon", slug, r.surgeon)         # registry can't -> human decides
        ins = c.execute("INSERT OR IGNORE INTO review VALUES(?,?,?,?,?,?,?,?)",
                        (rid, slug, r.surgeon, normalize_procedure(r.procedure), r.dt,
                         r.text_ko, en, int(v.confirmed)))
        if ins.rowcount:                                       # audit follows the write, never assumes it
            audit(c, "publish_review", slug, rid)
    checkpoint(c, url, body)                                   # step 7: commit only after full pass

def sync(c, extract, translate, verify):
    body = fetch(INDEX)                                         # step 1: one SSR fetch
    clinics = parse_index(body)
    if not unchanged(c, INDEX, body):
        for cl in clinics:
            # Upsert never touches name or name_needs_review on existing rows:
            # a human-corrected name survives every re-crawl of the index.
            c.execute("""INSERT INTO clinic VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(slug) DO UPDATE SET rating=excluded.rating,
                           reviews=excluded.reviews, district=excluded.district,
                           verified=excluded.verified""",
                      (cl.slug, cl.name, cl.rating, cl.reviews, cl.district,
                       int(cl.verified), int(cl.name_needs_review)))
            if cl.name_needs_review:
                flag(c, "clinic_name", cl.slug, cl.name)       # hitl PK dedupes re-flags
        checkpoint(c, INDEX, body)
    # Fan out over the live index, not the clinic table: delisted clinics keep
    # their published rows but stop being crawled.
    for cl in sorted(clinics, key=lambda x: -(x.reviews or 0)):  # step 2: biggest first
        try:
            process_clinic(c, cl.slug, extract, translate, verify)  # in prod: a Temporal activity
        except Exception as e:                                 # one clinic failing never stops the run
            audit(c, "error", cl.slug, str(e))
            c.commit()

# ---- prompts: the model proposes, deterministic keys decide ------------------
# Glossary pins the English procedure vocabulary so "쌍수" and "쌍꺼풀" land on
# one searchable string. normalize_procedure is its code-side projection;
# TRANSLATE_PROMPT carries the prompt-side one.
PROCEDURE_GLOSSARY = {
    "쌍꺼풀":        "double eyelid surgery",
    "쌍수":          "double eyelid surgery",
    "눈매교정":      "ptosis correction",
    "앞트임":        "epicanthoplasty",
    "뒤트임":        "lateral canthoplasty",
    "눈재수술":      "revision eye surgery",
    "코성형":        "rhinoplasty",
    "코재수술":      "revision rhinoplasty",
    "안면윤곽":      "facial contouring",
    "광대축소":      "cheekbone reduction",
    "사각턱":        "square jaw reduction",
    "양악수술":      "two-jaw surgery",
    "지방흡입":      "liposuction",
    "지방이식":      "fat grafting",
    "실리프팅":      "thread lift",
    "가슴성형":      "breast augmentation",
    "눈밑지방재배치": "under-eye fat repositioning",
}
_GLOSSARY_BLOCK = "\n".join(f"   {ko} -> {en}" for ko, en in PROCEDURE_GLOSSARY.items())

def normalize_procedure(s: str) -> str:
    s = s.strip()
    return PROCEDURE_GLOSSARY.get(s, s)

EXTRACT_PROMPT = """\
You are extracting patient reviews from the HTML of one clinic detail page.

Return a JSON array, one object per distinct patient review:
  {"surgeon": str, "procedure": str, "dt": str, "text_ko": str}

Rules, in priority order:
1. Verbatim only. Copy surgeon, procedure, and review text exactly as printed,
   Korean included. Do not translate, normalize, or expand abbreviations here;
   normalization happens downstream against a license registry.
2. Never guess. Review names no surgeon -> surgeon is "". No procedure stated
   -> procedure is "". A guessed name attaches a real patient review to the
   wrong surgeon, the one failure this pipeline exists to prevent.
3. dt is an ISO date (YYYY-MM-DD) only when the page prints an absolute date.
   Relative dates ("3주 전") become "": you do not know today's date.
4. text_ko is the full review body, untruncated. Skip everything that is not a
   patient review: clinic replies, marketing copy, procedure descriptions,
   navigation text.
5. Zero reviews on the page -> return [].
"""

TRANSLATE_PROMPT = f"""\
Translate one Korean patient review of a cosmetic clinic into English.

Rules:
1. Procedure terms use these exact English strings, no synonyms:
{_GLOSSARY_BLOCK}
2. Preserve tone. A complaint stays a complaint; do not soften criticism or
   inflate praise. The review's trust value is that it reads unedited.
3. Keep every number exact: prices (keep the ₩), dates, session counts,
   recovery times.
4. Romanize personal names (Revised Romanization); do not translate them.
5. Output the English translation only. No notes, no summary, no disclaimers,
   no added medical advice.
"""

# ---- model-backed seams: wire to your LLM / registry ------------------------
def strip_boilerplate(body: bytes) -> str:
    # The detail page is mostly nav, script, and svg; the reviews are a few KB.
    # Stripping before the extract call cuts its token spend by an order of
    # magnitude while keeping the tag structure the prompt relies on.
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "head", "svg", "noscript"]):
        tag.decompose()
    return str(soup)

if __name__ == "__main__":
    if "--dry-run" in sys.argv:                                # seed layer only: no key, no db
        for cl in parse_index(fetch(INDEX)):
            print(f"{cl.slug:28} | {cl.name:34} | {cl.rating} | {cl.reviews} | "
                  f"{cl.district} | needs_review={cl.name_needs_review}")
    else:
        try:
            from impls import YourLLM, registry                # <- wire real impls in impls.py
        except ImportError:
            raise SystemExit("no impls.py: define YourLLM (json/text) and registry (check)")
        conn = db()
        llm = YourLLM()

        def extract(html: bytes) -> Iterable[RawReview]:
            return llm.json(prompt=EXTRACT_PROMPT, html=strip_boilerplate(html), schema=RawReview)

        def translate(ko: str) -> str:
            return llm.text(prompt=TRANSLATE_PROMPT, input=ko)

        @lru_cache(maxsize=None)                               # same (surgeon, clinic) asked once per run
        def verify(surgeon: str, slug: str) -> Verdict:
            return registry.check(surgeon, slug)

        sync(conn, extract, translate, verify)
