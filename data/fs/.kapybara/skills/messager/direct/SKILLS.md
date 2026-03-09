---
name: messager/direct
description: Include the response text in the finish_action raw_output field for direct channel.
---

# messager/direct

This skill is for responding to events coming from the `direct` channel.
For direct execution, "sending" a message means including it in the final finish_action output.

## Usage

For `direct` channel responses, do not use `echo` or any external tool.
Instead, include the response text directly in the `raw_output` field when calling `finish_action`.

