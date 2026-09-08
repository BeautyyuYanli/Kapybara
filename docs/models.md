# Providers and registered models

Kapy stores model connections independently of sessions. `provider.type` selects
`openai_responses` (the default), `openai_chat`, or `google_ai_studio`. OpenAI base
URLs include `/v1`; Google base URLs are hosts or base prefixes and the native SDK
adds `/v1beta`. Neither SDK discovers keys from the server environment.

All operations below use the existing authenticated `POST /rpc` JSON-RPC endpoint
or `kapy control` through the machine daemon. Operator and trusted frontend callers
manage shared providers. An agent session can read the catalog of its bound provider
and select another registered model in that provider; it cannot enumerate other
connections, manage credentials, or use another provider merely by knowing its ID.

## Connection API

| Method | Parameters |
| --- | --- |
| `provider.create` | `request_id`, `name`, `api_key`, optional `type`, `base_url` |
| `provider.get` | `provider_id` |
| `provider.list` | optional `after_id`, `limit` (1–100) |
| `provider.update` | `provider_id`, `request_id`, `expected_revision`, `name`, `type`, `base_url`, optional `api_key` |
| `provider.delete` | `provider_id`, `request_id`, `expected_revision` |

Create/get/update return `id`, `name`, `type`, `base_url`, `has_api_key`, `revision`,
`created_at`, and `updated_at`. List returns `items` and `next_after_id`; delete returns
`{deleted:true}`. Keys never appear in these results. URLs cannot embed credentials,
queries, or fragments. Changing protocol or endpoint requires an explicit key;
otherwise an update may retain the current key. Configuration errors hide raw inputs.

Mutations and their UUID replay receipt commit together. Reusing a request ID with
changed arguments, including a changed key, conflicts. Updates/deletes also compare
`expected_revision`. There is one current provider row: rotation replaces the key;
delete clears it and marks the row deleted. Generic request JSON contains no key.
Sessions and their histories survive provider deletion. Existing running calls keep
the connection they borrowed; subsequent calls fail until another model is selected.

## Persistent model catalog

| Method | Parameters |
| --- | --- |
| `provider.discover` | `provider_id`, `request_id`, optional `page_token`, `limit` (1–100) |
| `provider.models` | `provider_id`, optional `after_id`, `limit` (1–100) |
| `provider.model.create` | `provider_id`, `request_id`, `name`, optional `defaults` |
| `provider.model.get` | `model_id` |
| `provider.model.update` | `model_id`, `request_id`, `expected_revision`, `defaults` |

Discovery fetches one bounded page, saves observations and its receipt atomically,
and returns `items` plus `next_page_token`. Repeat the call with a new request ID for
the next page or a fresh observation. A concurrent provider revision change rejects
stale observations. Repeated provider/name pairs keep their model ID. A page does not
represent the whole catalog: missing names are retained, and discovery never overwrites
manual defaults. Changing provider protocol/base clears observations but preserves
registered IDs and defaults.

`provider.models` reads the saved catalog without network I/O. It returns `items`,
`next_after_id`, and `default_model_id` (only when the entire provider catalog contains
exactly one model). Manual create supports endpoints without a models API. Existing
names conflict; use model update to replace defaults instead. Model names are immutable.

Each model view contains `id`, `provider_id`, `name`, `discovered`, `defaults`,
`revision`, `discovered_at`, `created_at`, and `updated_at`. Defaults accept nullable
`context_window_tokens` and `max_output_tokens`. An empty object clears all manual
overrides. Discovery records provider budgets when present; standard OpenAI model
metadata does not supply them, while Gemini supplies input/output token limits.

## Session selection and budgets

Session create/update use nonsecret configuration:

```json
{"config":{"model":{"model_id":"<registered-model-uuid>","context_window_tokens":null,"max_output_tokens":null},"instructions":""}}
```

No endpoint, key, protocol or raw model name belongs in session config. Recursive
creation inherits the calling session selection; changing model ID clears omitted
budget overrides. Other session update fields retain full replacement semantics;
updates require a waiting session. Old sessions without a model ID keep their history
and can be configured using session update.

Every Runner call freezes effective settings from the current provider and catalog:
session override → model defaults → discovered metadata → **262144 context / 16384
output**. Output must be positive and below context, and configured budgets cannot
exceed known provider limits. State saves explicit selections/overrides rather than
copying derived defaults permanently. Default changes affect the next call, including
recovery, but not an already running instance. Token usage still comes only from the
provider response; fallback budgets are policy, not token estimates.

Responses uses `store:false`, full local history, and no previous-response chaining
or automatic truncation. Responses keeps SDK-supported item IDs because its mapper
requires reasoning IDs to replay encrypted content. These IDs accompany complete local
items; they do not substitute for history stored on the server. Compression preserves
entire reasoning/tool blocks or removes them together. Same-provider recovery retains encrypted reasoning/thought
signatures and completed tool returns. Changing provider revision, protocol, endpoint
or model removes provider-specific state only from the model projection; raw history
is preserved. Effective budget changes invalidate previous compression usage without
removing signatures. Unknown model failures are persisted as safe errors without SDK
request details or credentials.

## CLI and Telegram

Create a local `provider.json` containing `{"name":"Primary","type":"openai_responses"}`.
From an authorized machine CLI, use:

```sh
kapy control provider create --config-file provider.json --key-file provider.key
kapy control provider discover PROVIDER_ID
kapy control provider models PROVIDER_ID
kapy control provider model create PROVIDER_ID MODEL_NAME --defaults '{"context_window_tokens":262144}'
kapy control provider model update MODEL_ID --expected-revision 1 --defaults '{"max_output_tokens":8192}'
kapy control session create 'Inspect the workspace' --model MODEL_ID --machine docker-machine
```

`--key-env VARIABLE` reads a named environment variable instead of a key file. CLI
prints only request IDs and public results. `session create/update --config-file FILE`
reads the whole session config object; `--model` always means a registered UUID.

Telegram `/providers` lists connections. `/provider ID` selects one; `/provider JSON`
creates one, or updates it when `provider_id` and `expected_revision` are supplied.
`/discover` refreshes the selected provider (an optional argument continues a page);
`/models` reads its catalog. Only a unique catalog model is selected automatically.
`/model ID` or `/model JSON` saves the model and optional budgets. `/modeldefaults JSON`
updates the selected shared model defaults with the current revision. Shared connection
and defaults changes affect later runs of all referencing sessions.

`/new` uses saved chat/topic settings. Busy sessions keep their running model; their
saved selection is available for the next `/new`. Configuration commands are never
model input. Provider commands are private durable ingress while pending; acknowledged
commands discard key-bearing payloads. Saved settings, output and receipts reveal no key.
