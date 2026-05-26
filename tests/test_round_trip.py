#!/usr/bin/env python3
"""
test_round_trip.py — exercise prompt + generate (stub backend) without
touching a remote.

Run from the repo root:
  python3 tests/test_round_trip.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import generate as gen
import prompt as pr


def test_stub_produces_summary():
    raw = json.loads((REPO / "tests/fixtures/morrissey.json").read_text())
    job = gen.job_from_pending_json(raw)
    md = gen.generate(job, {"backend": "stub"})
    assert "**morrissey**" in md.lower(), "expected head term bolded"
    assert md.count("\n") >= 3, "expected multi-line output"
    assert "stub" in md.lower(), "stub identifier should be in output"
    return md


def test_parse_summary_rejects_preamble():
    try:
        pr.parse_summary("Here is your summary: foo.", "foo")
    except ValueError:
        return
    raise AssertionError("expected ValueError on preamble")


def test_parse_summary_auto_bolds():
    out = pr.parse_summary(
        "Morrissey is an English singer and songwriter. He fronted the "
        "Smiths from 1982 to 1987. He is widely known as an outspoken "
        "vegan and animal rights advocate. He has many solo albums and "
        "continues to tour today across many countries and regions. "
        "He was born in 1959 in the Manchester area of England.",
        "Morrissey",
    )
    assert "**Morrissey**" in out, "auto-bold should have wrapped head term"


def test_parse_summary_too_short():
    try:
        pr.parse_summary("**foo** is a thing.", "foo")
    except ValueError as e:
        assert "too short" in str(e)
        return
    raise AssertionError("expected ValueError on too-short output")


def test_cap_doc_text_at_utf8_boundary():
    s = "a" * 10 + "é" * 100
    out = pr.cap_doc_text(s, 20)
    # Must end cleanly — no replacement chars or undecoded bytes
    out.encode("utf-8").decode("utf-8")


def test_build_prompt_includes_query_and_docs():
    raw = json.loads((REPO / "tests/fixtures/morrissey.json").read_text())
    job = gen.job_from_pending_json(raw)
    system, user = pr.build_prompt(job, per_doc_cap_bytes=2000)
    assert '"morrissey"' in user.lower()
    assert "=== DOC 1: Morrissey ===" in user
    assert "=== DOC 2: The Smiths ===" in user
    assert "knowledge-panel" in system or "summary" in system.lower()


# PRD-029 day-roundup mode.

def test_day_roundup_job_parses():
    raw = json.loads((REPO / "tests/fixtures/2026-05-25-day.json").read_text())
    job = gen.job_from_pending_json(raw)
    assert job.mode == "day-roundup"
    assert job.event_date == "2026-05-25"
    assert len(job.docs) == 5
    # category should survive into JobDoc.
    cats = sorted({d.category for d in job.docs})
    assert "world" in cats and "tech" in cats and "sport" in cats


def test_day_roundup_prompt_lists_events():
    raw = json.loads((REPO / "tests/fixtures/2026-05-25-day.json").read_text())
    job = gen.job_from_pending_json(raw)
    system, user = pr.build_day_prompt(job)
    assert "what happened today" in system.lower() or "roundup" in system.lower()
    assert "2026-05-25" in user
    assert "EVENT 1: Iran" in user
    assert "[world]" in user
    assert "[tech]" in user


def test_day_roundup_stub_passes_validator():
    raw = json.loads((REPO / "tests/fixtures/2026-05-25-day.json").read_text())
    job = gen.job_from_pending_json(raw)
    md = gen.generate(job, {"backend": "stub"})
    # Must round-trip through the day-roundup validator.
    out = pr.parse_day_summary(md)
    assert out, "stub must produce parseable day-roundup output"
    # At least 3 bolded entities per the prompt rules.
    assert out.count("**") >= 6, "expected >= 3 bolded entities"


def test_parse_day_summary_rejects_bullets():
    # Long enough to pass the word-count gate (>= 40 words), so the
    # bullet check actually fires.
    body = " ".join([
        "On 25 May 2026, **Iran** reached a phased IAEA framework",
        "after talks in Vienna; **Ukraine** intercepted a record drone",
        "wave overnight; in tech, **OpenAI** launched a long-context",
        "tier and **Apple** beat quarterly expectations; **Manchester",
        "City** sealed a fifth straight Premier League title with a",
        "1-1 draw, a fitting cap to a long and eventful season.",
    ])
    md = body + "\n- a stray bullet should be rejected"
    try:
        pr.parse_day_summary(md)
    except ValueError as e:
        assert "bullet" in str(e).lower(), f"unexpected reason: {e}"
        return
    raise AssertionError("expected ValueError on bullets in day roundup")


def main():
    tests = [
        test_stub_produces_summary,
        test_parse_summary_rejects_preamble,
        test_parse_summary_auto_bolds,
        test_parse_summary_too_short,
        test_cap_doc_text_at_utf8_boundary,
        test_build_prompt_includes_query_and_docs,
        test_day_roundup_job_parses,
        test_day_roundup_prompt_lists_events,
        test_day_roundup_stub_passes_validator,
        test_parse_day_summary_rejects_bullets,
    ]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            fails += 1
    if fails:
        sys.exit(f"\n{fails}/{len(tests)} failed")
    print(f"\nAll {len(tests)}/{len(tests)} passed.")


if __name__ == "__main__":
    main()
