# Make src/ importable so tests can `import hiring_agents` when run from the repo root.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import Runner  # noqa: E402


async def _no_real_llm_calls(*args, **kwargs):
    raise RuntimeError("Offline tests must not call a real LLM: stub this agent call "
                       "(see tests/stubs.py).")


# Any agent call a test forgot to stub fails loudly here instead of reaching the API.
# Tests that patch Runner.run (patch.object) replace this for their duration.
Runner.run = _no_real_llm_calls
