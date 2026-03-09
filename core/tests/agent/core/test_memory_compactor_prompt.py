from pathlib import Path


def _prompts_py() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "core").is_dir() and (candidate / "data").is_dir():
            repo_root = candidate
            break
    else:
        raise RuntimeError(f"Could not locate repository root from {__file__!r}")
    return repo_root / "core" / "src" / "k" / "agent" / "core" / "prompts.py"


def test_compacted_actions_prompt_emphasizes_high_fidelity_details() -> None:
    text = _prompts_py().read_text(encoding="utf-8")

    # Guardrails for memory quality: keep the API contract, preserve
    # high-fidelity process details, and avoid cross-field duplication.
    required_markers = [
        "<CompactedRules>",
        "Invocation contract",
        "finish_action(referenced_memory_ids, raw_input, raw_output, input_intents, compacted_actions)",
        "Single-fact ownership (avoid repetition)",
        "Do not duplicate the same fact across fields",
        "0) `referenced_memory_ids`",
        "direct causal relationship",
        "1) `raw_input`",
        "not a raw structured dump",
        "Do **not** copy/paste the original structured payload",
        "2) `raw_output`",
        "3) `input_intents`",
        "4) `compacted_actions`",
        "Include failed attempts",
        "Exclude facts already captured in `raw_input` or `raw_output`",
        "Completeness checklist (avoid missing details)",
        "High-fidelity rule (most important)",
        "received (inputs/constraints/context)",
        "tried (actions, commands, edits, tool calls)",
        "observed (tool outputs, errors, test results, confirmations)",
        "responded (messages delivered to the user and artifacts produced)",
        "`compacted_actions` must be a list of strings.",
        "</CompactedRules>",
    ]
    for marker in required_markers:
        assert marker in text
