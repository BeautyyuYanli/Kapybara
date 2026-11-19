import { client } from "./generated/client.gen";
import { ApiError } from "./errors";
client.setConfig({ baseUrl: window.location.origin, credentials: "same-origin" });
client.interceptors.error.use((error, response) => {
  if (!response) return error;
  const detail = error && typeof error === "object" && "detail" in error ? error.detail : undefined;
  return new ApiError(response.status, detail);
});
