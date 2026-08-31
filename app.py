"""Streamlit UI.

Four things only: pick an application, watch the run, answer an escalation,
answer an approval. There is no chat interface over the memo and no batch
processing - both would blur the point that an analyst is accountable for one
memo at a time.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from config import ANTHROPIC_API_KEY, NEBIUS_API_KEY, POLICY_VERSION
from src.agents.drafting import OfflineDrafter, drafting_node
from src.agents.evidence import EvidenceAgent
from src.agents.review import NoModelReviewer, ReviewAgent
from src.graph import (
    MemoGraph,
    approve,
    interrupt_type,
    reject,
    resolve_escalation,
    run,
)
from src.state import MemoState
from src.tools import database as db
from src.tools.calculators import format_cents
from src.tools.extraction import LocalTableExtractor
from src.tools.models import model_identifiers, read_call_log
from src.tools.renderer import render_markdown

st.set_page_config(page_title="Credit memo agent", layout="wide")

OUTPUT_DIR = Path("output")


class OfflineGraph(MemoGraph):
    """Every model call replaced by a deterministic stand-in, for running without
    credentials. The orchestration, ledger and provenance rules are unchanged."""

    def drafting(self, state: MemoState) -> dict:
        notes = [note.note for note in state.review_notes if note.must_fix]
        return drafting_node(state, OfflineDrafter(state), revision_notes=notes or None)


def build_graph() -> MemoGraph:
    offline = not (NEBIUS_API_KEY and ANTHROPIC_API_KEY)
    if offline:
        return OfflineGraph(
            evidence_agent=EvidenceAgent(extractor=LocalTableExtractor()),
            review_agent=ReviewAgent(NoModelReviewer()),
            output_dir=OUTPUT_DIR,
        )
    return MemoGraph(output_dir=OUTPUT_DIR)


def state() -> MemoState | None:
    return st.session_state.get("memo_state")


def set_state(value: MemoState | None) -> None:
    st.session_state["memo_state"] = value


# --- Sidebar: the application picker ----------------------------------------

with st.sidebar:
    st.title("Credit memo agent")
    st.caption(
        "Produces a first-draft memo consolidating a loan application file. "
        "It does not assess, approve, decline, rate or recommend."
    )

    try:
        applications = db.list_application_numbers()
    except Exception as exc:  # noqa: BLE001
        st.error(f"{exc}")
        applications = []

    selected = st.selectbox("Application", applications, index=0 if applications else None)

    offline = not (NEBIUS_API_KEY and ANTHROPIC_API_KEY)
    if offline:
        st.warning(
            "Running offline. Extraction and narrative are produced by the "
            "deterministic local stand-ins, not by the configured models. Set "
            "NEBIUS_API_KEY and ANTHROPIC_API_KEY in .env for the real path."
        )

    if st.button("Run", type="primary", disabled=not selected, width="stretch"):
        with st.spinner(f"Gathering evidence for {selected}"):
            set_state(run(selected, build_graph()))

    if state() is not None and st.button("Clear", width="stretch"):
        set_state(None)
        st.rerun()

    st.divider()
    st.caption(f"Policy ruleset {POLICY_VERSION}")
    for role, identifier in model_identifiers().items():
        st.caption(f"{role}: {identifier}")


current = state()

if current is None:
    st.info("Choose an application and run it.")
    st.stop()


# --- The run view -----------------------------------------------------------

st.header(f"{current.application_number}")

kind = interrupt_type(current)
badge = {
    "ESCALATION": ":red[ESCALATION - the agent cannot proceed]",
    "APPROVAL": ":orange[APPROVAL - finished, requesting permission to write]",
    None: ":green[Complete]",
}[kind]
st.markdown(f"**Status** {badge}")

columns = st.columns(4)
columns[0].metric("Template", current.template_id or "-")
columns[1].metric("Sourced values", len(current.ledger))
columns[2].metric("Unresolved", len(current.unresolved))
columns[3].metric(
    "Discrepancies",
    len([f for f in current.policy_findings if f.finding_type == "discrepancy"]),
)

run_tab, ledger_tab, findings_tab, memo_tab, calls_tab = st.tabs(
    ["Run", "Evidence ledger", "Policy findings", "Draft", "Model calls"]
)

with run_tab:
    st.subheader("Node progress")
    for line in current.trace:
        st.text(line)

    st.subheader("Documents")
    st.dataframe(
        [
            {
                "File": document.filename,
                "Identified as": document.document_type,
                "Subject": document.subject_name or "-",
                "Confidence": document.classification_confidence,
                "Read": "yes" if document.parse_ok else f"no - {document.parse_error}",
            }
            for document in current.documents
        ],
        width="stretch",
        hide_index=True,
    )

with ledger_tab:
    st.caption(
        "Every value the memo may state, with its source. Nothing reaches the memo "
        "that is not here."
    )
    st.dataframe(
        [
            {
                "Field": name,
                "Value": (
                    format_cents(item.value)
                    if isinstance(item.value, int) and name.endswith(
                        ("_amount", "_income", "_repayments", "_surplus"))
                    else str(item.value)
                ),
                "Source": item.provenance.source_kind,
                "Provenance": item.provenance.describe(),
            }
            for name, item in sorted(current.ledger.items())
        ],
        width="stretch",
        hide_index=True,
    )

    if current.verifications:
        st.subheader("Identity verification")
        st.dataframe(
            [
                {
                    "Field": v.field_name,
                    "Record": str(v.record_value.value) if v.record_value else "not found",
                    "Document": str(v.document_value.value) if v.document_value else "not found",
                    "Outcome": v.status,
                }
                for v in current.verifications
            ],
            width="stretch",
            hide_index=True,
        )

with findings_tab:
    if not current.policy_findings:
        st.info("Policy has not been evaluated - the run stopped before the analysis node.")
    else:
        st.caption(
            "`Outside parameter` records that a figure sits outside a policy "
            "parameter. It is not a finding about the merits of the application."
        )
        st.dataframe(
            [
                {
                    "Rule": f.rule_id,
                    "Subject": f.subject or "-",
                    "Observed": f.observed_value or "-",
                    "Parameter": f.parameter or "-",
                    "Status": f.status,
                    "Type": f.finding_type,
                }
                for f in current.policy_findings
            ],
            width="stretch",
            hide_index=True,
        )

    if current.review_notes:
        st.subheader("Review notes")
        for note in current.review_notes:
            marker = ":red[must fix]" if note.must_fix else "note"
            st.markdown(f"- **{note.category}** ({note.section}) {marker} - {note.note}")

with memo_tab:
    if current.draft_sections:
        st.markdown(render_markdown(current, current.approved_by))
    else:
        st.info("No draft. The run stopped at an escalation before drafting.")

with calls_tab:
    records = read_call_log(limit=50)
    if not records:
        st.info("No model calls logged.")
    else:
        st.dataframe(
            [
                {
                    "Time": record["timestamp"],
                    "Purpose": record["purpose"],
                    "Model": record["model"],
                    "In": record.get("input_tokens"),
                    "Out": record.get("output_tokens"),
                    "ms": record.get("duration_ms"),
                    "Error": record.get("error") or "",
                }
                for record in records
            ],
            width="stretch",
            hide_index=True,
        )


# --- Escalation panel -------------------------------------------------------

if kind == "ESCALATION":
    st.divider()
    st.subheader(":red[Escalation]")
    st.write(current.escalation.summary)

    # Not every escalation is about an unresolved ledger field. Drafting escalates
    # when the model is unreachable, and there is no value for the analyst to
    # supply in that case - only something to be told.
    if not any(name in current.unresolved for name in current.escalation.fields):
        st.code(current.escalation.detail, language=None)

    for field_name in current.escalation.fields:
        entry = current.unresolved.get(field_name)
        if entry is None:
            continue

        with st.expander(f"{field_name} - {entry.reason}", expanded=True):
            st.code(entry.describe(), language=None)

            if entry.conflicting_values:
                st.caption("Choose the value that is correct, or type another.")
                options = [str(v.value) for v in entry.conflicting_values]
                choice = st.radio(
                    "Value", options + ["Something else"], key=f"choice_{field_name}"
                )
                supplied = (
                    st.text_input("Value", key=f"other_{field_name}")
                    if choice == "Something else"
                    else choice
                )
            else:
                supplied = st.text_input(
                    "Value to supply", key=f"supply_{field_name}",
                    placeholder="Money as whole cents, e.g. 10790000",
                )

            reason = st.text_input(
                "Why this value is correct", key=f"reason_{field_name}",
                placeholder="e.g. confirmed against the passport; the KYC record is misspelled",
            )

            if st.button("Supply this value", key=f"apply_{field_name}"):
                if not supplied or not reason:
                    st.error("Both the value and the reason are needed.")
                else:
                    value: object = supplied
                    if supplied.lstrip("-").isdigit():
                        value = int(supplied)
                    updated = resolve_escalation(
                        current, field_name, value, "analyst", reason
                    )
                    if updated.escalation is None:
                        updated = MemoState.model_validate(
                            build_graph().build().invoke(updated)
                        )
                    set_state(updated)
                    st.rerun()

    st.divider()
    if st.button("Abandon this run"):
        set_state(None)
        st.rerun()


# --- Approval panel ---------------------------------------------------------

if kind == "APPROVAL":
    st.divider()
    st.subheader(":orange[Approval]")
    st.write(current.approval_request.summary)

    if current.approval_request.outstanding_notes:
        st.warning("Review notes not addressed within the revision cap:")
        for note in current.approval_request.outstanding_notes:
            st.markdown(f"- {note}")

    analyst = st.text_input("Approving analyst", value="", placeholder="Your name")
    edited = st.text_area(
        "Memo (edit before approving if you wish)",
        value=render_markdown(current, analyst or None),
        height=400,
    )

    approve_column, reject_column = st.columns(2)

    with approve_column:
        if st.button("Approve and write", type="primary", width="stretch"):
            if not analyst:
                st.error("An approving analyst is required before anything is written.")
            else:
                original = render_markdown(current, analyst)
                approved = approve(
                    current, analyst,
                    edited_memo=edited if edited != original else None,
                )
                set_state(build_graph().finalise(approved))
                st.rerun()

    with reject_column:
        rejection = st.text_input("Reason for rejecting", key="rejection")
        if st.button("Reject", width="stretch"):
            if not rejection:
                st.error("A reason is required.")
            else:
                set_state(reject(current, analyst or "analyst", rejection))
                st.rerun()


# --- Written ----------------------------------------------------------------

if current.rendered_path:
    st.divider()
    st.success(f"Written to {current.rendered_path}")
    path = Path(current.rendered_path)
    if path.exists():
        st.download_button(
            "Download the memo",
            data=path.read_bytes(),
            file_name=path.name,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
