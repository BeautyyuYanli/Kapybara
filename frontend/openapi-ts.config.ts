import { defineConfig } from "@hey-api/openapi-ts";
export default defineConfig({
  input: process.env.OPENAPI_URL ?? "http://127.0.0.1:8000/openapi.json",
  output: "src/api/generated",
  plugins: [
    "@hey-api/typescript",
    "@hey-api/sdk",
    { name: "@hey-api/client-fetch", baseUrl: false },
  ],
});
