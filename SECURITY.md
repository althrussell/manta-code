# Security Policy

## Supported versions

Manta Code is pre-1.0 and under active development. Security fixes are applied
to the `main` branch only.

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public
issue for a suspected vulnerability.

- Use GitHub's [private vulnerability reporting](https://github.com/althrussell/manta-code/security/advisories/new)
  ("Report a vulnerability" under the repository's Security tab), or
- Open a regular issue **only** for non-sensitive, low-severity hardening
  suggestions.

Please include:

- a description of the issue and its impact,
- steps to reproduce (proof-of-concept where possible),
- affected version / commit.

We aim to acknowledge reports within 5 business days.

## Scope

Manta is a thin launcher that wires the [`deepagents-code`](https://pypi.org/project/deepagents-code/)
TUI to Databricks Model Serving using your local Databricks profile. It does
not store credentials itself — authentication is delegated to the Databricks
SDK's unified auth. Vulnerabilities in upstream `deepagents-code`, `deepagents`,
or the Databricks SDK should be reported to those projects directly.
