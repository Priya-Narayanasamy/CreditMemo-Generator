"""Phase 8 tests: the Streamlit UI.

Driven through Streamlit's own AppTest harness, so these exercise the real script
rather than a 200 from the web server. The UI is the only place a human can
authorise a write, so the tests that matter are the ones about what the buttons
do and do not do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import DB_PATH, DOCUMENTS_DIR

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)

TIMEOUT = 120


APP_SCRIPT = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture()
def offline_credentials(monkeypatch):
    """Force the UI onto the offline path.

    app.py chooses real models whenever credentials are present, so without this
    the UI tests hit the live API as soon as anyone fills in .env - turning a
    fast, deterministic suite into a slow, costly and flaky one that depends on
    the machine it runs on. AppTest re-executes app.py per run, so patching the
    config module is enough.
    """
    import config

    monkeypatch.setattr(config, "NEBIUS_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    # app.py asks src.tools.models whether the configured provider has its key,
    # and that module bound the values at import time.
    monkeypatch.setattr("src.tools.models.NEBIUS_API_KEY", "")
    monkeypatch.setattr("src.tools.models.ANTHROPIC_API_KEY", "")


@pytest.fixture()
def app(tmp_path, monkeypatch, offline_credentials):
    # The app writes into ./output, so each test gets its own working directory.
    monkeypatch.chdir(tmp_path)
    return AppTest.from_file(str(APP_SCRIPT), default_timeout=TIMEOUT)


def run_application(app: AppTest, number: str) -> AppTest:
    app.run()
    app.selectbox[0].set_value(number)
    app.sidebar.button[0].click().run()
    return app


def all_text(app: AppTest) -> str:
    parts = []
    for collection in (app.markdown, app.text, app.caption, app.info,
                       app.warning, app.error, app.success):
        parts.extend(element.value for element in collection)
    return "\n".join(parts)


# --- Loading ----------------------------------------------------------------


def test_the_app_loads_without_an_exception(app):
    app.run()

    assert not app.exception


def test_the_picker_lists_every_application(app):
    app.run()

    assert len(app.selectbox[0].options) == 8
    assert "APP-2026-0001" in app.selectbox[0].options


def test_nothing_is_shown_before_a_run(app):
    app.run()

    assert any("Choose an application" in element.value for element in app.info)


def test_the_offline_banner_is_shown_without_credentials(app):
    app.run()

    assert any("offline" in element.value.lower() for element in app.sidebar.warning)


# --- The run view -----------------------------------------------------------


def test_running_a_clean_application_reaches_the_approval_panel(app):
    run_application(app, "APP-2026-0001")
    text = all_text(app)

    assert not app.exception
    assert "APPROVAL" in text
    assert "Nothing has been written" in text


def test_running_a_conflicted_application_reaches_the_escalation_panel(app):
    run_application(app, "APP-2026-0004")
    text = all_text(app)

    assert not app.exception
    assert "ESCALATION" in text
    assert "borrower_1_full_name" in text


def test_the_escalation_panel_shows_both_conflicting_values(app):
    run_application(app, "APP-2026-0004")
    options = [option for radio in app.radio for option in radio.options]

    assert "Kathryn Ellingham" in options
    assert "Katherine Ellingham" in options
    assert "Something else" in options


def test_the_escalation_panel_offers_abandoning_the_run(app):
    run_application(app, "APP-2026-0004")
    labels = [button.label for button in app.button]

    assert "Abandon this run" in labels


def test_the_ledger_is_shown_with_its_provenance(app):
    run_application(app, "APP-2026-0001")
    rows = app.dataframe[1].value.to_dict("records")

    assert any(row["Field"] == "computed_lvr" for row in rows)
    assert all(row["Provenance"] for row in rows)


def test_an_escalated_run_shows_no_draft(app):
    run_application(app, "APP-2026-0004")

    assert any("No draft" in element.value for element in app.info)


# --- Approval ---------------------------------------------------------------


def test_approving_without_a_name_writes_nothing(app, tmp_path):
    run_application(app, "APP-2026-0001")
    approve_button = next(b for b in app.button if b.label == "Approve and write")
    approve_button.click().run()

    assert any("approving analyst is required" in element.value.lower()
               for element in app.error)
    assert app.session_state["memo_state"].rendered_path is None


def test_rejecting_without_a_reason_writes_nothing(app):
    run_application(app, "APP-2026-0001")
    next(b for b in app.button if b.label == "Reject").click().run()

    assert any("reason is required" in element.value.lower() for element in app.error)
    assert app.session_state["memo_state"].approved_memo is None


def test_approving_with_a_name_writes_the_memo(app):
    run_application(app, "APP-2026-0001")
    app.text_input[0].set_value("P. Narayanasamy")
    next(b for b in app.button if b.label == "Approve and write").click().run()

    state = app.session_state["memo_state"]

    assert not app.exception
    assert state.approved_by == "P. Narayanasamy"
    assert state.rendered_path is not None
    assert any("Written to" in element.value for element in app.success)


def test_rejecting_with_a_reason_retains_no_memo(app):
    run_application(app, "APP-2026-0001")
    reason = next(i for i in app.text_input if i.label == "Reason for rejecting")
    reason.set_value("the income figure needs checking")
    next(b for b in app.button if b.label == "Reject").click().run()

    state = app.session_state["memo_state"]

    assert state.approved_memo is None
    assert state.approval_request is None
    assert state.rendered_path is None


# --- Escalation resolution --------------------------------------------------


def test_supplying_a_value_carries_the_run_to_approval(app):
    run_application(app, "APP-2026-0004")

    app.radio[0].set_value("Kathryn Ellingham")
    reason = next(i for i in app.text_input if i.label.startswith("Why this value"))
    reason.set_value("confirmed against the passport")
    next(b for b in app.button if b.label == "Supply this value").click().run()

    state = app.session_state["memo_state"]

    assert not app.exception
    assert state.escalation is None
    assert state.ledger["borrower_1_full_name"].value == "Kathryn Ellingham"
    assert state.ledger["borrower_1_full_name"].provenance.source_kind == "analyst"


def test_supplying_a_value_without_a_reason_is_refused(app):
    run_application(app, "APP-2026-0004")
    app.radio[0].set_value("Kathryn Ellingham")
    next(b for b in app.button if b.label == "Supply this value").click().run()

    assert any("reason are needed" in element.value.lower() for element in app.error)
    assert app.session_state["memo_state"].escalation is not None


# --- Clearing ---------------------------------------------------------------


def test_clearing_returns_to_the_picker(app):
    run_application(app, "APP-2026-0001")
    next(b for b in app.sidebar.button if b.label == "Clear").click().run()

    assert app.session_state["memo_state"] is None
    assert any("Choose an application" in element.value for element in app.info)


# --- An escalation that is not about a ledger field -------------------------


def test_a_drafting_failure_renders_without_crashing(app, monkeypatch):
    """Drafting escalates when the model is unreachable, naming `draft_sections`,
    which is not a field in the ledger. The escalation panel used to look every
    escalated field up in state.unresolved and raise KeyError."""
    from src.agents import drafting as drafting_module

    def unreachable(self, brief):
        raise RuntimeError("Error code: 400 - credit balance is too low")

    monkeypatch.setattr(drafting_module.OfflineDrafter, "file_overview", unreachable)

    run_application(app, "APP-2026-0001")

    assert not app.exception
    state = app.session_state["memo_state"]
    assert state.escalation is not None
    assert state.escalation.fields == ["draft_sections"]
    assert any("credit balance" in element.value for element in app.code)


def test_a_drafting_failure_offers_no_value_to_supply(app, monkeypatch):
    """There is nothing for the analyst to type in - only something to be told."""
    from src.agents import drafting as drafting_module

    def unreachable(self, brief):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(drafting_module.OfflineDrafter, "file_overview", unreachable)

    run_application(app, "APP-2026-0001")

    assert "Supply this value" not in [b.label for b in app.button]
    assert "Abandon this run" in [b.label for b in app.button]
