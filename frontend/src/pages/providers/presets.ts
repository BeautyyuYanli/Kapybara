export const presets = [
  {
    label: "OpenAI Chat compatible",
    provider: "pydantic_ai.providers.openai:OpenAIProvider",
    model: "pydantic_ai.models.openai:OpenAIChatModel",
  },
  {
    label: "OpenAI Responses compatible",
    provider: "pydantic_ai.providers.openai:OpenAIProvider",
    model: "pydantic_ai.models.openai:OpenAIResponsesModel",
  },
  {
    label: "Google AI Studio",
    provider: "pydantic_ai.providers.google:GoogleProvider",
    model: "pydantic_ai.models.google:GoogleModel",
  },
] as const;
