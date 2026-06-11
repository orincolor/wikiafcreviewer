"""The agentic review loop.

Drives Claude through a self-hosted tool-use loop: it reads the draft, verifies
each source with web search, checks on-wiki for existing/duplicate coverage via
the PoliteWiki custom tools, and returns a structured, policy-grounded assessment.

The system prompt is assembled faithfully from the wikipedia-contributor skills
package (the agent guardrail + the content-policies, create-article, and
cite-sources skills) and inlined here so the deployed backend is self-contained.
Advisory only: it never edits, accepts, or declines anything.
"""

import json
import logging
import os

import anthropic

log = logging.getLogger(__name__)

# Stage 1 (search + notability judgment) carries the quality — defaults to Opus.
# Effort high is thorough but the biggest cost/latency lever; drop to "medium" to
# trade a little depth for speed. The analyze model must support the effort param
# (Opus 4.x / Sonnet 4.6 — not Haiku).
ANALYZE_MODEL = os.environ.get("ANALYZE_MODEL", "claude-opus-4-8")
ANALYZE_EFFORT = os.environ.get("ANALYZE_EFFORT", "high")
# Stage 2 is mechanical restructuring into the schema — no judgment — so a small,
# fast model is plenty. (No effort param here; Haiku doesn't accept it.)
CONVERT_MODEL = os.environ.get("CONVERT_MODEL", "claude-haiku-4-5")

MAX_ITERATIONS = 8  # cap on tool/continuation rounds per review.

# --- System prompt (assembled from the skills package) -----------------------
SYSTEM_PROMPT = """\
You are an experienced English Wikipedia editor assisting an Articles for Creation \
(AfC) reviewer. You ASSESS a draft against policy and explain your reasoning. You \
produce an ADVISORY review only: a human reviewer makes every decision and performs \
any edit. You never write or rewrite article prose, never fabricate a source, and \
never act on any page.

Evaluate the draft against Wikipedia's core content policies:

NOTABILITY (WP:GNG / SNG). The subject must have significant coverage in MULTIPLE \
reliable, independent, secondary sources (major newspapers, magazines, books, \
peer-reviewed journals). Not sufficient: primary or self-published sources, press \
releases, interviews used for facts about the subject, social media, or the \
subject's own site. Multiple pieces by the SAME author or the SAME outlet count as \
roughly ONE source toward the "multiple sources" test. Finalist/nominee/award \
mentions do NOT by themselves satisfy WP:ANYBIO — treat them as facts to verify, \
not as the notability basis. For EACH source in the draft, use web search to verify \
it actually exists, is independent of the subject, is a reliable publication, and \
provides significant (not passing/trivial) coverage.

NEUTRAL POINT OF VIEW (WP:NPOV). Flag promotional, peacock, or editorializing \
language (quote the exact phrase); opinion stated as fact; undue weight.

VERIFIABILITY (WP:V) and BLP. Flag statements lacking an inline citation — \
especially quotations, challenged claims, and anything about living people — and \
sources that are not reliable/independent.

NO ORIGINAL RESEARCH (WP:NOR). Flag conclusions or syntheses not found in the cited \
sources.

CONFLICT OF INTEREST (WP:COI). Note promotional structure consistent with \
undisclosed COI or paid editing; do not assume bad faith. (A disclosed paid/COI \
relationship plus the AfC path is correct procedure, not a strike against the draft.)

COPYRIGHT (WP:COPYVIO). Note text that reads as copied or closely paraphrased; you \
cannot verify against the live source, so flag for the human to check.

Tools available to you:
- web_search: verify sources and look for additional independent coverage the draft \
  omits. This is your primary source-verification tool.
- wiki_search: search English Wikipedia for existing or duplicate articles on the \
  subject, and for related coverage.
- wiki_get_wikitext: read the wikitext of a related/duplicate article by title.

Base the notability verdict on what you can actually verify, not on the draft's own \
claims. Be specific, cite the governing policy shortcut for each finding, and state \
uncertainty honestly. Produce a thorough written assessment covering: each source \
(whether it exists, and whether it is independent, reliable, and significant \
coverage), whether the draft meets WP:GNG, any NPOV and COI concerns, and an overall \
AfC verdict — accept, decline, or borderline — with a short rationale."""

# Instruction for the second stage: turn the written assessment into the schema.
CONVERT_INSTRUCTION = (
    "Convert the following Articles for Creation assessment into the required JSON "
    "schema. Do not change the judgments — only restructure what the assessment says. "
    "Assessment:\n\n"
)

# --- Structured output schema (must match the userscript renderer) -----------
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sources", "meets_gng", "npov_flags", "coi_flags",
                 "afc_verdict", "verdict_rationale"],
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["citation", "exists", "independent", "reliable",
                             "significant_coverage", "note"],
                "properties": {
                    "citation": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "independent": {"type": "boolean"},
                    "reliable": {"type": "boolean"},
                    "significant_coverage": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
        "meets_gng": {"type": "boolean"},
        "npov_flags": {"type": "array", "items": {"type": "string"}},
        "coi_flags": {"type": "array", "items": {"type": "string"}},
        "afc_verdict": {"type": "string", "enum": ["accept", "decline", "borderline"]},
        "verdict_rationale": {"type": "string"},
    },
}

# --- Tools -------------------------------------------------------------------
# web_search runs server-side; the wiki_* tools execute host-side via PoliteWiki,
# which is what keeps every MediaWiki API hit under our compliant User-Agent.
TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {
        "type": "custom",
        "name": "wiki_search",
        "description": (
            "Search English Wikipedia for existing articles. Use to check whether "
            "the subject already has an article (or a duplicate) and to find related pages."
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "type": "custom",
        "name": "wiki_get_wikitext",
        "description": (
            "Fetch the raw wikitext of an English Wikipedia page by title (e.g. to "
            "inspect a possible duplicate article). Returns '(page does not exist)' if missing."
        ),
        "input_schema": {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
    },
]


def _log_usage(label, resp):
    """Log token usage for one API call (input/output + cache read/write)."""
    u = resp.usage
    log.info(
        "usage %s: input=%s output=%s cache_read=%s cache_write=%s",
        label,
        getattr(u, "input_tokens", None),
        getattr(u, "output_tokens", None),
        getattr(u, "cache_read_input_tokens", None),
        getattr(u, "cache_creation_input_tokens", None),
    )


def _dispatch(name, tool_input, wiki):
    """Execute a host-side custom tool with the polite wiki client."""
    if name == "wiki_search":
        return wiki.search(tool_input["query"])
    if name == "wiki_get_wikitext":
        return wiki.get_wikitext(tool_input["title"]) or "(page does not exist)"
    raise ValueError(f"unknown tool: {name}")


def review(title, wikitext, wiki, client=None):
    """Run the agentic review and return a dict matching REVIEW_SCHEMA.

    Two stages: (1) an agentic tool-use loop where the model verifies sources with
    web search and produces a written assessment; (2) a tool-less call that converts
    that assessment into the JSON schema. Splitting them keeps the structured-output
    constraint off the same request as web search (whose citation blocks conflict with
    structured output).
    """
    client = client or anthropic.Anthropic()
    analysis = _analyze(title, wikitext, wiki, client)
    return _to_schema(analysis, client)


def _analyze(title, wikitext, wiki, client):
    """Stage 1: agentic loop with tools; returns the written assessment text."""
    messages = [{
        "role": "user",
        "content": (
            f"Review this Articles for Creation draft titled '{title}'. "
            f"Verify its sources and assess it for notability, neutrality, and conflict "
            f"of interest. Here is the wikitext:\n\n{wikitext}"
        ),
    }]

    for i in range(1, MAX_ITERATIONS + 1):
        resp = client.messages.create(
            model=ANALYZE_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": ANALYZE_EFFORT},
            tools=TOOLS,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        _log_usage(f"analyze iter {i} [{ANALYZE_MODEL}/{ANALYZE_EFFORT}]", resp)

        # Server-side tool (web_search) hit its iteration cap — re-send to continue.
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue

        # Host-side custom tools requested — execute them and feed results back.
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = _dispatch(block.name, block.input, wiki)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out)})
                except Exception as exc:  # noqa: BLE001 - report to the model, keep looping
                    log.warning("tool %s failed: %s", block.name, exc)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(exc), "is_error": True})
            messages.append({"role": "user", "content": results})
            continue

        # Done — return the written assessment.
        return "".join(b.text for b in resp.content if b.type == "text")

    raise RuntimeError("review did not converge within iteration cap")


def _to_schema(analysis, client):
    """Stage 2: convert the written assessment into the JSON schema (no tools)."""
    resp = client.messages.create(
        model=CONVERT_MODEL,
        max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        messages=[{"role": "user", "content": CONVERT_INSTRUCTION + analysis}],
    )
    _log_usage(f"convert [{CONVERT_MODEL}]", resp)
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
