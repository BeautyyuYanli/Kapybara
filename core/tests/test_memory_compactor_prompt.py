from pathlib import Path


def _prompts_py() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "core" / "src" / "k" / "agent" / "core" / "prompts.py"


def test_compacted_actions_prompt_emphasizes_high_fidelity_details() -> None:
    text = _prompts_py().read_text(encoding="utf-8")

    # Guardrails for memory quality: preserve what the agent received, tried,
    # observed, and responded (including failures when they matter).
    assert "High-fidelity rule (most important)" in text
    assert "received (inputs/constraints/context)" in text
    assert "tried (actions, commands, edits, tool calls)" in text
    assert "observed (tool outputs, errors, test results, confirmations)" in text
    assert "responded (messages delivered to the user and artifacts produced)" in text
    assert "Include failed attempts" in text
    assert "0) `referenced_memory_ids`" in text
    assert "direct causal relationship" in text
    assert "1) `raw_input`" in text
    assert "not a raw structured dump" in text
    assert "Do **not** copy/paste the original structured payload" in text
    assert "2) `raw_output`" in text
    assert "3) `input_intents`" in text
