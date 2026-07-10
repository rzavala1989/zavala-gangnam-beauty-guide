# Gangnam Beauty Guide review pipeline

Single-file pipeline (`gbg.py`) that turns Korean patient reviews from [gangnambeautyguide.com](https://gangnambeautyguide.com/en/) into a searchable English dataset. It seeds from one server-rendered fetch of the clinic index (139 clinics, no API), then crawls each clinic page to extract, translate, dedupe, and trust-gate reviews.

## Run

```bash
pip install requests beautifulsoup4
python gbg.py --dry-run    # parse the live index, print every clinic; no API key, no database
python gbg.py              # full sync; requires impls.py (see wiring below)
```

## Pipeline

1. Seed: one SSR fetch of `/en/clinics/`; `parse_index` returns slug, name, rating, review count, district, and verified flag per clinic.
2. Fan out: clinics crawl biggest-first from the live index; a clinic that fails writes an audit row and never stops the run.
3. Extract: the LLM returns typed `RawReview` records under `EXTRACT_PROMPT` (verbatim only, never guess, no relative-date math).
4. Translate: Korean to English under `TRANSLATE_PROMPT`, procedure terms pinned by `PROCEDURE_GLOSSARY`; the Korean original is kept.
5. Dedup: review identity is (slug, whitespace-normalized text hash), so re-crawls and syndicated copies skip.
6. Trust: surgeon names cross-check against a license registry; the site's own "verified" badge feeds the check and never decides it. Unconfirmable names go to the human queue.
7. Checkpoint: (url, content hash) commits only after a full pass, so re-runs skip unchanged pages and never double-publish.

## Design decisions

The judgment that matters more than the code:

- The slug from the SSR index is clinic identity. Clinics are never fuzzy-merged on romanized names; the data proved the trap ("OZ" collisions, names that parse as "(2,767)"). Deterministic key first, the model only ever proposes.
- Review identity is (slug, text hash). The body is the one field the extractor can never leave blank, so anonymous reviews can't collide, and metadata drift (a relative date later rendering as absolute, a surgeon name appearing on a re-crawl) can't mint a duplicate. An edited review is a new review by design.
- Automation is gated by reversibility. A translation is cheap to undo, so it auto-publishes. A "verified" badge or an entity merge is not, so it needs registry proof and an audit row.
- Human corrections stick. The clinic upsert never overwrites a name a person has fixed, and a repaired name is flagged once, on first sight.

## Wiring impls.py

The full sync imports `YourLLM` and `registry` from `impls.py`:

```python
class YourLLM:
    def json(self, prompt, html, schema): ...   # schema-validated extraction
    def text(self, prompt, input): ...          # translation

class registry:
    @staticmethod
    def check(surgeon, slug): ...               # returns a Verdict
```

## Storage

SQLite (`gbg.db`), five tables:

- `checkpoint`: (url, content hash) pairs that make re-runs idempotent
- `clinic`: one row per slug from the index
- `review`: published reviews, Korean original beside the English translation
- `hitl`: the human review queue (repaired names, unconfirmable surgeons)
- `audit`: every publish and every per-clinic error

Guidance for working on this repo with Claude Code lives in [CLAUDE.md](CLAUDE.md).
