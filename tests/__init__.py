# Make src/ importable so tests can `import hiring_agents` when run from the repo root.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
