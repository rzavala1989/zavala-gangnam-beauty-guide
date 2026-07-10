# CLAUDE.md

`main.py` is a self-contained review syndication pipeline for gangnambeautyguide.com. README.md covers how it works. This file is the standing contract for coding agents working in this repo: the invariants an edit must preserve, how to prove it preserved them, and the live-site facts a fresh session would otherwise rediscover the hard way.

## Verifying changes

`.venv/bin/python main.py --dry-run` hits the live index and prints one row per clinic (139 as of July 2026). It must need no API key and must not create `gbg.db`. For logic changes, drive `process_clinic` or `sync` against an in-memory db (`main.db(":memory:")`) with `main.fetch` monkeypatched; no network or LLM required.

## Invariants to preserve

- Review identity is (slug, whitespace-normalized text hash), joined with `\x00`. Do not add surgeon, procedure, or date back into the key: empty-string fields collide anonymous reviews, and drifting fields re-publish the same review.
- The clinic upsert never updates `name` or `name_needs_review` on existing rows. Human corrections have to survive re-crawls. Flag once-ness lives in the `hitl` primary key (kind, slug, payload), never in caller bookkeeping.
- Fan-out iterates the freshly parsed index, never the clinic table, so delisted clinics stop being crawled but keep their published rows.
- `verify()` is skipped for anonymous reviews; an empty surgeon must never reach the registry or the hitl queue.

## Live-site parsing facts

- The index markup splits review counts across elements, so joined card text reads `( 21,120 )` with spaces. Regexes must tolerate whitespace inside the parens.
- Rating extraction must anchor to `★`; unanchored decimal matching grabs distances like "1.2 km".
- Lone punctuation and rating fragments (`(`, `)`, `4.5`, `2,767`) must match `NUMERIC` or an off-layout card picks one as the clinic name without flagging it.
- Roughly 10 of the 139 clinics have no rating or review count on the index. `None` there is correct data, a parse failure would show as junk names or a wrong row count.
