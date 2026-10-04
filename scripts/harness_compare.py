#!/usr/bin/env python3
"""Compare Harvest's deterministic policy with LLM harnesses on the SAME evidence and budget.

Every arm starts from its own offline replay of the same saved capture (default 212, the
legacy maigret scan of the owner's handle "Snaxxwax", 20 accounts), with identical
investigation-wide limits, and is asked the same question: which of these accounts belong to
the person behind https://github.com/Snaxxwax? Follow-ups are live fetches through Harvest,
so scope, provenance and the shared budget are Harvest's in every arm.

Phases per arm:
  clean        one uninterrupted session.
  interrupted  the session is killed as soon as its first follow-up job exists, then a NEW
               session is told only the job id and asked to resume.

Each run is scored by scripts/harness_eval.py against --labels, plus the harness's own
cost/usage report where the harness provides one. Exact commands and model settings are
written into every result file.

Usage:
    HARVEST_API_URL=... HARVEST_API_TOKEN=... python scripts/harness_compare.py \\
        --arms planner,claude,hermes,codex --phases clean,interrupted --out DIR --tag v07
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_here = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("harness_eval", _here / "harness_eval.py")
he = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(he)

ANCHOR = "https://github.com/Snaxxwax"
ANSWER_RE = re.compile(r"ANSWER_JSON:\s*(\{.*\})")
TERMINAL = {"completed", "partial", "failed", "cancelled", "budget_exhausted", "plateau"}

TASK = """You are driving the Harvest OSINT platform through its MCP tools (server "harvest").
Use only those tools: no shell, no web browsing.{codex_hint}

Investigation job {job} already contains a saved maigret scan of the username "Snaxxwax":
accounts on sites that answered to that handle. Read it with get_evidence(job_id="{job}")
(paginate with `after`) and get_dossier(job_id="{job}").

The whole investigation has ONE budget, enforced by Harvest and shared by every follow-up:
{requests} HTTP requests and {bytes} bytes. A profile on a new host usually costs 2 requests
(robots.txt + the page). When Harvest answers budget_exhausted, no retry can get around it.

Goal: decide which of these accounts belong to the same person as the GitHub account
{anchor}. A matching handle alone is NOT evidence of the same owner. To check an account,
call request_followup(job_id="{job}", url=<its profile URL from the evidence>,
idempotency_key=<that same URL>), poll investigation_status(job_id=<returned child id>) until
its status is terminal, then read get_evidence on the child. Spend the budget on the accounts
most likely to settle the question; stop when it is exhausted or you have enough evidence.
{resume}
End your reply with exactly one line, and nothing after it:
ANSWER_JSON: {{"owned": [...], "not_owned": [...], "undetermined": [...]}}
listing profile URLs (as they appear in the evidence) under the verdict you can support."""

RESUME = """
A previous session working on this investigation was interrupted. First call
investigation_status(job_id="{job}"): investigation.job_ids lists the follow-ups already made.
Inspect those instead of repeating them; reuse the same idempotency_key if you re-request one.
"""


def _client():
    return he._api()


def _wait(c, job_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        inv = c.get(f"/jobs/{job_id}").json()["investigation"]
        statuses = [c.get(f"/jobs/{j}").json()["status"] for j in inv["job_ids"]]
        if all(s in TERMINAL for s in statuses):
            return statuses
        time.sleep(3)
    return statuses


def make_parent(c, capture, limits, key):
    investigation = {
        "targets": [
            {"key": "snaxxwax", "label": "Snaxxwax", "identifiers": {"username": ["Snaxxwax"]}}
        ],
        "sources": [
            {
                "url": "tool://maigret/Snaxxwax",
                "identifier_fields": {"username": "username"},
                "field_map": {"url": "profile_url"},
            }
        ],
    }
    r = c.post(
        "/replays",
        json={"capture_ids": [capture], "limits": limits, "investigation": investigation},
        headers={"Idempotency-Key": key},
    )
    r.raise_for_status()
    job = r.json()["id"]
    _wait(c, job, 120)
    return job


def planner_arm(c, job, phase):
    """Harvest's own, LLM-free policy: follow accounts in Harvest's confidence order until the
    shared budget refuses, then assert nothing (a tool never proves ownership)."""
    obs = he._all(c, f"/jobs/{job}/observations")
    best: dict[str, float] = {}
    for o in obs:
        if o["field"] == "url":
            url = he._unpack(o["value"])
            best[url] = max(best.get(url, 0), o["confidence"])
    order = sorted(best, key=lambda u: (-best[u], u))
    made, refused = [], None
    for url in order:
        r = c.post(f"/jobs/{job}/followup", json={"url": url}, headers={"Idempotency-Key": url})
        if r.status_code == 409:
            refused = r.json()["detail"]
            break
        if r.status_code >= 400:
            continue
        made.append(url)
        _wait(c, r.json()["id"], 300)
    if phase == "interrupted":
        # Re-run from scratch: idempotency keys must make every repeated request a no-op.
        for url in made:
            c.post(f"/jobs/{job}/followup", json={"url": url}, headers={"Idempotency-Key": url})
    answer = {"owned": [], "not_owned": [], "undetermined": order}
    return {
        "answer": answer,
        "command": "in-process deterministic policy (harness_compare.planner_arm)",
        "model": "none",
        "refused": refused,
        "usage": None,
        "exit": 0,
    }


def _mcp_config(tmp: Path) -> Path:
    cfg = tmp / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harvest": {
                        "command": str(Path(sys.executable).parent / "harvest-mcp"),
                        "env": {
                            "HARVEST_API_URL": os.environ["HARVEST_API_URL"],
                            "HARVEST_API_TOKEN": os.environ["HARVEST_API_TOKEN"],
                        },
                    }
                }
            }
        )
    )
    return cfg


def agent_command(arm, prompt, tmp: Path):
    if arm == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            "sonnet",
            "--strict-mcp-config",
            "--mcp-config",
            str(_mcp_config(tmp)),
            "--allowedTools",
            "mcp__harvest",
            "--disallowedTools",
            "Bash,WebFetch,WebSearch",
        ]
    if arm == "hermes":
        return ["hermes", "-z", prompt, "--usage-file", str(tmp / "usage.json")]
    if arm == "codex":
        return [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-c",
            'mcp_servers.harvest.default_tools_approval_mode="approve"',
            prompt,
        ]
    raise ValueError(arm)


def _redact(cmd):
    return [a if len(a) < 200 else "<prompt>" for a in cmd]


def parse_run(arm, out: str, tmp: Path):
    text, usage = out, None
    if arm == "claude":
        try:
            data = json.loads(out)
            text = data.get("result", "")
            usage = {
                "total_cost_usd": data.get("total_cost_usd"),
                "num_turns": data.get("num_turns"),
                "models": sorted((data.get("modelUsage") or {}).keys()),
                "usage": data.get("usage"),
            }
        except ValueError:
            pass
    elif arm == "codex":
        texts, tokens = [], {}
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            item = e.get("item") or {}
            if e.get("type") == "item.completed" and item.get("type") == "agent_message":
                texts.append(item.get("text", ""))
            if e.get("type") == "turn.completed":
                for k, v in (e.get("usage") or {}).items():
                    tokens[k] = tokens.get(k, 0) + v
        text, usage = "\n".join(texts), {"tokens": tokens}
    elif arm == "hermes" and (tmp / "usage.json").exists():
        usage = json.loads((tmp / "usage.json").read_text())
    matches = ANSWER_RE.findall(text)
    answer = None
    if matches:
        try:
            answer = json.loads(matches[-1])
        except ValueError:
            answer = None
    return answer, usage, text[-2000:]


def agent_arm(c, arm, job, phase, args):
    fmt = dict(
        job=job,
        requests=args.requests,
        bytes=args.bytes,
        anchor=ANCHOR,
        codex_hint=" Use tool search to find the harvest tools." if arm == "codex" else "",
    )
    runs = []
    with tempfile.TemporaryDirectory(prefix=f"hc-{arm}-") as d:
        tmp = Path(d)
        prompts = [TASK.format(resume="", **fmt)]
        if phase == "interrupted":
            prompts.append(TASK.format(resume=RESUME.format(job=job), **fmt))
        for i, prompt in enumerate(prompts):
            cmd = agent_command(arm, prompt, tmp)
            started = time.time()
            proc = subprocess.Popen(
                cmd,
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            killed = False
            if phase == "interrupted" and i == 0:
                while proc.poll() is None and time.time() - started < args.timeout:
                    if c.get(f"/jobs/{job}").json()["investigation"]["jobs"] > 1:
                        time.sleep(2)  # let the follow-up request return to the harness
                        os.killpg(proc.pid, signal.SIGKILL)
                        killed = True
                        break
                    time.sleep(2)
            try:
                out, _ = proc.communicate(timeout=max(1, args.timeout - (time.time() - started)))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
                killed = True
            answer, usage, final_text = parse_run(arm, out or "", tmp)
            runs.append(
                {
                    "session": i + 1,
                    "exit": proc.returncode,
                    "killed": killed,
                    "seconds": round(time.time() - started, 1),
                    "answer": answer,
                    "usage": usage,
                    "final_text": final_text,
                    "command": _redact(cmd),
                }
            )
    return {"answer": runs[-1]["answer"], "sessions": runs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="planner,claude,hermes,codex")
    ap.add_argument("--phases", default="clean,interrupted")
    ap.add_argument("--capture", type=int, default=212)
    ap.add_argument("--requests", type=int, default=10)
    ap.add_argument("--bytes", type=int, default=3_000_000)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument(
        "--labels", default=str(_here.parent / "tests" / "data" / "eval_labels_snaxxwax.json")
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    labels = json.loads(Path(args.labels).read_text())
    limits = {
        "requests": args.requests,
        "bytes": args.bytes,
        "seconds": 3600,
        "depth": 0,
        "tool_runs": 0,
        "tasks": 60,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with _client() as c:
        for arm in args.arms.split(","):
            for phase in args.phases.split(","):
                job = make_parent(c, args.capture, limits, f"{args.tag}-{arm}-{phase}")
                started = time.time()
                run = (
                    planner_arm(c, job, phase)
                    if arm == "planner"
                    else agent_arm(c, arm, job, phase, args)
                )
                statuses = _wait(c, job, 600)
                score = he.evaluate(c, job, labels, run["answer"] or {"owned": []})
                result = {
                    "arm": arm,
                    "phase": phase,
                    "root_job": job,
                    "limits": limits,
                    "capture": args.capture,
                    "wall_seconds": round(time.time() - started, 1),
                    "final_statuses": statuses,
                    "answer_parsed": run["answer"] is not None,
                    "run": run,
                    "score": score,
                }
                (out / f"{arm}-{phase}.json").write_text(json.dumps(result, indent=1))
                print(
                    json.dumps(
                        {
                            "arm": arm,
                            "phase": phase,
                            "job": job,
                            "followups": score["followups"],
                            "useful": score["useful_followups"],
                            "errors": score["attribution_answer"].get("errors"),
                            "requests": score["cost_enforced"]["requests"],
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
