# Config Schema

## Config locations

Project config:

```text
.manta/config.toml
```

User config:

```text
~/.manta/config.toml
```

Precedence:

```text
CLI flags > project config > user config > defaults
```

## Example

```toml
[models]
router = "openai:gpt-5-nano"
cheap_responder = "openai:gpt-5-mini"
planner = "anthropic:claude-opus"
builder = "openai:gpt-builder"
reviewer = "google:gemini-pro"
security_reviewer = "google:gemini-pro"
release = "openai:gpt-5-mini"

[budgets]
default_task_usd = 1.00
hard_max_task_usd = 5.00
max_iterations = 3
max_turns = 30
show_cost_always = true

[autonomy]
mode = "smart_approve"
allow_file_writes = true
allow_shell = "allowlisted"
allow_network = false
allow_git_commit = "approval"
allow_git_push = false

[context]
strategy = "brokered"
auto_compact = true
store_full_history = true
repo_index = true
max_router_tokens = 4000
max_builder_tokens = 64000
max_reviewer_tokens = 128000

[review]
code_review_required = true
security_review_on_risk = true
block_on_high_severity = true
```

## Price table

Path:

```text
~/.manta/price_table.toml
```

Schema:

```toml
[models."openai:gpt-builder"]
input_per_million = 1.25
output_per_million = 10.00
context_window = 200000

[models."anthropic:claude-opus"]
input_per_million = 15.00
output_per_million = 75.00
context_window = 200000
```

Prices change often. The price table must be user-editable and should be refreshed intentionally.

## Policy config

```toml
[paths]
project_root_only = true
protected_globs = [".env", ".env.*", "**/*secret*", "**/*credential*"]

[shell]
mode = "allowlisted"
allow = ["pytest", "python -m pytest", "npm test", "npm run lint"]

[network]
allow = false
allowed_domains = []

[git]
commit = "approval"
push = "deny"
```
