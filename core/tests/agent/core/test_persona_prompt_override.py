from __future__ import annotations

from k.agent.core.agent import agent


def test_agent_does_not_register_persona_system_prompt() -> None:
    runners = agent._system_prompt_functions
    prompt_function_names: list[str] = [
        str(getattr(getattr(runner, "function", None), "__name__", ""))
        for runner in runners
    ]

    assert "persona_prompt_from_fs" not in prompt_function_names


def test_agent_registers_preferences_before_concat_skills_prompt() -> None:
    runners = agent._system_prompt_functions
    prompt_function_names: list[str] = [
        str(getattr(getattr(runner, "function", None), "__name__", ""))
        for runner in runners
    ]

    assert "preferences_system_prompt" in prompt_function_names
    assert "concat_skills_prompt" in prompt_function_names
    assert prompt_function_names.index(
        "preferences_system_prompt"
    ) < prompt_function_names.index("concat_skills_prompt")
