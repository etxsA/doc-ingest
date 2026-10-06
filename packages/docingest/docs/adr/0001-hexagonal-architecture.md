# ADR 0001: Hexagonal architecture with a plugin registry

**Status:** accepted, 2026-09-24

## Context
The ingestion layer has to outlive every technology choice it makes today:
- the OCR model (a new Qwen release every few months);
- the OCR runtime (MLX locally, vLLM on a GPU server);
- the LaTeX converter, the store, the QA engine and the document sources (arXiv today, others tomorrow).

The first prototype hard-wired all of these inside one `Pipeline` class, which made swaps risky and tests slow.

## Decision
- **Ports** are `typing.Protocol` classes (structural, `@runtime_checkable`). Adapters satisfy them by shape and never import them.
- **Domain** holds the canonical representation (`DocumentManifest`) and the pure policies: the OCR routing decision and text cleanup.
- **Application** services depend only on ports.
- **One composition root** (`bootstrap.py`) maps the names in `[adapters]` to factories. It also discovers third-party factories through the entry-point group `docingest.<port>`.
- Every adapter exposes a **fingerprint** that feeds the cache key, so replacing an adapter can never serve stale outputs.
- The layering is **enforced by import-linter contracts** that run in CI. It is not a convention that can quietly erode.

## Consequences
- Swapping the OCR model or runtime, or the converter, the store, the QA engine or the crawler, means editing one config line or installing a plugin.
- The application is unit-tested against in-memory fakes in milliseconds. The same contract tests run against fakes and against real adapters.
- Images cross the ports as `PIL.Image`. This is a pragmatic exception, because it is the de-facto standard in-memory image type.
- There is slightly more indirection: small Protocol modules and factory functions.
