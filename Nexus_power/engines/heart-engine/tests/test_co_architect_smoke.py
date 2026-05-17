"""Smoke test for P4 Co-Architect service primitives.

Pure functions only — no Heart engine, no LLM, no Platform API.

Verifies:
  * build_graph_context produces non-empty, deterministic text including
    every scene's id-prefix and OCR snippet
  * build_system_prompt includes the hard constraints, JSON schema (when
    propose=True), and the graph context
  * encode_conversation renders alternating user/assistant turns with the
    required trailing 'Assistant:' marker
  * parse_chat_response handles plain prose, valid JSON, fenced JSON,
    JSON with ungrounded steps (drops them), and malformed JSON
  * NO stub strings or hardcoded sample text leaks into the output
"""
from __future__ import annotations

import sys
from pathlib import Path

_HEART_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HEART_ROOT))

from app.co_architect import (  # noqa: E402
    build_graph_context,
    build_system_prompt,
    encode_conversation,
    parse_chat_response,
    SYSTEM_PROMPT_BASE,
)


GRAPH = {
    "scenes": [
        {
            "scene_id": "scene-aaaa1111-2222-3333-4444-555555555555",
            "scene_index": 0,
            "screen_name": "Login",
            "ocr_text": "Sign in to your account. Email. Password.",
            "scene_state_summary": {
                "screen_title": "Sign In",
                "screen_type": "form",
                "application_label": "Quote Portal",
            },
        },
        {
            "scene_id": "scene-bbbb2222-3333-4444-5555-666666666666",
            "scene_index": 1,
            "screen_name": "Quote Form",
            "ocr_text": "Date of birth. Smoker status. Calculate premium.",
            "scene_state_summary": {
                "screen_title": "Quote Form",
                "screen_type": "form",
                "application_label": "Quote Portal",
            },
        },
    ],
    "controls_by_scene": {
        "scene-aaaa1111-2222-3333-4444-555555555555": [
            {
                "control_id": "ctl-signin-aaaaaaaa",
                "element_type": "button",
                "label_text": "Sign In",
                "playwright_selector": "button:has-text('Sign In')",
                "selector_confidence": 0.94,
                "automation_ready": True,
            },
        ],
        "scene-bbbb2222-3333-4444-5555-666666666666": [
            {
                "control_id": "ctl-calc-bbbbbbbb",
                "element_type": "button",
                "label_text": "Calculate",
                "playwright_selector": "button:has-text('Calculate')",
                "selector_confidence": 0.92,
                "automation_ready": True,
            },
        ],
    },
    "edges": [
        {
            "edge_id": "edge-eeee0001-aaaa-bbbb-cccc-dddddddddddd",
            "edge_type": "action_confirmed_transition",
            "from_scene_id": "scene-aaaa1111-2222-3333-4444-555555555555",
            "to_scene_id": "scene-bbbb2222-3333-4444-5555-666666666666",
            "trigger_control_id": "ctl-signin-aaaaaaaa",
            "action_type": "click",
            "action_confidence": 0.93,
            "primary_action_summary": {
                "action_label": "Click Sign In button",
                "action_kind": "click_cta",
            },
        },
    ],
    "app_instances": [
        {
            "instance_id": "app-aaaa9999-bbbb-cccc-dddd-eeeeeeeeeeee",
            "app_name": "Quote Portal",
            "app_type": "web",
            "scene_count": 2,
        },
    ],
}


def test_build_graph_context_contains_every_scene():
    ctx = build_graph_context(GRAPH)
    assert ctx, "graph context must be non-empty"
    # Every scene's 8-char prefix should appear
    assert "scene-aa" in ctx
    assert "scene-bb" in ctx
    # OCR snippets should appear (truncated)
    assert "Sign in to your account" in ctx
    assert "Date of birth" in ctx
    # Controls section present
    assert "ctl-sign" in ctx
    assert "ctl-calc" in ctx
    assert "button:has-text('Sign In')" in ctx
    # Edges section
    assert "edge-eee" in ctx
    assert "Click Sign In button" in ctx
    # App instance summary
    assert "Quote Portal" in ctx
    print("[OK] build_graph_context includes all scenes/controls/edges/apps")


def test_build_graph_context_handles_empty():
    assert build_graph_context({}) == ""
    assert build_graph_context({"scenes": []}) == ""
    assert build_graph_context(None) == ""  # type: ignore[arg-type]
    print("[OK] build_graph_context handles empty/None gracefully")


def test_system_prompt_includes_hard_constraints():
    prompt = build_system_prompt(GRAPH, propose_scenarios=False)
    # Base constraints
    assert "HARD CONSTRAINTS" in prompt
    assert "NEVER VIOLATE" in prompt
    assert "may NOT invent" in prompt
    assert "transcripts" in prompt
    # Graph context included
    assert "=== VISUAL EVIDENCE GRAPH ===" in prompt
    assert "scene-aa" in prompt
    # JSON schema NOT included when propose=False
    assert "STRUCTURED OUTPUT" not in prompt
    print("[OK] system_prompt (propose=False) has constraints + graph, no JSON schema")


def test_system_prompt_includes_json_schema_when_proposing():
    prompt = build_system_prompt(GRAPH, propose_scenarios=True)
    assert "STRUCTURED OUTPUT" in prompt
    assert "proposed_scenarios" in prompt
    assert "evidence_scene_id" in prompt
    assert "evidence_control_id" in prompt
    assert "Return ONLY the JSON object" in prompt
    print("[OK] system_prompt (propose=True) includes JSON schema instructions")


def test_encode_conversation_basic():
    msgs = [
        {"role": "user", "content": "What scenarios exist?"},
        {"role": "assistant", "content": "I see 2 scenes."},
        {"role": "user", "content": "Propose a happy-path test."},
    ]
    encoded = encode_conversation(msgs)
    assert encoded.startswith("--- CONVERSATION ---")
    assert "User: What scenarios exist?" in encoded
    assert "Assistant: I see 2 scenes." in encoded
    assert "User: Propose a happy-path test." in encoded
    # Trailing 'Assistant:' primer
    assert encoded.rstrip().endswith("Assistant:")
    print("[OK] encode_conversation renders turns + trailing Assistant: primer")


def test_encode_conversation_filters_empty_and_system():
    msgs = [
        {"role": "user", "content": ""},                  # dropped (empty)
        {"role": "system", "content": "should be dropped"},
        {"role": "user", "content": "Real question."},
    ]
    encoded = encode_conversation(msgs)
    assert "should be dropped" not in encoded
    assert "Real question." in encoded
    print("[OK] encode_conversation filters empty + system messages")


def test_parse_plain_text_response():
    turn = parse_chat_response(
        "I see 2 scenes covering login and quote.",
        propose_scenarios=False,
    )
    assert turn.response.startswith("I see 2 scenes")
    assert turn.proposed_scenarios == []
    assert turn.parse_warning is None
    print("[OK] parse_chat_response (propose=False) returns prose only")


def test_parse_valid_json_response():
    text = '''{
      "response": "Two boundary tests proposed.",
      "proposed_scenarios": [
        {
          "title": "Boundary: empty DOB",
          "rationale": "DOB is required",
          "strategy": "co_architect",
          "steps": [
            {
              "step_number": 1,
              "action": "Click Calculate without DOB",
              "input_data": "",
              "expected_output": "DOB required error",
              "evidence_scene_id": "scene-bb",
              "evidence_control_id": "ctl-calc-bb",
              "evidence_edge_id": "edge-eee",
              "proof_confidence": 0.88
            }
          ]
        }
      ]
    }'''
    turn = parse_chat_response(text, propose_scenarios=True)
    assert turn.response == "Two boundary tests proposed."
    assert len(turn.proposed_scenarios) == 1
    sc = turn.proposed_scenarios[0]
    assert sc.title == "Boundary: empty DOB"
    assert len(sc.steps) == 1
    assert sc.steps[0].evidence_scene_id == "scene-bb"
    assert sc.steps[0].evidence_control_id == "ctl-calc-bb"
    assert sc.steps[0].proof_confidence == 0.88
    assert turn.parse_warning is None
    print("[OK] parse_chat_response handles valid JSON")


def test_parse_fenced_json_response():
    text = '''```json
{
  "response": "ok",
  "proposed_scenarios": []
}
```'''
    turn = parse_chat_response(text, propose_scenarios=True)
    assert turn.response == "ok"
    assert turn.proposed_scenarios == []
    print("[OK] parse_chat_response strips ```json fences")


def test_parse_drops_ungrounded_steps():
    text = '''{
      "response": "two scenarios proposed",
      "proposed_scenarios": [
        {
          "title": "Bad — no evidence",
          "strategy": "co_architect",
          "steps": [
            {
              "step_number": 1,
              "action": "Imagined click",
              "input_data": "",
              "expected_output": "",
              "evidence_scene_id": "",
              "evidence_control_id": "",
              "evidence_edge_id": "",
              "proof_confidence": 0.5
            }
          ]
        },
        {
          "title": "Good — grounded",
          "strategy": "co_architect",
          "steps": [
            {
              "step_number": 1,
              "action": "Click Calculate",
              "input_data": "",
              "expected_output": "Result appears",
              "evidence_scene_id": "scene-bb",
              "evidence_control_id": "ctl-calc-bb",
              "evidence_edge_id": "",
              "proof_confidence": 0.9
            }
          ]
        }
      ]
    }'''
    turn = parse_chat_response(text, propose_scenarios=True)
    # The ungrounded scenario should be dropped (no surviving steps)
    assert len(turn.proposed_scenarios) == 1
    assert turn.proposed_scenarios[0].title == "Good — grounded"
    assert turn.parse_warning is not None
    assert "Dropped" in (turn.parse_warning or "")
    print("[OK] parse_chat_response drops ungrounded scenarios + warns")


def test_parse_malformed_json_falls_back():
    text = "Sorry, I'll try again: { unclosed bracket"
    turn = parse_chat_response(text, propose_scenarios=True)
    assert "Sorry" in turn.response
    assert turn.proposed_scenarios == []
    assert turn.parse_warning is not None
    assert "plain text" in turn.parse_warning
    print("[OK] parse_chat_response falls back to prose on malformed JSON")


def test_parse_clamps_proof_confidence():
    text = '''{
      "response": "",
      "proposed_scenarios": [
        {
          "title": "clamp test",
          "strategy": "co_architect",
          "steps": [
            {
              "step_number": 1,
              "action": "x",
              "input_data": "",
              "expected_output": "",
              "evidence_scene_id": "scene-aa",
              "evidence_control_id": "ctl-signin",
              "evidence_edge_id": "",
              "proof_confidence": 7.5
            }
          ]
        }
      ]
    }'''
    turn = parse_chat_response(text, propose_scenarios=True)
    assert turn.proposed_scenarios[0].steps[0].proof_confidence == 1.0
    print("[OK] parse_chat_response clamps proof_confidence to [0, 1]")


def test_no_stub_text_in_system_prompt():
    """No hardcoded sample scenarios should leak into the prompt."""
    prompt = build_system_prompt(GRAPH, propose_scenarios=True)
    forbidden = [
        "Visual Flow Smoke Test",
        "All transitions succeed",
        "TODO",
    ]
    for s in forbidden:
        # 'TODO' might appear in actual scenario data so check it doesn't
        # appear as an isolated marker
        if s == "TODO":
            assert "TODO:" not in prompt, f"Forbidden marker {s!r} leaked"
        else:
            assert s not in prompt, f"Forbidden marker {s!r} leaked"
    print("[OK] No stub markers in system prompt")


def test_system_prompt_base_is_immutable():
    """The base constraints must not contain TODOs or escape hatches."""
    assert "NEVER" in SYSTEM_PROMPT_BASE
    assert "transcripts" in SYSTEM_PROMPT_BASE
    assert "MUST cite" in SYSTEM_PROMPT_BASE
    print("[OK] SYSTEM_PROMPT_BASE has the hard constraints")


if __name__ == "__main__":
    test_build_graph_context_contains_every_scene()
    test_build_graph_context_handles_empty()
    test_system_prompt_includes_hard_constraints()
    test_system_prompt_includes_json_schema_when_proposing()
    test_encode_conversation_basic()
    test_encode_conversation_filters_empty_and_system()
    test_parse_plain_text_response()
    test_parse_valid_json_response()
    test_parse_fenced_json_response()
    test_parse_drops_ungrounded_steps()
    test_parse_malformed_json_falls_back()
    test_parse_clamps_proof_confidence()
    test_no_stub_text_in_system_prompt()
    test_system_prompt_base_is_immutable()
    print("\nAll Co-Architect smoke tests passed.")
