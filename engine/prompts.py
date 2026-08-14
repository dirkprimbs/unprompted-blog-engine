"""Prompt layer: loads engine/prompts.yaml and fills in its placeholders.

The prompt text itself lives in the YAML file, not here and not in the pipeline
code, so wording can be tuned without touching a Python file. This module is
only the plumbing: read the file once, hand out `render(name, **values)` and
`temperature(name)`.

Substitution is deliberately NOT str.format(). format() treats every brace in
the template as significant, so a stray '{' typed into a prompt raises, and a
placeholder the engine doesn't supply raises an unhelpful KeyError. Here only
the exact names a caller passes are replaced; any other brace is left alone as
literal text. What IS checked is the reverse - a {placeholder} left unfilled
after substitution is an error, because that is the failure that would
otherwise reach the model silently and quietly degrade its output.
"""

import os
import re
import sys

import yaml

from paths import PROMPTS_PATH

# {placeholder} - letters, digits, and underscores only, so JSON or prose braces
# in a prompt are never mistaken for one.
_PLACEHOLDER_RE = re.compile(r'\{([a-z_][a-z0-9_]*)\}')

_cache = None


def _load():
    """Read and validate prompts.yaml once, caching the result."""
    global _cache
    if _cache is not None:
        return _cache

    if not os.path.exists(PROMPTS_PATH):
        print(f"❌ Prompt file not found at '{PROMPTS_PATH}'.")
        print("💡 It ships with the engine - restore it from git.")
        sys.exit(1)
    try:
        with open(PROMPTS_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"❌ Could not parse '{PROMPTS_PATH}': {exc}")
        print("💡 Check the indentation of the block you last edited - every "
              "prompt's text must stay indented under its 'text: |-' line.")
        sys.exit(1)

    if not isinstance(data, dict) or not data:
        print(f"❌ '{PROMPTS_PATH}' did not parse to a mapping of prompts.")
        sys.exit(1)

    for name, entry in data.items():
        if not isinstance(entry, dict) or 'text' not in entry:
            print(f"❌ Prompt '{name}' in '{PROMPTS_PATH}' has no 'text:' block.")
            sys.exit(1)

    _cache = data
    return _cache


def temperature(name, default=0.2):
    """The sampling temperature configured for one prompt."""
    entry = _load().get(name)
    if entry is None:
        _missing(name)
    value = entry.get('temperature', default)
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"❌ Prompt '{name}' has a non-numeric temperature: {value!r}")
        sys.exit(1)


def _missing(name):
    known = ", ".join(sorted(_load()))
    print(f"❌ No prompt named '{name}' in '{PROMPTS_PATH}'.")
    print(f"💡 Prompts defined there: {known}")
    sys.exit(1)


def render(prompt_name, /, **values):
    """Return one prompt with its placeholders filled in.

    Only the names passed by the caller are substituted, so literal braces in a
    prompt survive untouched. Any {placeholder} still present afterwards means
    the prompt asks for something the engine does not supply - almost always a
    typo in the YAML - and is reported rather than sent to the model.

    The prompt name is positional-only ('/') so that every remaining keyword is
    free to be a placeholder. Without it a prompt could never use a placeholder
    called {prompt_name}, and the collision would surface as a confusing
    TypeError rather than anything to do with prompts."""
    name = prompt_name
    entry = _load().get(name)
    if entry is None:
        _missing(name)

    text = str(entry['text'])

    # Check the template, never the filled-in result. A prompt asking for
    # something the engine does not supply is a property of the YAML, and the
    # values are arbitrary prose - a post that happens to quote one of these
    # prompts carries literal braces, and those are data, not placeholders.
    leftover = sorted(set(_PLACEHOLDER_RE.findall(text)) - set(values))
    if leftover:
        supplied = ", ".join(sorted(values)) or "(none)"
        print(f"❌ Prompt '{name}' has unfilled placeholder(s): "
              f"{', '.join('{' + p + '}' for p in leftover)}")
        print(f"💡 The engine supplies these for this prompt: {supplied}. "
              f"Fix the name in '{PROMPTS_PATH}', or delete the placeholder if "
              f"you do not want that value in the prompt.")
        sys.exit(1)

    # One pass over the template, so a brace inside one value is never treated
    # as a placeholder for another - which would let a draft or an existing
    # post inject text into the prompt just by quoting it.
    return _PLACEHOLDER_RE.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        text)


def validate(expected=()):
    """Fail fast, before any generation, if the prompt file is unusable or is
    missing a prompt the run will need. Mirrors llm.check_required_keys() - the
    point is to die before a long batch, not halfway through it."""
    data = _load()
    absent = [name for name in expected if name not in data]
    if absent:
        print(f"❌ '{PROMPTS_PATH}' is missing required prompt(s): "
              f"{', '.join(absent)}")
        print(f"💡 Prompts defined there: {', '.join(sorted(data))}")
        sys.exit(1)
