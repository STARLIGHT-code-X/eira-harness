# Eira 0.2

This release addresses the internal 0.1 audit and adds model profiles, daily financial data, and a user-local installer.

| Audit area | Change |
|---|---|
| Approval visibility | JSON-escaped review text preserves invisible content |
| Credential paths | Standard and configured user credential locations are protected |
| Terminal processing | Single-pass sanitizer replaces backtracking expression |
| Redacted file edits | Protected reads are uneditable; placeholder writes are rejected |
| Shell output | Bounded pipe capture; Docker's disk logging is disabled |
| Detached host processes | Host execution is removed; shell is Docker-only |
| Malformed inputs | JSON-depth, provider-schema, and numeric checks fail safely |
| Network duration | Whole-request deadlines cover DNS, headers, and body |
| Rounded equity | Calculations retain unrounded equity |
| SMA precision | Bounded-precision Decimal rolling sums replace float prefixes |
| CSV ambiguity | Duplicate headers and inconsistent row shapes are rejected |
| Directory scans | Entry, depth, and elapsed-time budgets apply |

Model profiles: OpenAI, Anthropic Messages, OpenRouter, Gemini through its OpenAI-compatible API, Ollama, and custom OpenAI-compatible endpoints. Exact model availability and tool support depend on the provider. No paid model quality benchmark is claimed.

Data adapters: Alpha Vantage compact daily stock prices and Coinbase Exchange daily crypto candles. Both return validated CSV and provenance metadata, omit the current UTC day, and use fixed endpoints. No order execution is included.

The installer uses a pinned source commit, a private Python virtual environment, and a managed launcher. It does not need sudo or change shell startup files. Failed installation does not replace the current working release. Existing unmanaged commands are not overwritten.

The implementation is safer than 0.1; it is not an independent security certification. See [remaining boundaries](SECURITY.md).
