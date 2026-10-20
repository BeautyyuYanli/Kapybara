export type ExecutionReference = {
  session_id: string;
  activity_id: string;
  run_id: string;
};

// Error bodies are not generated response DTOs. Retain only the safe identity
// needed to recover a request, never arbitrary upstream response fields.
export function readExecutionReference(value: unknown): ExecutionReference | undefined {
  if (!value || typeof value !== "object") return;
  if (!("session_id" in value) || !("activity_id" in value) || !("run_id" in value)) return;
  const { session_id, activity_id, run_id } = value;
  if (
    typeof session_id !== "string" || typeof activity_id !== "string" ||
    typeof run_id !== "string" || !run_id
  ) return;
  return { session_id, activity_id, run_id };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
    public reference?: ExecutionReference,
  ) {
    super("API request failed");
  }
}
export function describeError(error: unknown, names: string[] = []) {
  const fields: Record<string, string> = {};
  let message = "请求失败，请稍后重试。";
  if (error instanceof ApiError) {
    const messages: Record<number, string> = {
      404: "资源不存在。",
      409: "配置重名或仍被 Session 引用，请检查后重试。",
      422: "配置无效，请检查输入。",
      502: "模型发现失败，请稍后重试。",
    };
    message = messages[error.status] ?? message;
    if (error.status === 422 && Array.isArray(error.detail)) {
      const unmapped: string[] = [];
      for (const item of error.detail) {
        if (!item || typeof item !== "object" || typeof item.msg !== "string") continue;
        const name = Array.isArray(item.loc)
          ? item.loc.find((key: unknown) => typeof key === "string" && names.includes(key))
          : undefined;
        if (name) fields[name] = item.msg;
        else unmapped.push(item.msg);
      }
      if (unmapped.length) message = unmapped.join("；");
    }
  } else if (error instanceof TypeError) message = "网络连接失败，请检查连接后重试。";
  return { message, fields };
}
