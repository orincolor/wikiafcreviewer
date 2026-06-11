# WikiAfCReviewer

An **advisory AI assessment tool** for Wikipedia Articles for Creation (AfC) drafts. It adds an
"AI Assessment" panel to `Draft:` pages that returns a source-by-source notability assessment
(WP:GNG / SNG), neutrality (WP:NPOV) and conflict-of-interest (WP:COI) flags, citation-quality
checks, and an overall AfC-readiness verdict — with the governing policy cited for each finding.

> **Guardrail — advisory, read-only, no autonomous edits.** It *assesses* drafts and explains its
> reasoning. It does **not** write or rewrite article prose, and it does **not** edit, move, accept,
> decline, or otherwise act on any page. A human reviewer makes every decision and performs any edit
> under their own account. The service is strictly read-only against the wikis.

This is the deployment of the review capability from the `wikipedia-contributor` skills package in
the parent directory. It packages that agent's *judgment* (notability / policy review) into a tool;
it deliberately does **not** reimplement the *mechanics* of review — those belong to
[AFCH](https://github.com/wikimedia-gadgets/afc-helper), the established AfC Helper Script that
reviewers already use to accept/decline/comment. WikiAfCReviewer is complementary: it advises, AFCH
acts (under the human's control).

## Architecture

```
Companion userscript  ──►  Toolforge backend (Flask)  ──►  Claude agentic loop
 (Draft: pages,             CORS-restricted, holds the         · web_search  (off-wiki sources)
  renders the panel)        Anthropic key server-side          · PoliteWiki  (on-wiki reads)
```

- **`userscript/`** — a clean-room, GPL-3.0 userscript that injects the advisory panel on `Draft:`
  pages and `POST`s the draft *title* to the backend. Renders results via `textContent` only (no
  `innerHTML`), and never touches a write API.
- **`backend/`** — a Flask service:
  - `app.py` — `POST /review {title}` → fetch the draft → run the review → return JSON.
  - `reviewer.py` — the agentic tool-use loop. The model verifies each source with **web search**,
    checks English Wikipedia for existing/duplicate coverage via the polite client, and emits a
    structured verdict. Its system prompt is assembled from the skills package (the agent guardrail
    plus the content-policies, create-article, and cite-sources skills).
  - `polite_wiki.py` — `PoliteWiki`, an etiquette-compliant MediaWiki Action API client.
- **`about.md`** — the public tool description (AI-assisted disclosure + contact); the page the
  `User-Agent` points to.

### Why a self-hosted loop (not Managed Agents)

The review is *search → verify → judge* — it needs no sandbox/workspace. A self-hosted Claude API
tool-use loop is genuinely agentic (the model drives the trajectory), lean, keeps the key
server-side, and — critically — runs every on-wiki read **host-side**, so each MediaWiki API request
carries our compliant `User-Agent`. (Managed Agents would only match that by adding the same
host-side tools, while bringing container infrastructure the task never uses. It remains the right
choice if a future version becomes workspace-backed — e.g. autonomously assembling sourced drafts.)

## API etiquette

All on-wiki reads go through `PoliteWiki`, which complies with the
[Wikimedia User-Agent policy](https://meta.wikimedia.org/wiki/User-Agent_policy) and
[API etiquette](https://www.mediawiki.org/wiki/API:Etiquette):

- a descriptive `User-Agent` identifying the tool with contact info (configurable via `USER_AGENT`),
- the `maxlag=5` parameter so the tool backs off when the database replicas are lagged,
- serialized requests (one at a time) with rate limiting and `Retry-After` handling.

Off-wiki source verification uses Claude's server-side `web_search`; the Wikimedia UA policy governs
the MediaWiki API, which only `PoliteWiki` touches.

## Setup

### Backend

```sh
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export USER_AGENT='WikiAfCReviewer/1.0 (https://en.wikipedia.org/wiki/User:Ordiopside; or@diopside.ai) python-requests'
# Optional: export ALLOWED_ORIGIN=https://en.wikipedia.org
python app.py            # dev server on :5000
```

Test:

```sh
curl -XPOST localhost:5000/review \
  -H 'Content-Type: application/json' \
  -d '{"title":"Draft:Bernard James"}'
```

Deploy on [Toolforge](https://wikitech.wikimedia.org/wiki/Help:Toolforge) as a Python web service;
keep `ANTHROPIC_API_KEY` in the tool's secrets, never in client code.

### Userscript (for testing)

1. Save `userscript/WikiAfCReviewer.js` at `User:<you>/WikiAfCReviewer.js` on English Wikipedia and
   set `BACKEND_URL` to your deployed endpoint.
2. Add to `Special:MyPage/common.js`:
   ```js
   mw.loader.load('//en.wikipedia.org/w/index.php?title=User:<you>/WikiAfCReviewer.js&action=raw&ctype=text/javascript');
   ```
3. Open any existing `Draft:` page → **Assess this draft**.

(Optional: load `WikiAfCReviewer.css` for verdict colors.)

## License

Userscript and CSS: **GNU General Public License v3.0 or later** (matching the Wikimedia gadgets
ecosystem). Backend: choose your own license — GPL does not reach across the network boundary, so
the userscript's license does not bind the service.
