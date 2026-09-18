# llm-driven-semantica

A **minimal implementation** based on the knowledge-graph philosophy of [semantica], dropping its heavy dependencies and complex internals in favor of a **Rule-by-Contract** architecture:

> Semantic extraction (entity-relation triples, attributes, business rules, business processes, the predictive-decision six-pack, temporal / actions / constraints / permissions) is performed by the **host agent (LLM)** — the host only needs to deliver a JSON contract in the agreed format (the eleven-part contract);
> Python keeps only a **deterministic thin shell** — content-addressed IDs, Schema validation, enum fallbacks, lazy anchors, dual-temporality knowledge graph.
> **Zero-rule extraction**: no `SMINI_LLM_*` environment variables, no model HTTP calls, no deterministic Python extraction rules.

Extraction quality depends heavily on the capability of the model being called; we recommend running this skill with a SOTA model.

---

## Features

- **Extract-only three-step pipeline**: `ingest (read-source primitive) → extract (host-LLM contract extraction ★) → build_kg (lazy-anchor consumer layer)`
- **Host-as-LLM (Mode 3)**: the host agent delivers the JSON contract (`--host-contract` / `HostAgentLLMProvider.inject()`) — no model credentials needed
- **Deterministic runtime `smini`**: pure Python standard library, **zero third-party dependencies**, only does what the skill cannot (IDs / validation / graph building)
- **Idempotent**: the LLM only "proposes" facts; every ID is content-addressed by Python via `sha256` → reruns are strictly consistent
- **Eleven-part contract**: `entities / relations / attributes / rules / processes / states / functions / temporal / actions / constraints / permissions`
- **126 tests green** (contract + end-to-end + runtime), with a self-contained HTML viewer rendered alongside (`scripts/render_viewer.py`, 12 tabs)

## Architecture

```
User document / text
   │  read by host agent (ingest/normalize outsourced to the host; no deterministic Python parsing)
   ▼  smini-extract  ★ host delivers the contract (eleven-part JSON)
   │  Python thin shell: ID computation (sha256 content addressing) + Schema validation + enum fallbacks
   ▼  build_kg (lazy-anchor consumer layer)
   │  object_literal derivation · attributes dual-placed · six-pack counts registered · entity-edge graph
   ▼
KnowledgeGraph + HTML viewer (scripts/render_viewer.py)
```

## Quick Start

### Using the skill in a host agent (primary path)

Any host agent with LLM capability (Claude, WorkBuddy, etc.) can deliver the contract described in `skills/smini-extract/SKILL.md` directly, e.g.:
“Call the smini-extract skill to extract /path/to/doc”
**No model API credentials required**:
host reads the document → produces the eleven-part JSON `{entities, relations, attributes, rules, processes, ...}` → Python thin shell does the deterministic mapping → builds the graph + renders the HTML.

```bash
# 0. Environment: Python ≥ 3.10 (the code uses `X | None` annotations and `types.UnionType`,
#    which are unsupported on 3.9); Python 3.12 recommended — `uv run --python 3.12 -m smini.cli …`
#    (uv resolves the interpreter automatically)

# 1. Install (editable mode)
pip install -e .

# 2. Run the tests (126)
python -m unittest discover -s tests

# 3. Run the built-in sample (host does not deliver → extraction is empty, but the pipeline
#    runs end to end and reports it)
python -m smini.cli build --sample

# 4. Host agent acts as the LLM: extraction after delivering the contract
python -m smini.cli build --sample --host-contract contract.json --extract-out runs/<run_id>/04-extraction.json
```

## Project Structure

```
smini/                 # Deterministic thin shell (Python standard library, zero dependencies)
├── sources.py         #   read-source primitives (text:// / file:// / inline; passthrough host raw_docs)
├── steps/extract.py   #   ★ host contract extraction (thin-shell mapping + lazy anchors)
├── steps/build_kg.py  #   entity-edge graph + attributes dual-placed + six-pack counts
├── llm.py             #   HostAgentLLMProvider (host-as-LLM, zero credentials)
├── ids.py             #   sha256 content-addressed IDs
├── runtime.py         #   atomic commands: ids / validate / graph build
├── cli.py             #   python -m smini.cli
└── types.py           #   data contract (dataclasses)
contracts/             # single JSON Schema: extraction.schema.json
skills/smini-extract/  # host-contract skill (SKILL.md + references + scripts/render_viewer.py)
tests/                 # 126 tests
docs/                  # design documents + source analysis
```

## License

Apache-2.0 (`LICENSE` / `NOTICE`). Ontology extraction rules are partially derived from the MIT-licensed [semantica](https://github.com/sharptoolbox/semantica).
