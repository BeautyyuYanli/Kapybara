from k.agent.core.prompts import general_prompt


def test_general_prompt_uses_agent_view_config_base_expression() -> None:
    assert "$K_CONFIG_BASE/skills" in general_prompt
