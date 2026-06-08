# Context Management

## Design principle

Each role should see only what it needs.

Do not send the whole repo to every model.

## Context packs by role

### Router

Sees:

- user prompt,
- git status,
- file count by extension,
- top-level repo structure,
- short session summary.

### Planner

Sees:

- repo map,
- architecture docs,
- project conventions,
- relevant files,
- prior decisions,
- user request.

### Builder

Sees:

- plan,
- acceptance criteria,
- selected files,
- relevant tests,
- coding standards.

### Reviewer

Sees:

- diff,
- acceptance criteria,
- test logs,
- relevant surrounding code,
- coding standards.

### Security reviewer

Sees:

- diff,
- security policy,
- auth/config/dependency context,
- network and shell changes,
- secrets risk indicators.

## Context manifest

Every run should write a manifest.

```json
{
  "session_id": "abc123",
  "route": "normal_code_change",
  "repo_root": "/repo",
  "selected_files": ["src/app.py", "tests/test_app.py"],
  "excluded_paths": ["node_modules/**", "dist/**"],
  "role_token_estimates": {
    "router": 1200,
    "builder": 18000,
    "reviewer": 12000
  },
  "reasoning": "Selected files based on imports, filename match, and test linkage."
}
```

## Repo map v1

The v1 repo map should include:

- top-level directories,
- source files by extension,
- package files,
- test files,
- config files,
- docs files,
- recent git changes.

## Token estimation

v1 can use a simple approximation:

```text
tokens ≈ characters / 4
```

Replace later with tokenizer-specific estimates.

## Context compression

Rules:

- Long command output is summarized and stored as artifact.
- Only the summary goes back into model context unless full logs are needed.
- Large files are summarized unless selected for edit.
- Reviewer gets diff-first context.
- Security reviewer gets only risk-relevant files beyond the diff.

## Context drift prevention

Manta should store:

- what each agent saw,
- what files were selected,
- why they were selected,
- token estimates,
- any context omitted due to budget.

This makes failures debuggable.
