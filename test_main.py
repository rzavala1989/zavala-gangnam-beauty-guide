# Offline suite: canned index cards, monkeypatched fetch, in-memory sqlite.
# No network, no LLM. The live check is `python main.py --dry-run`.

import pytest

import main


def card(slug, *, name=None, stars=5, rating=None, count=None, district=None,
         verified=False, extra=None):
    lines = ["Photo via Google Maps"]
    if name:
        lines.append(name)
    if extra:
        lines.append(extra)
    lines += ["★"] * stars
    if rating:
        lines.append(rating)
    if count:
        lines += ["(", count, ")"]
    if district:
        lines.append(f"7F, 596, Gangnam-daero, {district}, Seoul, Republic of Korea")
    if verified:
        lines += ["✓", "Verified clinic"]
    spans = "".join(f"<span>{ln}</span>" for ln in lines)
    return f'<a href="/en/clinics/{slug}">{spans}</a>'


def index_of(*cards):
    return "".join(cards).encode()


def fetch_stub(index_body, detail_log):
    def f(url):
        if url == main.INDEX:
            return index_body
        detail_log.append(url)
        return url.encode()
    return f


# ---- parse_index -------------------------------------------------------------

def test_slug_ignores_fragments_subpaths_and_queries():
    html = (
        '<a href="/en/clinics/oz-clinic">x</a>'
        '<a href="/en/clinics/oz-clinic#reviews">x</a>'
        '<a href="/en/clinics/oz-clinic/photos">x</a>'
        '<a href="https://gangnambeautyguide.com/en/clinics/abs-ps?utm=1">x</a>'
        '<a href="/en/clinics/">x</a>'
    ).encode()
    assert [c.slug for c in main.parse_index(html)] == ["oz-clinic", "abs-ps"]


def test_full_card_parses_every_field():
    html = index_of(card("honesty-ps", name="Honesty Plastic Surgery", rating="4.5",
                         count="21,120", district="Gangnam-gu", verified=True))
    (c,) = main.parse_index(html)
    assert (c.name, c.rating, c.reviews, c.district) == \
        ("Honesty Plastic Surgery", 4.5, 21120, "Gangnam-gu")
    assert c.verified and not c.name_needs_review


def test_rating_anchors_to_star_glyph():
    html = index_of(card("a-ps", name="A Plastic Surgery", rating="4.8",
                         extra="1.2 km from Gangnam Station"))
    (c,) = main.parse_index(html)
    assert c.rating == 4.8


def test_nameless_card_falls_back_to_slug_and_flags():
    html = index_of(card("365mc-hospital", stars=0, count="2,767"))
    (c,) = main.parse_index(html)
    assert c.name == "365Mc Hospital"
    assert c.name_needs_review
    assert c.reviews == 2767


def test_unrated_card_yields_none_rating_and_count():
    html = index_of(card("new-ps", name="New Plastic Surgery", stars=0))
    (c,) = main.parse_index(html)
    assert c.rating is None and c.reviews is None


# ---- process_clinic ----------------------------------------------------------

def anonymous_registry(surgeon, slug):
    raise AssertionError("registry must not be called for anonymous reviews")


def test_distinct_anonymous_reviews_both_publish(monkeypatch):
    c = main.db(":memory:")
    reviews = [main.RawReview("", "쌍수", "", "첫번째 후기"),
               main.RawReview("", "", "", "두번째 후기")]
    monkeypatch.setattr(main, "fetch", lambda url: b"crawl-1")
    main.process_clinic(c, "oz", lambda body: reviews, lambda ko: f"EN({ko})",
                        anonymous_registry)
    rows = c.execute("SELECT procedure FROM review ORDER BY procedure DESC").fetchall()
    assert rows == [("double eyelid surgery",), ("",)]
    assert c.execute("SELECT COUNT(*) FROM hitl").fetchone()[0] == 0


def test_metadata_drift_never_republishes(monkeypatch):
    c = main.db(":memory:")
    batches = iter([
        [main.RawReview("", "쌍수", "", "첫번째 후기"), main.RawReview("", "", "", "두번째 후기")],
        [main.RawReview("김원장", "쌍꺼풀", "2026-06-15", "첫번째  후기"),
         main.RawReview("", "", "", "두번째 후기")],
    ])
    bodies = iter([b"crawl-1", b"crawl-2"])
    monkeypatch.setattr(main, "fetch", lambda url: next(bodies))
    for _ in range(2):
        main.process_clinic(c, "oz", lambda body: next(batches), lambda ko: "EN",
                            lambda s, g: main.Verdict(False, True))
    assert c.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM audit WHERE action='publish_review'").fetchone()[0] == 2


def test_unconfirmed_named_surgeon_routes_to_hitl(monkeypatch):
    c = main.db(":memory:")
    reviews = [main.RawReview("김원장", "코성형", "2026-01-01", "후기 본문")]
    monkeypatch.setattr(main, "fetch", lambda url: b"crawl-1")
    main.process_clinic(c, "oz", lambda body: reviews, lambda ko: "EN",
                        lambda s, g: main.Verdict(False, True))
    assert c.execute("SELECT kind, payload FROM hitl").fetchall() == [("verify_surgeon", "김원장")]
    assert c.execute("SELECT verified_surgeon FROM review").fetchone()[0] == 0


def test_unchanged_page_skips_extraction(monkeypatch):
    c = main.db(":memory:")
    calls = []
    monkeypatch.setattr(main, "fetch", lambda url: b"same-body")
    def extract(body):
        calls.append(body)
        return []
    main.process_clinic(c, "oz", extract, None, None)
    main.process_clinic(c, "oz", extract, None, None)
    assert len(calls) == 1


def test_failed_pass_leaves_no_checkpoint_and_retries_clean(monkeypatch):
    c = main.db(":memory:")
    reviews = [main.RawReview("", "", "", "좋은 후기"), main.RawReview("", "", "", "나쁜 후기")]
    monkeypatch.setattr(main, "fetch", lambda url: b"crawl-1")
    def flaky(ko):
        if "나쁜" in ko:
            raise RuntimeError("translator down")
        return "EN"
    with pytest.raises(RuntimeError):
        main.process_clinic(c, "oz", lambda body: reviews, flaky, anonymous_registry)
    assert c.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0] == 0
    main.process_clinic(c, "oz", lambda body: reviews, lambda ko: "EN", anonymous_registry)
    assert c.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM audit WHERE action='publish_review'").fetchone()[0] == 2


# ---- sync ---------------------------------------------------------------------

def test_human_name_correction_survives_recrawl(monkeypatch):
    c = main.db(":memory:")
    pages = iter([index_of(card("365mc-hospital", stars=0, count="2,767")),
                  index_of(card("365mc-hospital", stars=0, count="2,768"))])
    monkeypatch.setattr(main, "fetch",
                        lambda url: next(pages) if url == main.INDEX else b"detail")
    main.sync(c, lambda body: [], None, None)
    c.execute("UPDATE clinic SET name='365mc Hospital', name_needs_review=0")
    c.commit()
    main.sync(c, lambda body: [], None, None)
    assert c.execute("SELECT name, name_needs_review FROM clinic").fetchall() == \
        [("365mc Hospital", 0)]
    assert c.execute("SELECT COUNT(*) FROM hitl").fetchone()[0] == 1


def test_fan_out_is_biggest_first_and_skips_delisted(monkeypatch):
    c = main.db(":memory:")
    c.execute("INSERT INTO clinic VALUES('ghost-ps','Ghost',NULL,NULL,NULL,0,0)")
    c.commit()
    idx = index_of(card("small-ps", name="Small PS", count="10"),
                   card("big-ps", name="Big PS", count="20,000"),
                   card("new-ps", name="New PS", stars=0))
    crawled = []
    monkeypatch.setattr(main, "fetch", fetch_stub(idx, crawled))
    main.sync(c, lambda body: [], None, None)
    assert crawled == [main.INDEX + s for s in ("big-ps", "small-ps", "new-ps")]
    assert c.execute("SELECT 1 FROM clinic WHERE slug='ghost-ps'").fetchone()


def test_one_failing_clinic_never_stops_the_run(monkeypatch):
    c = main.db(":memory:")
    idx = index_of(card("bad-ps", name="Bad PS", count="9"),
                   card("good-ps", name="Good PS", count="5"))
    monkeypatch.setattr(main, "fetch", fetch_stub(idx, []))
    def extract(body):
        if b"bad-ps" in body:
            raise RuntimeError("boom")
        return []
    main.sync(c, extract, None, None)
    assert c.execute("SELECT slug FROM audit WHERE action='error'").fetchall() == [("bad-ps",)]
    assert c.execute("SELECT 1 FROM checkpoint WHERE url=?",
                     (main.INDEX + "good-ps",)).fetchone()


# ---- helpers ------------------------------------------------------------------

def test_normalize_procedure_pins_synonyms():
    assert main.normalize_procedure(" 쌍수 ") == "double eyelid surgery"
    assert main.normalize_procedure("쌍꺼풀") == "double eyelid surgery"
    assert main.normalize_procedure("custom procedure") == "custom procedure"


def test_strip_boilerplate_keeps_reviews_drops_scripts():
    body = ("<html><head><style>.x{color:red}</style></head><body>"
            "<script>var secret=1;</script><div>리뷰 내용</div></body></html>").encode()
    out = main.strip_boilerplate(body)
    assert "리뷰 내용" in out
    assert "secret" not in out and "color:red" not in out


def test_checkpoint_roundtrip():
    c = main.db(":memory:")
    assert not main.unchanged(c, "u", b"v1")
    main.checkpoint(c, "u", b"v1")
    assert main.unchanged(c, "u", b"v1")
    assert not main.unchanged(c, "u", b"v2")
