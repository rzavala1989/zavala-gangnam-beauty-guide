# Gangnam Beauty Guide - review syndication pipeline
# deps: requests, beautifulsoup4   |   run: python gbg.py --dry-run
#
# Judgment that matters more than the code:
#  - The slug from the SSR index IS clinic identity. We never fuzzy-merge clinics
#    on romanized names (the data proved the trap: "OZ" collisions, names that
#    parse as "(2,767)"). Deterministic key first; the model only ever proposes.
#  - Idempotent on (url, content_hash): re-running never double-publishes.
#  - Gated by reversibility: a translation is cheap to undo -> auto-publish.
#    A "verified" badge or an entity merge is not -> needs registry proof and audit.

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://gangnambeautyguide.com"
INDEX = "https://gangnambeautyguide.com/en/clinics/"
UA = {"User-Agent": "gbg-syndication/1.0 (+contact@example.com)"}

# ---- storage / idempotency (sqlite: durable, safe to re-run) -----------------
def db(path="gbg.db") -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS checkpoint(url TEXT PRIMARY KEY, hash TEXT);
      CREATE TABLE IF NOT EXISTS clinic(slug TEXT PRIMARY KEY, name TEXT, rating REAL,
        reviews INTEGER, district TEXT, verified INT, name_needs_review INT);
      CREATE TABLE IF NOT EXISTS review(id TEXT PRIMARY KEY, slug TEXT, surgeon TEXT,
        procedure TEXT, dt TEXT, text_ko TEXT, text_en TEXT, verified_surgeon INT);
      CREATE TABLE IF NOT EXISTS hitl(kind TEXT, slug TEXT, payload TEXT, ts REAL);
      CREATE TABLE IF NOT EXISTS audit(action TEXT, slug TEXT, detail TEXT, ts REAL);
    """)
    return c

def unchanged(c, url, body):
    row = c.execute("SELECT hash FROM checkpoint WHERE url=?", (url,)).fetchone()
    return bool(row and row[0] == hashlib.sha256(body).hexdigest())

def checkpoint(c, url, body):
    c.execute("INSERT INTO checkpoint VALUES(?,?) ON CONFLICT(url) DO UPDATE SET hash=excluded.hash",
              (url, hashlib.sha256(body).hexdigest()))
    c.commit()

def flag(c, kind, slug, payload):
    c.execute("INSERT INTO hitl VALUES(?,?,?,?)", (kind, slug, payload, time.time()))
    c.commit()

def audit(c, action, slug, detail):
    c.execute("INSERT INTO audit VALUES(?,?,?,?)", (action, slug, detail, time.time()))

# ---- models -----------------------------------------------------------------
@dataclass
class Clinic:
    slug: str
    name: str
    rating: Optional[float]
    reviews: Optional[int]
    district: Optional[str]
    verified: bool
    name_needs_review: bool = False

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
NUMERIC = re.compile(r"^\(?[\d,]+\)?$")

def parse_index(html: bytes) -> list[Clinic]:
    soup, seen, out = BeautifulSoup(html, "html.parser"), set(), []
    for a in soup.select('a[href*="/clinics/"]'):
        slug = a.get("href", "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug == "clinics" or slug in seen:
            continue
        seen.add(slug)
        lines = [ln.strip() for ln in a.get_text("\n").split("\n") if ln.strip()]
        raw = next((ln for ln in lines if not NUMERIC.match(ln) and "★" not in ln
                    and "verified" not in ln.lower() and "photo via" not in ln.lower()), "")
        needs = not raw or NUMERIC.match(raw) is not None      # the "(2,767)" name bug
        name = slug.replace("-", " ").title() if needs else raw
        t = " ".join(lines)
        rating = float(m.group(1)) if (m := re.search(r"\b([1-5]\.\d)\b", t)) else None
        reviews = int(m.group(1).replace(",", "")) if (m := re.search(r"\(\s*([\d,]+)\s*\)", t)) else None
        district = m.group(1) if (m := re.search(r"([A-Za-z]+-gu)", t)) else None
        out.append(Clinic(slug, name, rating, reviews, district, "verified" in t.lower(), needs))
    return out

# ---- steps 2-7: per-clinic crawl (idempotent, retry-safe unit) --------------
def fetch(url: str) -> bytes:
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.content

def process_clinic(c, slug, extract, translate, verify):
    url = f"{BASE}/en/clinics/{slug}"
    body = fetch(url)
    if unchanged(c, url, body):                                # page unchanged -> skip
        return
    for r in extract(body):                                    # step 3: LLM structured extract
        rid = hashlib.sha256(f"{slug}|{r.surgeon}|{r.procedure}|{r.dt}".encode()).hexdigest()
        dup = c.execute("SELECT 1 FROM review WHERE slug=? AND surgeon=? AND procedure=? AND dt=?",
                        (slug, r.surgeon, r.procedure, r.dt)).fetchone()
        if dup:                                                # step 5: same review, syndicated
            continue
        en = translate(r.text_ko)                              # step 4: KO->EN, reversible -> auto
        v: Verdict = verify(r.surgeon, slug)                   # step 6: cross-check license registry
        if not v.confirmed and v.site_flag:                    # site says verified, registry can't
            flag(c, "verify_surgeon", slug, r.surgeon)         #   -> human; never trust flag blind
        c.execute("INSERT OR IGNORE INTO review VALUES(?,?,?,?,?,?,?,?)",
                  (rid, slug, r.surgeon, r.procedure, r.dt, r.text_ko, en, int(v.confirmed)))
        audit(c, "publish_review", slug, rid)
    checkpoint(c, url, body)                                   # step 7: commit only after full pass
    c.commit()

def sync(c, extract, translate, verify):
    body = fetch(INDEX)                                         # step 1: one SSR fetch
    if not unchanged(c, INDEX, body):
        for cl in parse_index(body):
            c.execute("INSERT OR REPLACE INTO clinic VALUES(?,?,?,?,?,?,?)",
                      (cl.slug, cl.name, cl.rating, cl.reviews, cl.district,
                       int(cl.verified), int(cl.name_needs_review)))
            if cl.name_needs_review:
                flag(c, "clinic_name", cl.slug, cl.name)       # repaired name -> confirm before live
        checkpoint(c, INDEX, body)
        c.commit()
    rows = c.execute("SELECT slug FROM clinic ORDER BY reviews IS NULL, reviews DESC").fetchall()
    for (slug,) in rows:                                       # step 2: fan out, biggest first
        try:
            process_clinic(c, slug, extract, translate, verify)  # in prod: a Temporal activity
        except Exception as e:                                 # one clinic failing never stops the run
            audit(c, "error", slug, str(e))
            c.commit()

# ---- prompts: the model proposes, deterministic keys decide ------------------
# Glossary pins the English procedure vocabulary. Search and the dedup key both
# depend on "코재수술" landing on the same string every run, not a synonym.
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

EXTRACT_PROMPT = """\
You are extracting patient reviews from the HTML of one clinic detail page.

Return a JSON array, one object per distinct patient review:
  {"surgeon": str, "procedure": str, "dt": str, "text_ko": str}

Rules, in priority order:
1. Verbatim only. Copy surgeon, procedure, and review text exactly as printed,
   Korean included. Do not translate, normalize, or expand abbreviations here;
   normalization happens downstream against a license registry.
2. Never guess. Review names no surgeon -> surgeon is "". No procedure stated
   -> procedure is "". These fields feed the dedup key; a guessed value
   publishes the same review twice under two identities.
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
def real_extract(llm) -> Callable[[bytes], Iterable[RawReview]]:
    def extract(html: bytes) -> Iterable[RawReview]:
        return llm.json(prompt=EXTRACT_PROMPT, html=html, schema=RawReview)  # schema-validated
    return extract

if __name__ == "__main__":
    conn = db()
    if "--dry-run" in sys.argv:                                # proves the seed layer runs, no key
        for cl in parse_index(fetch(INDEX)):
            print(f"{cl.slug:28} | {cl.name:34} | {cl.rating} | {cl.reviews} | "
                  f"{cl.district} | needs_review={cl.name_needs_review}")
    else:
        try:
            from impls import YourLLM, registry                # <- wire real impls in impls.py
        except ImportError:
            raise SystemExit("no impls.py: define YourLLM (json/text) and registry (check)")
        llm = YourLLM()

        def translate(ko: str) -> str:
            return llm.text(prompt=TRANSLATE_PROMPT, input=ko)

        def verify(surgeon: str, slug: str) -> Verdict:
            return registry.check(surgeon, slug)

        sync(conn, real_extract(llm), translate, verify)
