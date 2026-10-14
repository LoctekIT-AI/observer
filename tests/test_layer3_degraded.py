"""Layer 3: formats a collector cannot read are counted as inferred calls, never block a source."""
from __future__ import annotations

import json

from cowork_runtime.adapters.dsh import DshAdapter


def _dsh_record(**changes):
    record = {
        "schema_version": "component_event.v1", "event_id": "dsh:e1", "invocation_id": "dsh:i1", "run_id": "session-1",
        "runtime": "dsh", "plugin_id": "dsh-observer", "installation_id": "install-dsh-1",
        "occurred_at": "2026-10-14T08:00:00.000Z", "started_at": None, "ended_at": "2026-10-14T08:00:00.000Z",
        "parent_invocation_id": None, "evidence_level": "verified", "source_type": "dsh_native_metadata_jsonl",
        "component_kind": "skill", "component_id": "office-docx", "error_code": None, "stage": "completed",
        "observation_kind": "skill_tool_result", "evidence_source": "dsh:tools/result",
    }
    record.update(changes)
    return json.dumps(record) + "\n"


def test_dsh_reader_skips_bad_lines_and_accepts_unrecognized_calls(tmp_path):
    source = tmp_path / "component-events.jsonl"
    source.write_text(
        "not json\n"
        + _dsh_record(event_id="dsh:e1")
        + _dsh_record(event_id="dsh:e2", observation_kind="future_kind")  # this version does not accept it
        + _dsh_record(event_id="dsh:e3", invocation_id="dsh:i3", component_kind="unknown", component_id="unrecognized:dsh_tool",
                      observation_kind="unrecognized_call", evidence_level="inferred")
        + '{"half": '
    )
    adapter = DshAdapter()
    results = list(adapter.read(source))
    events = [event for event, _ in results if event is not None]
    assert [event.event_id for event in events] == ["dsh:e1", "dsh:e3"]
    assert adapter.skipped == 2
    assert results[-1][1] < source.stat().st_size  # the half-written last line is waited for
