# Context plugins

A ContextPlugin processes closed raw pages in on_page and supplies prior information
in get_context. The host retains paging thresholds, safe anchors, reference-window
selection, checkpoint protection and final system/upper/current-page assembly.
Input values contain payloads and raw messages, not session or anchor identities.
External artifacts are referenced by the plugin's persisted JSON payload.

ContextPluginRegistry maps stored names to ordinary config-to-plugin constructors;
closures can bind application dependencies. Each lease creates a new instance.
The default kapy/summary accepts an empty config and uses the existing summary/v1
page payload. It asks the borrowed call_agent for ordinary summary text, with client
tools blocked by SDK retries, and creates ContextPage itself. Its get_context returns
summary explanation, reference original rounds and the resume prompt.

No plugin lifecycle or staged preparation framework is added. Plugins needing model
calls use call_agent only while on_page is active. The helper borrows the current
Agent and resources; optional Pydantic result_type and client tool blocking are
independent. See [runner contracts](../agent_runner/README.md) for persistence,
retry, cancellation and cache-prefix boundaries.
