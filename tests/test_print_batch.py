import pytest
from aethelark3d import api


def test_a_failure_on_one_printer_does_not_cancel_the_others(monkeypatch):
    seen = []
    def fake(query, **kw):
        seen.append(kw["printer_key"])
        if kw["printer_key"] == "CC2":
            raise RuntimeError("offline")
        return {"success": True, "action": "printing_started",
                "uploaded": True, "print_command_sent": True}
    monkeypatch.setattr(api, "find_and_prepare_print", fake)
    out = api.print_batch([{"model_id": "1", "printer": "CC1"},
                           {"model_id": "2", "printer": "CC2"},
                           {"model_id": "3", "printer": "CC1"}])
    assert seen == ["CC1", "CC2", "CC1"]
    assert [j["model_id"] for j in out["started"]] == ["1", "3"]
    assert out["failed"][0]["printer"] == "CC2"


def test_jobs_run_in_selection_order(monkeypatch):
    order = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: order.append(q) or {"success": True})
    api.print_batch([{"model_id": "9", "printer": "CC1"},
                     {"model_id": "4", "printer": "CC2"}])
    assert order == ["9", "4"]


# --- Edge cases beyond the brief -------------------------------------------

def test_empty_job_list_returns_empty_result(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: calls.append(q) or {"success": True})
    out = api.print_batch([])
    assert out == {"started": [], "failed": []}
    assert calls == []


def test_a_job_with_no_printer_is_failed_not_defaulted(monkeypatch):
    """The failure mode this whole design exists to avoid: routing to a machine
    nobody chose. Covers both an explicit empty string and the key being absent
    entirely -- find_and_prepare_print must not be called for either job."""
    calls = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: calls.append(kw.get("printer_key")) or {"success": True})
    out = api.print_batch([{"model_id": "1", "printer": ""},
                           {"model_id": "2"}])
    assert calls == []
    assert [j["model_id"] for j in out["failed"]] == ["1", "2"]
    assert all(j["error"] == "no printer named" for j in out["failed"])
    assert out["started"] == []


def test_duplicate_model_on_two_printers_both_attempted(monkeypatch):
    """The same model picked for two machines is two independent jobs, not a
    dedup'd one -- both must run, each against its own printer."""
    seen = []
    def fake(query, **kw):
        seen.append((query, kw["printer_key"]))
        return {"success": True, "action": "printing_started",
                "uploaded": True, "print_command_sent": True}
    monkeypatch.setattr(api, "find_and_prepare_print", fake)
    out = api.print_batch([{"model_id": "42", "printer": "CC1"},
                           {"model_id": "42", "printer": "CC2"}])
    assert seen == [("42", "CC1"), ("42", "CC2")]
    assert [j["printer"] for j in out["started"]] == ["CC1", "CC2"]
    assert len(out["started"]) == 2


def test_job_missing_model_id_key_is_short_circuited_like_a_missing_printer(monkeypatch):
    """A malformed job dict must not reach the search at all.

    This previously documented the opposite: the missing id became the literal
    string "None" and was handed to find_and_prepare_print to be rejected
    downstream. That fails safe only if the search happens to return nothing,
    and "probably nothing" is not a property to give the code path that puts
    plastic on a bed. A missing printer already short-circuited by
    construction; a missing model now does too.
    """
    calls = []
    def fake(query, **kw):
        calls.append(query)
        return {"success": True}
    monkeypatch.setattr(api, "find_and_prepare_print", fake)
    out = api.print_batch([{"printer": "CC1"}])
    assert calls == [], "a job with no model reached the search"
    assert out["started"] == []
    assert out["failed"][0]["error"] == "no model named"

def test_a_job_with_no_model_named_is_refused_and_never_attempted():
    """str(None) is "None", which the search would take as a query and try.

    A missing printer already fails by construction. A missing model has to
    fail the same way rather than relying on a search returning nothing.
    """
    calls = []
    import aethelark3d.api as api_mod
    original = api_mod.find_and_prepare_print
    api_mod.find_and_prepare_print = lambda q, **kw: calls.append(q) or {
        "success": True, "action": "printing_started",
        "uploaded": True, "print_command_sent": True}
    try:
        out = api_mod.print_batch([{"printer": "CC1"},
                                   {"model_id": None, "printer": "CC1"},
                                   {"model_id": "", "printer": "CC1"},
                                   {"model_id": "5", "printer": "CC1"}])
    finally:
        api_mod.find_and_prepare_print = original
    assert calls == ["5"], f"a job with no model was attempted: {calls}"
    assert [j["model_id"] for j in out["started"]] == ["5"]
    assert len(out["failed"]) == 3
    assert all("no model named" in f["error"] for f in out["failed"])


# --- C2 (final review): an unreachable printer must not be reported started -

def test_a_sliced_only_job_is_not_reported_as_started(monkeypatch):
    """C2: find_and_prepare_print returns success=True unconditionally once
    slicing works, even when the driver never reached the printer. Real shape
    for an offline CC2, reproduced from the review's probe."""
    monkeypatch.setattr(api, "find_and_prepare_print", lambda q, **kw: {
        "success": True, "action": "sliced_only", "model_title": "Watch Stand",
        "printer": kw["printer_key"], "filament": "Default Loaded",
        "model_file": "/tmp/x.3mf", "gcode_file": "/tmp/x.gcode",
        "uploaded": False, "print_command_sent": False,
    })
    out = api.print_batch([{"model_id": "123456", "printer": "CC2"}])
    assert out["started"] == []
    assert out["failed"] == [{"model_id": "123456", "printer": "CC2",
                              "error": "sliced, but could not reach the printer"}]


def test_an_uploaded_but_unstarted_job_is_not_reported_as_started(monkeypatch):
    """Same defect, other half: start_print() returning False (busy/rejected)
    yields action="uploaded", uploaded=True, still not actually printing."""
    monkeypatch.setattr(api, "find_and_prepare_print", lambda q, **kw: {
        "success": True, "action": "uploaded", "uploaded": True,
        "print_command_sent": False, "printer": kw["printer_key"],
    })
    out = api.print_batch([{"model_id": "7", "printer": "CC1"}])
    assert out["started"] == []
    assert out["failed"] == [{"model_id": "7", "printer": "CC1",
                              "error": "uploaded to the printer, but it did not start printing"}]


def test_a_real_started_job_is_still_reported_started(monkeypatch):
    """Regression guard the other direction: a genuine success must not get
    caught by the tightened predicate."""
    monkeypatch.setattr(api, "find_and_prepare_print", lambda q, **kw: {
        "success": True, "action": "printing_started", "uploaded": True,
        "print_command_sent": True, "printer": kw["printer_key"],
    })
    out = api.print_batch([{"model_id": "1", "printer": "CC1"}])
    assert out["failed"] == []
    assert [j["model_id"] for j in out["started"]] == ["1"]


# --- C3 (final review): one malformed job must not abort the whole batch ----

def test_a_non_dict_entry_is_recorded_failed_and_the_batch_continues(monkeypatch):
    """The exact confirmed repro: a string where a job object was expected,
    sandwiched between two good jobs. Both good jobs must still run -- one of
    them AFTER the bad entry, which is the part that used to be lost."""
    seen = []
    def fake(query, **kw):
        seen.append((query, kw["printer_key"]))
        return {"success": True, "print_command_sent": True}
    monkeypatch.setattr(api, "find_and_prepare_print", fake)
    out = api.print_batch([{"model_id": "124686", "printer": "CC1"},
                           "oops",
                           {"model_id": "999", "printer": "CC2"}])
    assert seen == [("124686", "CC1"), ("999", "CC2")]
    assert [j["model_id"] for j in out["started"]] == ["124686", "999"]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["model_id"] is None
    assert "str" in out["failed"][0]["error"]


def test_a_null_element_among_jobs_is_recorded_failed_not_a_crash(monkeypatch):
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: {"success": True, "print_command_sent": True})
    out = api.print_batch([{"model_id": "1", "printer": "CC1"}, None])
    assert [j["model_id"] for j in out["started"]] == ["1"]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["model_id"] is None


def test_jobs_as_a_json_object_instead_of_a_list_does_not_crash(monkeypatch):
    """A plausible LLM slip: the whole `jobs` argument arrives as one object
    rather than an array of them. Must not dispatch anything and must not
    raise -- a clear top-level failure, not a per-key guessing game."""
    calls = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: calls.append(1) or {"success": True, "print_command_sent": True})
    out = api.print_batch({"model_id": "1", "printer": "CC1"})
    assert calls == []
    assert out["started"] == []
    assert len(out["failed"]) == 1


def test_jobs_as_a_bare_string_does_not_crash(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: calls.append(1) or {"success": True, "print_command_sent": True})
    out = api.print_batch("oops")
    assert calls == []
    assert out["started"] == []
    assert len(out["failed"]) == 1


# ---- bed levelling is a choice, not a constant ----

def test_levelling_is_on_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: seen.update(kw) or {"success": True,
                                                            "print_command_sent": True})
    api.print_batch([{"model_id": "1", "printer": "CC1"}])
    assert seen["auto_level"] is True


def test_a_job_can_ask_for_no_levelling(monkeypatch):
    """A batch can mix a first print that levels with later ones that need not."""
    seen = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: seen.append(kw["auto_level"]) or
                        {"success": True, "print_command_sent": True})
    api.print_batch([{"model_id": "1", "printer": "CC1", "auto_level": True},
                     {"model_id": "2", "printer": "CC1", "auto_level": False}])
    assert seen == [True, False]


@pytest.mark.parametrize("given,expected", [(None, True), (True, True), (False, False)])
def test_an_absent_or_null_levelling_flag_means_level(monkeypatch, given, expected):
    """Absent must mean the safe default, not the falsy one."""
    seen = []
    monkeypatch.setattr(api, "find_and_prepare_print",
                        lambda q, **kw: seen.append(kw["auto_level"]) or
                        {"success": True, "print_command_sent": True})
    job = {"model_id": "1", "printer": "CC1"}
    if given is not None or True:
        job["auto_level"] = given
    api.print_batch([job])
    assert seen == [expected]
