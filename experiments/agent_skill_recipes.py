#!/usr/bin/env python3
"""Gemma 3 4B + Python browser-harness with 3 SKILL-BACKED verbs:

  arxiv_search(query)   — Atom API, no browser (from arxiv/scraping.md)
  hn_top()              — HN front page scrape (from hackernews/scraping.md)
  ddg_lookup(entity)    — DuckDuckGo Instant Answer JSON (from duckduckgo/scraping.md)
  browser_done(answer)  — terminate with final answer

These verbs encapsulate the scraping recipes from browser-harness'
agent-workspace/domain-skills/ — the agent just picks which to call,
we execute pre-baked Python via the harness on Dragon, and feed the
structured result back.

The harness here is the Python `browser-harness` CLI, which gives us
http_get + Python stdlib for free without needing the actual browser.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time

DRAGON = "radxa@192.168.1.91"
SSH_PASS = "thedragon"
STEP_LIMIT = 4

SYSTEM = """You are a research agent. The user asks a question; you call ONE verb.

Verbs:
  arxiv_search(query)         - search arxiv.org by title/abstract; returns top 5 papers
  hn_top()                    - top 10 stories on Hacker News right now
  ddg_lookup(entity)          - DuckDuckGo entity lookup (people, companies, concepts)
  browser_done(answer)        - finish; pass the final answer to the user

Output ONE verb per turn in this format:
  <|tool_call_start|>[verb_name(arg="value")]<|tool_call_end|>

When the observation answers the user's question, output browser_done(answer="..."). Never explain. Never repeat a verb you already called."""

RE_LFM = re.compile(
    r"<\|tool_call_start\|>\s*\[?\s*([a-z_]+)\s*\(([^)]*)\)",
    re.I | re.DOTALL,
)
VERB_NAMES = {"arxiv_search", "hn_top", "ddg_lookup", "browser_done"}
RE_BARE = re.compile(
    r"\b(arxiv_search|hn_top|ddg_lookup|browser_done)\s*\(([^)]*)\)",
    re.I | re.DOTALL,
)


def parse_call(text: str) -> tuple[str, dict] | None:
    import ast
    m = RE_LFM.search(text) or RE_BARE.search(text)
    if not m:
        return None
    name = m.group(1).lower()
    if name not in VERB_NAMES:
        return None
    args_src = m.group(2).strip()
    args: dict = {}
    if args_src:
        try:
            tree = ast.parse(f"_({args_src})", mode="eval")
            call = tree.body  # type: ignore
            for kw in call.keywords:  # type: ignore
                if kw.arg is None:
                    continue
                try:
                    args[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    pass
        except Exception:
            pass
    return name, args


def llm_step(history: list[dict]) -> tuple[str, float]:
    body = json.dumps({
        "model": "default",
        "messages": history,
        "max_tokens": 220,
        "temperature": 0.1,
    })
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"curl -s -m 180 -X POST http://127.0.0.1:1234/v1/chat/completions "
        f"-H 'Content-Type: application/json' -d {json.dumps(body)!s}",
    ]
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
    dt = time.monotonic() - t0
    try:
        return json.loads(r.stdout)["choices"][0]["message"]["content"], dt
    except Exception as e:
        return f"<<ERROR {e}: {r.stdout[:200]}>>", dt


def harness_exec(py_src: str) -> tuple[str, int]:
    remote = (
        "export PATH=$HOME/.local/bin:$PATH && "
        "BU_CDP_URL=http://127.0.0.1:9222 browser-harness"
    )
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"{remote} <<'PYEOF'\n{py_src}\nPYEOF",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = r.stdout.rstrip() if r.returncode == 0 else r.stderr.rstrip()
    return out, r.returncode


# ────────────────────────── Skill recipes ──────────────────────────

ARXIV_PY = r"""
import xml.etree.ElementTree as ET, urllib.parse
q = urllib.parse.quote_plus({query!r})
xml = http_get(
    "http://export.arxiv.org/api/query"
    "?search_query=all:" + q + "&max_results=5"
    "&sortBy=submittedDate&sortOrder=descending"
)
NS = {{'atom':'http://www.w3.org/2005/Atom','arxiv':'http://arxiv.org/schemas/atom'}}
root = ET.fromstring(xml)
for e in root.findall('atom:entry', NS):
    title = (e.find('atom:title', NS).text or '').strip().replace('\n',' ')
    arxiv_id = (e.find('atom:id', NS).text or '').split('/')[-1]
    published = (e.find('atom:published', NS).text or '')[:10]
    authors = [a.find('atom:name', NS).text for a in e.findall('atom:author', NS)]
    print(f"{{arxiv_id}}  {{published}}  {{title[:80]}}")
    if authors:
        print("   ", ", ".join(authors[:3]))
"""

HN_PY = r"""
import re, html as htmllib
page = http_get("https://news.ycombinator.com")
ids = re.findall(r'<tr class="athing submission" id="(\d+)">', page)[:10]
titles_urls = re.findall(
    r'class="titleline"[^>]*><a href="([^"]*)"[^>]*>(.*?)</a>', page
)[:10]
scores = {{m.group(1): int(m.group(2))
          for m in re.finditer(
              r'<span class="score" id="score_(\d+)">(\d+) points</span>', page)}}
for i, (sid, (u, t)) in enumerate(zip(ids, titles_urls), 1):
    title = htmllib.unescape(re.sub(r'<[^>]+>','', t))
    score = scores.get(sid, 0)
    print(f"{{i:2}}. ({{score}}) {{title}}")
    print(f"    {{u}}")
"""

DDG_PY = r"""
import json, urllib.parse
q = urllib.parse.quote({entity!r})
raw = http_get(f"https://api.duckduckgo.com/?q={{q}}&format=json&no_html=1&skip_disambig=1")
data = json.loads(raw)
print("Heading:", data.get('Heading','(none)'))
print("Entity:", data.get('Entity','(none)'))
abstract = data.get('AbstractText') or '(no abstract)'
print("Abstract:", abstract[:500])
print("URL:", data.get('AbstractURL','(none)'))
print("Site:", data.get('OfficialWebsite','(none)'))
"""


def execute_verb(name: str, args: dict) -> str:
    if name == "arxiv_search":
        q = args.get("query", "").strip()
        if not q:
            return "ERROR: arxiv_search requires query arg"
        py = ARXIV_PY.format(query=q)
        out, rc = harness_exec(py)
        return out if rc == 0 else f"ERROR: {out}"

    if name == "hn_top":
        out, rc = harness_exec(HN_PY.format())
        return out if rc == 0 else f"ERROR: {out}"

    if name == "ddg_lookup":
        ent = args.get("entity", "").strip()
        if not ent:
            return "ERROR: ddg_lookup requires entity arg"
        py = DDG_PY.format(entity=ent)
        out, rc = harness_exec(py)
        return out if rc == 0 else f"ERROR: {out}"

    if name == "browser_done":
        return f"__DONE__::{args.get('answer', '(no answer)')}"

    return f"ERROR: unknown verb {name!r}"


def run_goal(goal: str) -> None:
    print(f"=== USER GOAL: {goal} ===\n")
    history = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": goal},
    ]
    used_verbs: list[str] = []
    for step in range(1, STEP_LIMIT + 1):
        used_str = "; ".join(used_verbs) if used_verbs else "(none yet)"
        text, dt = llm_step(history)
        call = parse_call(text)
        if not call:
            print(f"[step {step}] ({dt:.1f}s) NO VERB EMITTED")
            print(f"  llm: {text.strip()[:200]}")
            return
        name, args = call
        used_verbs.append(f"{name}({json.dumps(args)})")
        print(f"[step {step}] ({dt:.1f}s) → {name}({args})")
        if name == "browser_done":
            print(f"\n=== ANSWER: {args.get('answer', '?')} ===")
            return
        obs = execute_verb(name, args)
        if obs.startswith("__DONE__::"):
            print(f"\n=== ANSWER: {obs[len('__DONE__::'):]} ===")
            return
        print(f"  obs:\n{obs[:800]}")
        history.append({"role": "assistant", "content": text})
        history.append({"role": "user", "content": (
            f"Result of your last verb:\n{obs[:1500]}\n\n"
            f"GOAL: {goal}\n"
            f"VERBS USED SO FAR: {used_str}\n\n"
            "If you can now answer the GOAL, output ONLY: "
            "<|tool_call_start|>[browser_done(answer=\"<answer with concrete details from the result>\")]<|tool_call_end|>\n"
            "Do NOT repeat a verb above. Output only one verb."
        )})
    print(f"\n=== STEP LIMIT ({STEP_LIMIT}) HIT — agent did not call browser_done ===")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("goal")
    a = p.parse_args()
    run_goal(a.goal)
