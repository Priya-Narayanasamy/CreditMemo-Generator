import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The generators are scripts rather than a package, so `data/` goes on the path
# alongside the project root.
for entry in (ROOT, ROOT / "data"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
