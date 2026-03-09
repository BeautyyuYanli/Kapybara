---
name: workflow
description: Standardized semantics for multi-node agentic workflows using detached nodes launched via self-hook and parent-memory edges (DAG).
---

# Workflow Semantics (DAG)

A workflow in Kapybara is a **Directed Acyclic Graph (DAG)** where each **node** is a detached `kapy` run launched according to `skills:core/self-hook/SKILLS.md`, and each **edge** is a causal dependency defined by parent memories.

## Core Concepts

### 1. Nodes Categories
A workflow consists of three types of nodes:
- **Start Node**: Triggered by an external event. Its primary responsibility is to **sequentially register subsequent nodes** (Mid and End) and then exit.
- **Mid Nodes**: Intermediate processing steps.
- **End Node**: The final step that typically replies to the original `out_channel`.

### 2. Channel Scoping & Routing
- **`in_channel`**: For all Mid and End nodes, set to `workflow/<start_memory_id>` for isolation.
- **`out_channel`**:
  - **Mid Nodes**: Default to `direct/default`.
  - **End Node**: Default to the **Start Node's `out_channel`**.
- **`contacts`**: If the Start Node had any contacts, every Mid and End node must repeat the same `--contact` flags in the same set.

### 3. Dependency Mapping (Edges)
- Connections are defined by parent memories.
- Dependencies are declared using `--parent-memory='<ID>'`.
- Register dependent nodes sequentially: capture one node's `memory_id` from the kapy command JSON output before launching the next node that depends on it.

### 4. Parallelism & ID Collision
- Memory IDs are derived from millisecond-level timestamps.
- When launching multiple independent nodes in parallel (e.g., from a Start Node), **wait at least 1ms** (e.g., `sleep 0.001`) between `kapy` calls to ensure each node receives a unique ID.

## Usage Patterns

#### 1. Register the first intermediate step
```bash
# Register Mid Node A
# If the Start Node had contacts, repeat --contact once per contact.
# The detached command prints JSON; capture <node_a_id> from its memory_id field.
env K_CONFIG_BASE='/home/k/.kapybara' \
  kapy \
  --in-channel='workflow/<start_id>' \
  --out-channel='direct/default' \
  --contact='<start_contact_1>' \
  --parent-memory='<start_id>' \
  "Mid Node A: Process raw data"
```

#### 2. Register the second intermediate step (depends on A)
```bash
# Register Mid Node B
# Repeat every start-node contact here too, if any.
env K_CONFIG_BASE='/home/k/.kapybara' \
  kapy \
  --in-channel='workflow/<start_id>' \
  --out-channel='direct/default' \
  --contact='<start_contact_1>' \
  --parent-memory='<start_id>' \
  --parent-memory='<node_a_id>' \
  "Mid Node B: Analyze processed data"
```

#### 3. Register the End Node (depends on B)
```bash
# Register End Node C
# Repeat every start-node contact here too, if any.
env K_CONFIG_BASE='/home/k/.kapybara' \
  kapy \
  --in-channel='workflow/<start_id>' \
  --out-channel='<original_user_out_channel>' \
  --contact='<start_contact_1>' \
  --parent-memory='<start_id>' \
  --parent-memory='<node_b_id>' \
  "End Node C: Summarize analysis and reply to user"
```
