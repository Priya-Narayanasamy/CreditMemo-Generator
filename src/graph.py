"""Graph assembly.

A supervisor routes between the four agents. Two interrupt types are kept
distinct in state and never collapsed into one "waiting for a human" flag,
because they mean opposite things:

- `ESCALATION` - the agent cannot proceed. Evidence is missing or sources
  disagree. The analyst supplies a value or abandons the run.
- `APPROVAL` - the agent has finished and is asking permission to write. The
  analyst approves, edits or rejects.

No write of any kind happens before an approval. The render node sits after the
approval interrupt, and it is the only node that touches the filesystem.

Once `approved_memo` is set the drafting node does not run again; resuming from a
checkpoint restores the approved text rather than regenerating it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from config import CHECKPOINT_DB, MAX_REVISIONS
from src.agents.analysis import AnalysisAgent, analysis_node
from src.agents.drafting import NarrativeDrafter, default_drafter, drafting_node
from src.agents.evidence import EvidenceAgent, evidence_node
from src.agents.review import ReviewAgent, review_node
from src.state import ApprovalRequest, MemoState, Provenance


class MemoGraph:
    """The assembled graph, with its agents injected once.

    Agents are constructed here rather than inside the nodes so a run can be given
    an offline extractor or a stub drafter without reaching into module globals.
    """

    def __init__(
        self,
        evidence_agent: EvidenceAgent | None = None,
        analysis_agent: AnalysisAgent | None = None,
        drafter: NarrativeDrafter | None = None,
        review_agent: ReviewAgent | None = None,
        output_dir: Path | None = None,
        max_revisions: int = MAX_REVISIONS,
    ) -> None:
        self.evidence_agent = evidence_agent or EvidenceAgent()
        self.analysis_agent = analysis_agent or AnalysisAgent()
        self.drafter = drafter
        self.review_agent = review_agent or ReviewAgent(max_revisions=max_revisions)
        self.output_dir = Path(output_dir) if output_dir else Path("output")
        self.max_revisions = max_revisions

    # --- nodes ------------------------------------------------------------

    def evidence(self, state: MemoState) -> dict:
        return evidence_node(state, self.evidence_agent)

    def analysis(self, state: MemoState) -> dict:
        return analysis_node(state, self.analysis_agent)

    def drafting(self, state: MemoState) -> dict:
        drafter = self.drafter or default_drafter(state)
        notes = [note.note for note in state.review_notes if note.must_fix]
        return drafting_node(state, drafter, revision_notes=notes or None)

    def review(self, state: MemoState) -> dict:
        return review_node(state, self.review_agent)

    def request_approval(self, state: MemoState) -> dict:
        """Assemble the approval request. Still no write of any kind."""
        outstanding = [
            f"{note.category}: {note.note}"
            for note in state.review_notes
            if note.must_fix
        ]
        discrepancies = [
            f for f in state.policy_findings if f.finding_type == "discrepancy"
        ]

        request = ApprovalRequest(
            summary=(
                f"A first draft for {state.application_number} is ready for review. "
                f"{len(state.ledger)} sourced values, {len(discrepancies)} discrepancy "
                f"finding(s), {len(state.review_notes)} review note(s). Nothing has "
                f"been written."
            ),
            figure_count=len(state.ledger),
            finding_count=len(state.policy_findings),
            outstanding_notes=outstanding,
        )
        return {
            "approval_request": request,
            "trace": state.trace + ["Awaiting analyst approval. Nothing written yet."],
        }

    def render(self, state: MemoState) -> dict:
        """The only node that writes. Reached only after an approval."""
        if state.approved_memo is None:
            raise RuntimeError(
                "the render node was reached without an approved memo; no write is "
                "permitted before approval"
            )

        from src.tools.renderer import render_docx

        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.output_dir / f"{state.application_number}_credit_memo_{stamp}.docx"
        render_docx(state, path, approved_by=state.approved_by)

        return {
            "rendered_path": str(path),
            "trace": state.trace + [f"Approved by {state.approved_by}. Written to {path}."],
        }

    # --- routing ------------------------------------------------------------

    def after_evidence(self, state: MemoState) -> Literal["escalate", "analysis"]:
        return "escalate" if state.escalation is not None else "analysis"

    def after_review(self, state: MemoState) -> Literal["drafting", "approval"]:
        """Back to drafting while there is something that must be fixed and budget
        left to fix it.

        An approved memo goes straight on. Without that check the pair of nodes
        deadlocks: drafting is forbidden from running once the memo is approved, so
        it returns the same sections, so review raises the same notes, forever.
        """
        if state.approved_memo is not None:
            return "approval"
        if state.revision_count >= self.max_revisions:
            return "approval"
        return "drafting" if any(note.must_fix for note in state.review_notes) else "approval"

    def after_approval(self, state: MemoState) -> Literal["render", "wait"]:
        return "render" if state.approved_memo is not None else "wait"

    # --- assembly -----------------------------------------------------------

    def finalise(self, state: MemoState) -> MemoState:
        """Run the render node against an approved state.

        The path for a caller holding no checkpointer. Re-invoking the whole graph
        would restart it at the entry point and redo the evidence loop, so the one
        remaining node is run directly.
        """
        if state.approved_memo is None:
            raise RuntimeError("nothing has been approved; no write is permitted")

        state = state.model_copy(deep=True)
        for key, value in self.render(state).items():
            setattr(state, key, value)
        return state

    def build(self, checkpointer=None):
        graph = StateGraph(MemoState)

        graph.add_node("evidence", self.evidence)
        graph.add_node("analysis", self.analysis)
        graph.add_node("drafting", self.drafting)
        graph.add_node("review", self.review)
        graph.add_node("approval", self.request_approval)
        graph.add_node("render", self.render)

        graph.set_entry_point("evidence")

        # ESCALATION: the run stops here and the state carries why.
        graph.add_conditional_edges(
            "evidence", self.after_evidence, {"escalate": END, "analysis": "analysis"}
        )
        graph.add_edge("analysis", "drafting")
        graph.add_edge("drafting", "review")
        graph.add_conditional_edges(
            "review", self.after_review, {"drafting": "drafting", "approval": "approval"}
        )
        # APPROVAL: the run stops here until an analyst answers. Resuming with
        # approved_memo set continues to the render node, and only then is
        # anything written.
        graph.add_conditional_edges(
            "approval", self.after_approval, {"render": "render", "wait": END}
        )
        graph.add_edge("render", END)

        return graph.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: Path | None = None):
    """A context manager yielding the SqliteSaver, as LangGraph requires."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver.from_conn_string(str(path or CHECKPOINT_DB))


# --- Running ----------------------------------------------------------------


def interrupt_type(state: MemoState) -> Literal["ESCALATION", "APPROVAL"] | None:
    """Which interrupt, if any, the run is sitting at.

    An escalation outranks an approval: a run that could not gather its evidence
    is not waiting for permission to write.
    """
    if state.escalation is not None:
        return "ESCALATION"
    if state.approval_request is not None and state.approved_memo is None:
        return "APPROVAL"
    return None


def run(
    application_number: str,
    graph: MemoGraph | None = None,
    checkpointer=None,
) -> MemoState:
    """Run to the first interrupt, or to completion."""
    graph = graph or MemoGraph()
    compiled = graph.build(checkpointer)
    result = compiled.invoke(MemoState(application_number=application_number))
    return MemoState.model_validate(result)


def resolve_escalation(
    state: MemoState,
    field_name: str,
    value: Any,
    analyst: str,
    reason: str = "supplied by the analyst",
) -> MemoState:
    """Accept a value from the analyst for a field the agent could not resolve.

    The value enters the ledger with `source_kind="analyst"`, so it is as
    traceable as anything the agent found for itself, and it is visibly not
    something the file supports on its own.
    """
    state = state.model_copy(deep=True)
    state.record(field_name, value, Provenance(
        source_kind="analyst",
        detail={"supplied_by": analyst, "reason": reason},
    ))
    state.trace.append(f"{analyst} supplied {field_name} = {value!r} ({reason}).")

    if not any(name in state.unresolved for name in state.required_fields):
        state.escalation = None
        state.trace.append("All required fields are resolved; the escalation is cleared.")

    return state


def approve(state: MemoState, analyst: str, edited_memo: str | None = None) -> MemoState:
    """Approve the draft. The first point at which a write becomes permitted."""
    if state.approval_request is None:
        raise RuntimeError("nothing has been submitted for approval")
    if state.approved_memo is not None:
        return state

    from src.tools.renderer import render_markdown

    state = state.model_copy(deep=True)
    state.approved_by = analyst
    state.approved_memo = edited_memo if edited_memo is not None else render_markdown(
        state, approved_by=analyst
    )
    state.trace.append(
        f"{analyst} approved the draft"
        + (" with edits." if edited_memo is not None else ".")
    )
    return state


def reject(state: MemoState, analyst: str, reason: str) -> MemoState:
    """Reject the draft. Nothing is written and no memo is retained."""
    state = state.model_copy(deep=True)
    state.approval_request = None
    state.approved_memo = None
    state.trace.append(f"{analyst} rejected the draft: {reason}. Nothing was written.")
    return state
