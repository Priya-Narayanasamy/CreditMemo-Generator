import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The generators are scripts rather than a package, so `data/` goes on the path
# alongside the project root.
for entry in (ROOT, ROOT / "data"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from config import DEFECTS_PATH  # noqa: E402
from src.agents.drafting import OfflineDrafter, drafting_node  # noqa: E402
from src.agents.evidence import EvidenceAgent  # noqa: E402
from src.agents.review import NoModelReviewer, ReviewAgent  # noqa: E402
from src.graph import MemoGraph  # noqa: E402
from src.tools.extraction import LocalTableExtractor  # noqa: E402


class OfflineGraph(MemoGraph):
    """The graph with every model call replaced by a deterministic stand-in.

    Nothing about the orchestration, the ledger or the provenance rules changes -
    only where the narrative text comes from. That keeps the end-to-end tests
    exact and runnable without credentials.
    """

    def drafting(self, state):
        notes = [note.note for note in state.review_notes if note.must_fix]
        return drafting_node(state, OfflineDrafter(state), revision_notes=notes or None)


def build_offline_graph(output_dir: Path | None = None, **kwargs) -> OfflineGraph:
    return OfflineGraph(
        evidence_agent=EvidenceAgent(extractor=LocalTableExtractor()),
        review_agent=ReviewAgent(NoModelReviewer()),
        output_dir=output_dir,
        **kwargs,
    )


@pytest.fixture()
def offline_graph(tmp_path) -> OfflineGraph:
    return build_offline_graph(output_dir=tmp_path / "output")


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    return json.loads(DEFECTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def defects(ground_truth) -> dict[str, dict]:
    """Seeded defects keyed by application. Tests read expectations from here
    rather than hard-coding them."""
    return {defect["application_number"]: defect for defect in ground_truth["defects"]}
