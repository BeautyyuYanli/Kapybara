import type { CreateProvider } from "@/api/generated";
/** JSON drafts remain text until submit; the generated DTO owns the value type. */
export function parseObject(text: string): NonNullable<CreateProvider["provider_kwargs"]> {
  const value = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("JSON object required");
  return value;
}
export function optionalInteger(text: string | number, min = 1) {
  if (String(text).trim() === "") return null;
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < min) throw new Error("Invalid integer");
  return value;
}
