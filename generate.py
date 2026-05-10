"""
generate.py — call the local model for one job, return markdown.

Two backends today:
  stub   — deterministic placeholder for round-trip testing.
  ollama — POST to a local ollama server. Retries on timeout/parse errors.

Future backends (llama.cpp HTTP, mlx, openrouter, etc.) plug in here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from prompt import (
    Job,
    JobDoc,
    build_prompt,
    parse_summary,
    stub_summary,
)


class GenerationError(Exception):
    """Worker treats this as a soft failure — write to errors/ and move on."""


def job_from_pending_json(raw: dict) -> Job:
    """Turn a parsed pending/<query_norm>.json dict into a Job."""
    if raw.get("schema_version") not in (None, 1):
        raise GenerationError(f"unknown schema_version {raw.get('schema_version')!r}")
    if not raw.get("query") or not raw.get("query_norm"):
        raise GenerationError("missing query / query_norm")
    docs = []
    for r in raw.get("results", [])[:5]:  # hard top-5 cap on input
        docs.append(JobDoc(
            rank=int(r.get("rank", len(docs) + 1)),
            title=r.get("title") or r.get("docno") or "(untitled)",
            text=r.get("text") or "",
        ))
    return Job(query=raw["query"], query_norm=raw["query_norm"], docs=docs)


def generate(job: Job, cfg: dict) -> str:
    """Returns markdown for the job. Raises GenerationError on failure."""
    backend = cfg.get("backend", "stub")
    if backend == "stub":
        return stub_summary(job)
    if backend == "ollama":
        return _generate_ollama(job, cfg)
    raise GenerationError(f"unknown backend {backend!r}")


def _generate_ollama(job: Job, cfg: dict) -> str:
    url = cfg["ollama_url"].rstrip("/") + "/api/chat"
    model = cfg["ollama_model"]
    per_doc_cap = cfg.get("per_doc_cap_bytes", 12000)
    max_tokens = cfg.get("max_output_tokens", 400)

    system, user = build_prompt(job, per_doc_cap)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": 0.2,
            "num_predict": max_tokens,
        },
        "stream": False,
    }

    last_err = None
    for attempt in range(3):
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            raw = (data.get("message") or {}).get("content", "")
            return parse_summary(raw, job.query)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 + attempt * 4)
    raise GenerationError(f"ollama failed after retries: {last_err}")
