"""Offline tests for the dry-run edit builder (no network)."""
import pytest
from refs.edit import Skip, add_author_params, apply_to_page, choose_params, edit_summary, publication, split_name, verify_template
from collections import Counter

ONE = [("last", "Smith"), ("first", "Jane")]


def style(**kw):
    return Counter(kw)


def page_apply(text, raw, names):
    return apply_to_page(text, [dict(citation_id=1, raw=raw, names=names)])


# ---------- spacing styles ----------

# Authors always go before the first named parameter, whatever it is.
@pytest.mark.parametrize("raw, expected", [
    ("{{cite news |url=http://x |title=T |work=W}}",
     "{{cite news |last=Smith |first=Jane |url=http://x |title=T |work=W}}"),
    ("{{cite news|url=http://x|title=T|work=W}}",
     "{{cite news|last=Smith|first=Jane|url=http://x|title=T|work=W}}"),
    ("{{cite news | url = http://x | title = T | work = W }}",
     "{{cite news | last = Smith | first = Jane | url = http://x | title = T | work = W }}"),
    ("{{Cite web\n | url = http://x\n | title = T\n | website = W\n}}",
     "{{Cite web\n | last = Smith\n | first = Jane\n | url = http://x\n | title = T\n | website = W\n}}"),
    ("{{cite web\n|url=http://x\n|title=T\n}}",
     "{{cite web\n|last=Smith\n|first=Jane\n|url=http://x\n|title=T\n}}"),
    ("{{cite news |title=T |url=http://x}}",
     "{{cite news |last=Smith |first=Jane |title=T |url=http://x}}"),
    # VisualEditor order without an author: authors go before |date=, where VisualEditor puts them
    ("{{Cite web |date=1 May 2020 |title=T |url=http://x |website=W}}",
     "{{Cite web |last=Smith |first=Jane |date=1 May 2020 |title=T |url=http://x |website=W}}"),
    ("{{cite web |url=http://x |website=W}}",
     "{{cite web |last=Smith |first=Jane |url=http://x |website=W}}"),
    # the separator between parameters is copied, not the one after the template name
    ("{{cite web|url=http://x |title=T |website=W}}",
     "{{cite web|last=Smith |first=Jane |url=http://x |title=T |website=W}}"),
    ("{{cite web|url=http://x\n|title=T\n}}",
     "{{cite web|last=Smith\n|first=Jane\n|url=http://x\n|title=T\n}}"),
    # a single parameter: only the separator after the template name is there to copy
    ("{{cite web |url=http://x}}",
     "{{cite web |last=Smith |first=Jane |url=http://x}}"),
])
def test_spacing(raw, expected):
    assert add_author_params(raw, ONE) == expected


# ---------- comments and nested templates ----------

def test_comment_inside_template_kept():
    raw = "{{cite news |url=http://x <!-- dead? --> |title=T |date=1 May 2020}}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |url=http://x <!-- dead? --> |title=T |date=1 May 2020}}"


def test_comment_after_template_name_kept():
    raw = "{{cite web <!-- note --> |url=http://x |title=T}}"
    assert add_author_params(raw, ONE) == "{{cite web <!-- note --> |last=Smith |first=Jane |url=http://x |title=T}}"


def test_nested_templates_in_values():
    raw = "{{cite news |url=http://x |title={{lang|fr|Le titre}} |date={{date|2020-05-01}} |work=W}}"
    new = add_author_params(raw, ONE)
    assert new == "{{cite news |last=Smith |first=Jane |url=http://x |title={{lang|fr|Le titre}} |date={{date|2020-05-01}} |work=W}}"


def test_nested_template_in_first_value():
    raw = "{{cite news |date={{date|2020-05-01}} |title=T |url=http://x}}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |date={{date|2020-05-01}} |title=T |url=http://x}}"


def test_pipe_escape_in_title():
    raw = "{{cite web |url=http://x |title=A {{!}} B |website=W}}"
    assert add_author_params(raw, ONE) == "{{cite web |last=Smith |first=Jane |url=http://x |title=A {{!}} B |website=W}}"


# ---------- existing author parameters ----------

def test_blank_placeholders_filled_in_place():
    raw = "{{cite news |last= |first= |url=http://x |title=T}}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |url=http://x |title=T}}"


def test_blank_placeholders_spaced():
    raw = "{{cite news\n| last  = \n| first = \n| url = http://x\n| title = T\n}}"
    assert add_author_params(raw, ONE) == "{{cite news\n| last  = Smith\n| first = Jane\n| url = http://x\n| title = T\n}}"


def test_mismatched_placeholder_removed_then_added():
    assert add_author_params("{{cite news |author= |url=http://x |title=T}}", ONE) == \
        "{{cite news |last=Smith |first=Jane |url=http://x |title=T}}"


def test_stray_numbered_slots_removed():
    # Jared Kushner / New York Post: empty slots for a 3rd and 4th author, and no others
    raw = "{{Cite web |last3= |first3= |last4= |first4= |date=September 18, 2013|title=T |url=http://x |url-status=live }}"
    new = add_author_params(raw, ONE)
    assert new == "{{Cite web |last=Smith|first=Jane|date=September 18, 2013|title=T |url=http://x |url-status=live }}"
    verify_template(raw, new, ONE)


def test_matching_slots_filled_and_extra_slots_removed():
    raw = "{{cite news |last= |first= |last2= |first2= |url=http://x |title=T}}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |url=http://x |title=T}}"


def test_stray_slots_removed_in_multiline_template():
    raw = "{{cite news\n| url = http://x\n| title = T\n| last2 = \n| first2 = \n}}"
    new = add_author_params(raw, ONE)
    assert new == "{{cite news\n| last = Smith\n| first = Jane\n| url = http://x\n| title = T\n}}"
    verify_template(raw, new, ONE)


def test_empty_non_name_slots_left_alone():
    raw = "{{cite news |url=http://x |title=T |interviewer= }}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |url=http://x |title=T |interviewer= }}"


def test_verify_refuses_removal_of_a_filled_or_non_name_parameter():
    with pytest.raises(Skip):
        verify_template("{{cite news |url=http://x |title=T}}", "{{cite news |last=Smith |first=Jane |url=http://x}}", ONE)
    with pytest.raises(Skip):
        verify_template("{{cite news |url=http://x |via= }}", "{{cite news |last=Smith |first=Jane |url=http://x}}", ONE)


def test_page_with_stray_slots_builds():
    raw = "{{Cite web |last3= |first3= |url=http://x |title=T}}"
    new, res = page_apply(f"A.<ref>{raw}</ref>", raw, ["Jane Smith"])
    assert res[0]["status"] == "ok" and "{{Cite web |last=Smith |first=Jane |url=http://x |title=T}}" in new


def test_existing_author_skip():
    with pytest.raises(Skip):
        add_author_params("{{cite news |author=Someone |url=http://x |title=T}}", ONE)


def test_not_stated_comment_skip():
    with pytest.raises(Skip):
        add_author_params("{{cite news |author=<!--Not stated--> |url=http://x |title=T}}", ONE)


# ---------- page-level: named, reused, list-defined, duplicates ----------

CITE = "{{cite news |url=http://x |title=T |work=W}}"
NEW = "{{cite news |last=Smith |first=Jane |url=http://x |title=T |work=W}}"


def test_named_ref_and_reuse():
    text = f"Alpha.<ref name=\"a\">{CITE}</ref> Beta.<ref name=\"a\" /> Gamma.<ref name=a/>\n\n==References==\n{{{{reflist}}}}"
    new, res = page_apply(text, CITE, ["Jane Smith"])
    assert res[0]["status"] == "ok" and res[0]["occurrences"] == 1
    assert new == text.replace(CITE, NEW)
    assert new.count('<ref name="a" />') == 1  # reuses untouched


def test_list_defined_reference():
    text = "Alpha.<ref name=a/>\n\n==References==\n{{reflist|refs=\n<ref name=a>" + CITE + "</ref>\n<ref name=b>{{cite web |url=http://y |title=U}}</ref>\n}}"
    new, res = page_apply(text, CITE, ["Jane Smith"])
    assert res[0]["status"] == "ok"
    assert new == text.replace(CITE, NEW)


def test_identical_duplicates_all_updated():
    text = f"A.<ref>{CITE}</ref> B.<ref>{CITE}</ref>"
    new, res = page_apply(text, CITE, ["Jane Smith"])
    assert res[0]["occurrences"] == 2 and new == text.replace(CITE, NEW)


def test_citation_inside_other_template():
    text = "{{Infobox person\n| name = X\n| spouse = Y<ref>" + CITE + "</ref>\n}}\nBody."
    new, res = page_apply(text, CITE, ["Jane Smith"])
    assert res[0]["status"] == "ok" and new == text.replace(CITE, NEW)


def test_citation_changed_since_scan():
    new, res = page_apply("A.<ref>{{cite news |url=http://x |title=T2}}</ref>", CITE, ["Jane Smith"])
    assert res[0]["status"] == "skip" and "edited on Wikipedia since the scan" in res[0]["reason"] \
        and new == "A.<ref>{{cite news |url=http://x |title=T2}}</ref>"


# ---------- article name style (WP:CITEVAR) ----------

def test_style_last_first_default():
    assert choose_params(["Jane Smith"], style()) == ONE


def test_style_numbered_when_article_uses_last1():
    assert choose_params(["Jane Smith"], style(last1=5, last=1)) == [("last1", "Smith"), ("first1", "Jane")]


def test_style_multiple_authors_numbered():
    assert choose_params(["Ben Kochman", "Larry Celona"], style(last=3)) == [
        ("last1", "Kochman"), ("first1", "Ben"), ("last2", "Celona"), ("first2", "Larry")]


def test_style_author_full_name():
    assert choose_params(["Jane Smith"], style(author=6, last=2)) == [("author", "Jane Smith")]


def test_style_vauthors_skips():
    with pytest.raises(Skip):
        choose_params(["Jane Smith"], style(vauthors=4))


def test_article_style_detected_on_page():
    text = ("{{cite news |author=A B |url=http://1 |title=1}}{{cite news |author=C D |url=http://2 |title=2}}"
            "{{cite web |last=E |first=F |url=http://3 |title=3}}" + CITE)
    new, res = page_apply(text, CITE, ["Jane Smith"])
    assert res[0]["params"] == [("author", "Jane Smith")]


@pytest.mark.parametrize("name, parts", [
    ("Jane Smith", ("Smith", "Jane")),
    ("Jane A. Smith", ("Smith", "Jane A.")),
    ("Ludwig van Beethoven", ("van Beethoven", "Ludwig")),
    ("Martin Luther King Jr.", ("King", "Martin Luther Jr.")),
    ("Emily-Ann Elliott", ("Elliott", "Emily-Ann")),
    ("Kevin O'Keeffe", ("O'Keeffe", "Kevin")),
])
def test_split_name(name, parts):
    assert split_name(name) == parts


# ---------- reviewer-chosen name split ----------

def test_split_override_family_name_first():
    assert choose_params(["Choi Won-suk"], style(), [["Choi", "Won-suk"]]) == [("last", "Choi"), ("first", "Won-suk")]
    assert choose_params(["Lee Nian Tjoe"], style(last1=3), [["Lee", "Nian Tjoe"]]) == [("last1", "Lee"), ("first1", "Nian Tjoe")]


@pytest.mark.parametrize("split", [["Choi", "Wonsuk"], ["Choi", ""], ["", "Choi Won-suk"], ["Choi", "Won-suk Kim"]])
def test_split_override_must_use_the_names_words(split):
    with pytest.raises(Skip):
        choose_params(["Choi Won-suk"], style(), [split])


def test_split_override_applies_to_page():
    raw = "{{cite news |url=http://x |title=T}}"
    new, res = apply_to_page(f"<ref>{raw}</ref>", [dict(citation_id=1, raw=raw, names=["Choi Won-suk"], split=[["Choi", "Won-suk"]])])
    assert res[0]["status"] == "ok" and "|last=Choi |first=Won-suk" in new


# ---------- edit summary ----------

@pytest.mark.parametrize("raw, source", [
    ("{{cite news |url=http://x |title=T |work=[[The Guardian]]}}", "The Guardian"),
    ("{{cite web |url=https://www.bbc.co.uk/sport/1 |title=T |website=''BBC Sport''}}", "BBC Sport"),
    ("{{cite news |url=http://x |title=T |newspaper=[[The Times|Times]] |publisher=News UK}}", "Times"),
    ("{{cite web |url=https://www.thehindu.com/a |title=T}}", "thehindu.com"),
    ("{{cite web |url=https://www.thehindu.com/a |title=T |work={{lang|ta|x}}}}", "thehindu.com"),  # template only: domain
    ("{{cite news |title=T |publisher=Reuters}}", "Reuters"),
    ("{{cite news |title=T}}", ""),
])
def test_publication(raw, source):
    assert publication(raw) == source


def ok(names, source):
    return dict(names=names, source=source)


@pytest.mark.parametrize("results, summary", [
    ([ok(["Jane Smith"], "The Guardian")],
     "Add author to 1 citation, from the source's byline: Jane Smith (The Guardian)"),
    ([ok(["Jane Smith", "John Doe"], "BBC News")],
     "Add authors to 1 citation, from the source's byline: Jane Smith, John Doe (BBC News)"),
    ([ok(["Jane Smith"], "The Guardian"), ok(["John Doe"], "Variety")],
     "Add authors to 2 citations, from the sources' bylines: Jane Smith (The Guardian); John Doe (Variety)"),
    ([ok(["Jane Smith"], "")],
     "Add author to 1 citation, from the source's byline: Jane Smith"),
    ([ok([f"Author {i}"], "W") for i in range(6)],
     "Add authors to 6 citations, from the sources' bylines"),
    ([ok(["A" * 150], "W"), ok(["B" * 150], "W")],
     "Add authors to 2 citations, from the sources' bylines"),
])
def test_edit_summary(results, summary):
    assert edit_summary(results) == summary


def test_summary_from_page():
    raw = "{{cite news |url=http://x |title=T |work=[[The Guardian]]}}"
    new, res = apply_to_page(f"A.<ref>{raw}</ref>", [dict(citation_id=1, raw=raw, names=["Jane Smith"])])
    assert edit_summary(res) == "Add author to 1 citation, from the source's byline: Jane Smith (The Guardian)"


def test_blank_first_parameter_lends_no_space_to_the_new_values():
    raw = "{{cite news |last1= |first1= |date= |title=T |url=http://x}}"
    assert add_author_params(raw, ONE) == "{{cite news |last=Smith |first=Jane |date= |title=T |url=http://x}}"
    raw = "{{cite news\n| date = \n| title = T\n}}"
    assert add_author_params(raw, ONE) == "{{cite news\n| last = Smith\n| first = Jane\n| date = \n| title = T\n}}"


# ---------- citations changed on Wikipedia since the scan ----------

SCANNED = "{{cite news |url=https://news.example.com/a |title=T}}"


@pytest.mark.parametrize("live, reason, already", [
    ("{{cite news |last=Weiss |first=Lois |url=https://www.news.example.com/a/ |title=T}}",
     "the citation now credits the same author (|last=Weiss |first=Lois)", True),
    ("{{cite news |last=Kaufman |first=Gil |url=https://news.example.com/a |title=T}}",
     "the citation now credits an author (|last=Kaufman |first=Gil)", True),
    ("{{cite news |url=https://news.example.com/a |title=T |access-date=1 May 2026}}", "the citation was edited on Wikipedia since the scan", False),
    ("{{cite news |url=https://other.example.com/b |title=U}}", "the citation is no longer on the page", False)])
def test_says_why_a_scanned_citation_is_gone(live, reason, already):
    _, res = page_apply(f"A.<ref>{live}</ref>", SCANNED, ["Lois Weiss"])
    assert res[0]["status"] == "skip" and res[0]["reason"] == reason and res[0]["already"] is already
