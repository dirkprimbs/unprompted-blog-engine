"""Model layer: the shared LLM / vision interface used by both the ingest
pipeline (ingest.py) and the site build (build_blog.py). It owns the provider
endpoints, the model-role configuration, and the retrying request/response
plumbing, so callers just say call_model(...) or generate_alt_text(...) without
caring whether a role is served locally (Ollama) or remotely (OpenRouter).

To change which model does what, edit the role definitions below."""

import os
import sys
import json
import time
import base64
import urllib.request

import prompts   # prompt text + per-prompt temperature, from prompts.yaml

# --- MODEL ENDPOINTS ---
OLLAMA_URL = "http://localhost:11434/api/generate"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model roles. Each role is a {"provider", "model"} pair. "provider" is either
# "ollama" (local) or "openrouter" (remote). Remote steps read the key from the
# OPENROUTER_API_KEY environment variable (never hard-code it here - this file
# is committed to git).
#   POLISH  - turns rough notes into a finished, publish-ready article (prose)
#   UTILITY - short structured steps (tags, title, summary, themes)
#   TRIAGE  - reads the ledger to shortlist backlink candidates
#   WEAVE   - adds inline backlinks to shortlisted posts
#
# POLISH does the heavy creative lifting and runs once per post. It is SPLIT in
# two by how finished the draft already is (see classify_draft / select_polish_
# role in ingest.py), because the two cases are genuinely different jobs:
#
#   POLISH_PROSE   - the draft is already flowing sentences; the model only
#                    copy-edits. There is little to invent, so a small LOCAL
#                    model does this indistinguishably from a big remote one -
#                    and the draft never leaves the machine (see privacy note).
#   POLISH_OUTLINE - the draft is bare bullets; the model must WRITE the prose.
#                    This is where model quality actually shows, so it goes to a
#                    strong remote model.
#
# This split came out of polish-model-test/: output divergence between models
# tracks how outline-y the source is almost perfectly, and gemma4:e4b matched
# the remote models closely on prose input but ran wordier on pure outlines.
#
# The other roles are cheap/structured and default to a small local model. WEAVE
# in particular makes one call per candidate, each carrying a full article in
# context, so keeping it local avoids paying a large model N times to place links.
#
# Privacy note: a role set to "openrouter" sends that step's text to a third
# party. With the split above, prose drafts stay fully local; only outline
# drafts (bullets, not yet finished writing) are sent out.
UTILITY = {"provider": "ollama", "model": "gemma4:e4b"}
POLISH_PROSE = {"provider": "ollama", "model": "gemma4:e4b"}
POLISH_OUTLINE = {"provider": "openrouter", "model": "google/gemini-3.5-flash"}
TRIAGE = {"provider": "ollama", "model": "gemma4:e4b"}
WEAVE = {"provider": "ollama", "model": "gemma4:e4b"}
# IMAGE writes alt text for images that don't already have it. It needs a
# vision-capable model and runs once per uncaptioned image (at build time, not
# ingest). Kept local (gemma4:e4b is multimodal) so image bytes never leave the
# machine and captioning is free; a hosted vision model like
# google/gemini-3.5-flash is sharper on fine detail (lens flare, colour cast) if
# quality matters more than privacy for a given run.
IMAGE = {"provider": "ollama", "model": "gemma4:e4b"}

ALL_ROLES = (POLISH_PROSE, POLISH_OUTLINE, UTILITY, TRIAGE, WEAVE, IMAGE)

# Per-request timeout (seconds). Generous enough to absorb a cold model load
# plus generation of a full-length article. Raise it if you switch to a larger,
# slower model for POLISH.
REQUEST_TIMEOUT = 600
# Transient failures (timeouts, rate limits, 5xx) are retried with backoff.
# Matters most for the paid remote provider; harmless for local Ollama.
MAX_RETRIES = 3

def label(role):
    """Human-readable 'provider:model' for progress output."""
    return f"{role['provider']}:{role['model']}"

def check_required_keys():
    """Fail fast, before any generation, if a role needs a remote key that is
    not present. Avoids dying halfway through a batch."""
    if any(r["provider"] == "openrouter" for r in ALL_ROLES):
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("❌ A model role uses the 'openrouter' provider but "
                  "OPENROUTER_API_KEY is not set.")
            print("💡 Export it (e.g. in publish.local.sh) or switch that role "
                  "back to the 'ollama' provider.")
            sys.exit(1)

def _build_request(role, prompt, temperature, image=None):
    """Construct the provider-specific urllib Request for one model call.

    When `image` is provided (an (bytes, mime_type) tuple), the request is built
    as a vision call: Ollama takes a bare base64 string in its `images` array,
    OpenRouter takes an OpenAI-style `image_url` content part with a data URI.
    The role's model must be vision-capable for this to succeed."""
    provider = role["provider"]
    if provider == "ollama":
        data = {
            "model": role["model"],
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if image is not None:
            img_bytes, _ = image
            data["images"] = [base64.b64encode(img_bytes).decode("ascii")]
        return urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("❌ OPENROUTER_API_KEY is not set - cannot reach OpenRouter.")
            sys.exit(1)
        if image is not None:
            img_bytes, mime = image
            b64 = base64.b64encode(img_bytes).decode("ascii")
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]
        else:
            content = prompt
        data = {
            "model": role["model"],
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        }
        return urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
    print(f"❌ Unknown model provider: {provider!r} (use 'ollama' or 'openrouter').")
    sys.exit(1)

def _extract_response(role, res_data):
    """Pull the generated text out of a provider-specific response body."""
    if role["provider"] == "ollama":
        return res_data.get("response", "").strip()
    # openrouter (OpenAI-style chat completion)
    return res_data["choices"][0]["message"]["content"].strip()

def call_model(prompt, role=UTILITY, temperature=0.2, image=None):
    """Send one generation request for the given role, dispatching to the local
    (Ollama) or remote (OpenRouter) provider.

    Prose steps use a little randomness (default 0.2). Judgement/extraction
    steps - triage and backlink weaving - pass temperature=0 so that a genuine
    connection is found reliably instead of appearing on only some runs.

    Pass `image` as an (bytes, mime_type) tuple to make a vision call (the role's
    model must be multimodal).

    Transient errors are retried with backoff before giving up."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = _build_request(role, prompt, temperature, image=image)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                return _extract_response(role, res_data)
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                backoff = 2 ** attempt  # 2s, 4s, ...
                print(f"⚠️  {label(role)} request failed (attempt "
                      f"{attempt}/{MAX_RETRIES}): {e} - retrying in {backoff}s...")
                time.sleep(backoff)
    print(f"❌ Model API Error ({label(role)}) after {MAX_RETRIES} attempts: {last_error}")
    sys.exit(1)


def generate_alt_text(image_bytes, mime_type):
    """Return a concise, factual alt-text string for an image, using the IMAGE
    role's vision model. Alt text describes the image for screen-reader users
    and for when the image fails to load, so it should state what is shown
    plainly - no 'image of'/'photo of' preamble, no marketing gloss.

    The wording lives in prompts.yaml under 'alt_text'.

    Returns a single trimmed line (never multi-line, never wrapped in quotes)."""
    raw = call_model(prompts.render('alt_text'), role=IMAGE,
                     temperature=prompts.temperature('alt_text'),
                     image=(image_bytes, mime_type))
    # Collapse to a single clean line: models occasionally wrap the answer in
    # quotes or spread it over several lines despite the instruction.
    alt = " ".join(raw.split()).strip().strip('"').strip("'").strip()
    return alt
