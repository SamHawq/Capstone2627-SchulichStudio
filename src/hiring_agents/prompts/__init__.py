"""Agent prompts, kept as plain text files so they can be read and edited without touching code.

Every LLM agent's instructions live in a `.md` file in this folder (see README.md for which
code fills each one and what input the agent receives). Dynamic parts are written as
`{placeholder}` (lowercase letters and underscores) and filled by `render`:

    render("ranker/main", criterion_id="teaching_experience", ...)

Files are read on every call, so an edited prompt is used by the next agent call, even in a
running server. Anything else in braces (e.g. a JSON example) is left as written.
"""

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def path_of(name: str) -> Path:
    """The file for a prompt name, e.g. "ranker/main" -> prompts/ranker/main.md."""
    return PROMPTS_DIR / f"{name}.md"


def render(name: str, /, **values: str) -> str:
    """The prompt `name` with its placeholders filled in.

    Inserted values are not scanned again, so text containing braces is safe to insert.

    Raises:
        FileNotFoundError: If there is no such prompt file.
        KeyError: If a placeholder in the file has no value, or a value matches no placeholder
            (usually a renamed placeholder in the file).
    """
    text = path_of(name).read_text(encoding="utf-8").strip()
    found = set(_PLACEHOLDER.findall(text))
    missing, unused = found - values.keys(), values.keys() - found
    if missing or unused:
        raise KeyError(f"Prompt '{name}': no value for {sorted(missing)}" if missing
                       else f"Prompt '{name}' has no placeholder for {sorted(unused)}")
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
