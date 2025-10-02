# Trust5

Autonomous code generation with correctness guarantees.

Trust5 orchestrates LLM agents through a multi-phase pipeline
(plan, implement, validate, repair, quality) to produce working code
with automated testing and quality gates. It runs on top of the
Stabilize workflow engine with SQLite persistence, crash recovery,
and event sourcing.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [Commands](#commands)
5. [Pipeline Stages](#pipeline-stages)
6. [LLM Providers](#llm-providers)
7. [Language Support](#language-support)
8. [Configuration](#configuration)
9. [Quality Gates](#quality-gates)
10. [Validate / Repair Loop](#validate--repair-loop)
11. [Parallel Pipelines](#parallel-pipelines)
12. [TUI and Headless Mode](#tui-and-headless-mode)
13. [Crash Recovery](#crash-recovery)
14. [File Layout](#file-layout)
15. [Timeouts and Limits](#timeouts-and-limits)
16. [Troubleshooting](#troubleshooting)

---

## Requirements

- Python 3.10 or later
- One of: Anthropic API key (Claude), Google API key (Gemini), or a local Ollama instance

---

## Installation

```
pip install -e .
```

This installs the `trust5` CLI and all runtime dependencies (Typer, Stabilize,
Pydantic, Textual, Rich, PyYAML, cryptography).

For development:

```
pip install -e ".[dev]"
```

Adds pytest, ruff, and mypy.

---

## Quick Start

Initialize a project directory, authenticate with a provider, and run a
full pipeline:

```
mkdir myproject && cd myproject
trust5 init .
trust5 login claude
trust5 develop "Build a URL shortener with click tracking"
```

Trust5 will:

1. Plan the implementation (create a SPEC document)
2. Write tests for each module (TDD RED phase)
3. Implement the source code
4. Run syntax checks, linting, and tests
5. Repair any failures automatically (up to 5 attempts per cycle)
6. Validate against TRUST 5 quality gates

The result is working code with tests in your project directory.

---

## Commands

```
trust5 develop "description"     Full pipeline: plan + implement + validate + repair + quality
trust5 plan "description"        Plan phase only (creates a SPEC document)
trust5 run SPEC-ID               Implement from an existing SPEC
trust5 resume                    Resume the last failed pipeline from its failure point
trust5 loop                      Continuous LSP diagnostics fix loop
trust5 watch [path]              Stream live events from a running pipeline
trust5 init [path]               Initialize a new project (creates .moai/ and .trust5/)
trust5 login PROVIDER            Authenticate (claude, google, ollama)
trust5 logout [PROVIDER]         Remove stored credentials
trust5 auth-status               Show authentication state for all providers
```

### Global Flags

```
--provider, -p PROVIDER    Override the LLM provider for this run (claude, google, ollama)
--headless                 Disable the TUI; output to stdout only
```

### Examples

Plan only, then implement separately:

```
trust5 plan "REST API for task management with SQLite backend"
trust5 run SPEC-001
```

Use a local Ollama model:

```
trust5 --provider ollama develop "Monte Carlo simulation library"
```

Resume after a crash or failure:

```
trust5 resume
```

---

## Pipeline Stages

The `develop` command runs a two-phase pipeline:

### Phase 1: Plan

A planner agent reads the request and produces a SPEC document containing
requirements, acceptance criteria, module decomposition, setup commands,
test commands, and quality thresholds. The SPEC is saved under
`.moai/specs/`.

### Phase 2: Implement

The SPEC drives one or more implementation cycles. For each module:

```
Setup --> Write Tests --> Implement --> Validate --> Repair (if needed)
                                           |            |
                                           +<-----------+
                                           |
                                       Quality Gate --> Integration Repair (if needed)
                                           |                    |
                                           +<-------------------+
```

| Stage             | Task Type    | Purpose                                        |
|-------------------|--------------|-------------------------------------------------|
| Setup             | setup        | Run planner-specified shell commands (venv, deps)|
| Write Tests       | agent        | TDD RED phase: write test files only             |
| Implement         | implementer  | Write source code to pass the tests              |
| Validate          | validate     | Syntax check + lint check + run tests            |
| Repair            | repair       | LLM-driven code fix for failing tests/lint       |
| Quality           | quality      | TRUST 5 gate (coverage, security, readability)   |

---

## LLM Providers

Trust5 supports three LLM backends:

| Provider | Backend   | Auth Command          | Notes                          |
|----------|-----------|-----------------------|--------------------------------|
| claude   | Anthropic | `trust5 login claude` | Claude 3.5/4 models            |
| google   | Google AI | `trust5 login google` | Gemini models                  |
| ollama   | Local     | (no login needed)     | Any model served by Ollama     |

The default provider is `claude`. Override per-run with `--provider`:

```
trust5 -p ollama develop "calculator app"
trust5 -p google plan "REST API"
```

Ollama requires no authentication. Just ensure the Ollama server is running
locally.

---

## Language Support

Trust5 auto-detects the project language from manifest files and file
extensions. Each language has a full profile defining test runner, linter,
syntax checker, coverage tool, security scanner, and package manager.

### Supported Languages (23)

| Language    | Manifest Files                      | Test Runner        | Lint Tool         |
|-------------|-------------------------------------|--------------------|-------------------|
| Python      | pyproject.toml, requirements.txt    | pytest             | ruff              |
| Go          | go.mod                              | go test            | gofmt, go vet     |
| TypeScript  | tsconfig.json, package.json         | jest / vitest      | eslint            |
| JavaScript  | package.json                        | jest / vitest      | eslint            |
| Rust        | Cargo.toml                          | cargo test         | clippy, rustfmt   |
| Java        | pom.xml, build.gradle               | mvn test / gradle  | spotless          |
| Ruby        | Gemfile                             | rspec / minitest   | rubocop           |
| Elixir      | mix.exs                             | mix test           | credo             |
| C++         | CMakeLists.txt                      | ctest              | clang-format      |
| C           | CMakeLists.txt, Makefile            | ctest              | clang-format      |
| PHP         | composer.json                       | phpunit            | php-cs-fixer      |
| Kotlin      | build.gradle.kts                    | gradle test        | ktlint            |
| Swift       | Package.swift                       | swift test         | swift-format      |
| Dart        | pubspec.yaml                        | dart test          | dart analyze      |
| Scala       | build.sbt                           | sbt test           | scalafmt          |
| Haskell     | package.yaml, *.cabal               | cabal test         | ormolu            |
| Zig         | build.zig                           | zig test           | (none)            |
| R           | DESCRIPTION                         | testthat           | lintr             |
| C#          | *.csproj, *.sln                     | dotnet test        | dotnet format     |
| Lua         | *.rockspec                          | busted             | luacheck          |
| HTML        | index.html                          | (none)             | (none)            |
| Vue         | vue.config.js, nuxt.config.*        | vitest             | eslint            |
| Svelte      | svelte.config.*                     | vitest             | eslint            |

Detection order: manifest files first, then dominant file extension.

---

## Configuration

`trust5 init` creates the following config structure:

```
.moai/
  config/
    sections/
      quality.yaml        Quality gate settings
      language.yaml        Language and test framework
      git-strategy.yaml    Branch strategy
      workflow.yaml        Workflow settings
  specs/                   SPEC documents
  project/                 Project documentation
  memory/                  Checkpoint storage
  cache/                   Loop snapshots
.trust5/
  trust5.db               SQLite state database
  trust5.log              Runtime log (TUI mode)
  events.sock             Unix socket for live event streaming
```

### quality.yaml

```yaml
quality:
  development_mode: ddd          # ddd, tdd, or hybrid
  coverage_threshold: 85
  pass_score_threshold: 0.85
  max_errors: 0
  max_type_errors: 0
  max_lint_errors: 0
  max_security_warnings: 5
```

### language.yaml

```yaml
language:
  conversation_language: en
  code_comments: en
  language: auto                 # auto-detect from project files
  test_framework: auto
```

### git-strategy.yaml

```yaml
git_strategy:
  auto_branch: true
  branch_prefix: trust5/
```

---

## Quality Gates

Trust5 enforces the TRUST 5 framework, a weighted quality model with
five pillars:

| Pillar         | Weight | What It Measures                               |
|----------------|--------|------------------------------------------------|
| Tested         | 30%    | Test coverage, test count, all tests passing   |
| Readable       | 15%    | Code style, lint errors, formatting            |
| Understandable | 15%    | Complexity, documentation, type safety         |
| Secured        | 25%    | Security scanner findings (bandit, gosec, etc) |
| Trackable      | 15%    | Git history, conventional commits              |

Each pillar is scored 0.0 to 1.0. The quality gate passes when the
weighted score meets `pass_score_threshold` (default: 0.85).

Pillar-level thresholds:

- Score >= 0.85: PASS
- Score 0.50 - 0.84: WARNING
- Score < 0.50: CRITICAL

If the quality gate fails, the pipeline jumps to an integration repair
stage where the LLM fixes the reported issues, then re-runs the gate.
This cycle repeats up to 3 times.

---

## Validate / Repair Loop

The validate stage runs three checks in order:

1. **Syntax check** -- language-specific compiler/parser (e.g., `compileall` for Python, `go vet` for Go)
2. **Lint check** -- read-only linter from the language profile (e.g., `ruff check` for Python, `clippy` for Rust)
3. **Test execution** -- full test suite via the language profile's test command

If any check fails, the pipeline jumps to the repair stage where an LLM
agent reads the error output and fixes the source code. Repair then jumps
back to validate. This loop has three safety limits:

| Limit                   | Default | Purpose                                 |
|-------------------------|---------|-----------------------------------------|
| Max repair attempts     | 5       | Per validate/repair cycle               |
| Max reimplementations   | 3       | Full rewrites when repair is exhausted  |
| Max total jumps         | 50      | Absolute ceiling across all jump types  |

When repair attempts are exhausted, the pipeline reimplements the module
from scratch (up to 3 times). If all reimplementations fail, the pipeline
terminates.

---

## Parallel Pipelines

When the planner decomposes a request into multiple modules, Trust5
creates a parallel pipeline:

```
Setup
  |
  +---> [Module A: Write Tests -> Implement -> Validate -> Repair]
  |
  +---> [Module B: Write Tests -> Implement -> Validate -> Repair]
  |
  +---> [Module C: Write Tests -> Implement -> Validate -> Repair]
  |
  v
Integration Validate (all tests together)
  |
Integration Repair (cross-module fixes)
  |
Quality Gate (TRUST 5)
```

Each module has its own file ownership scope. The parallel pipeline
enforces strict file ownership: no two modules may claim the same source
file. Test files are scoped per module during validation to prevent
cross-module interference.

After all modules pass individually, an integration validate stage runs
all tests together. The quality gate runs last.

---

## TUI and Headless Mode

By default, Trust5 launches a terminal UI (Textual-based) that shows
live progress, stage status, and streaming output.

| Key | Action          |
|-----|-----------------|
| q   | Quit            |
| c   | Clear log       |
| s   | Toggle scroll   |

The TUI auto-disables when stdout is piped. Force headless mode with:

```
trust5 --headless develop "my request"
```

In headless mode, events are printed to stdout. You can also attach to a
running pipeline from another terminal:

```
trust5 watch .
```

This connects to the Unix Domain Socket at `.trust5/events.sock` and
streams events in real time.

---

## Crash Recovery

Trust5 uses Stabilize's event sourcing and SQLite persistence for crash
recovery. If the process is killed mid-pipeline:

1. Workflow state is preserved in `.trust5/trust5.db`
2. Run `trust5 resume` to restart from the failed stage
3. All stage context (repair attempts, test files, module ownership) is preserved
4. Downstream stages are reset to NOT_STARTED and re-triggered by DAG completion

The resume command finds the most recent TERMINAL or CANCELED workflow,
resets failed stages to RUNNING, and lets Stabilize's recovery mechanism
re-queue them.

---

## File Layout

```
trust5/
  main.py                  CLI entry point (Typer app)
  core/
    agent.py               Agent conversation loop (prompt -> LLM -> tools -> repeat)
    agent_task.py           Generic LLM-driven Stabilize task
    implementer_task.py     SPEC implementation task
    llm.py                 Multi-provider LLM client (Anthropic, Google, Ollama)
    tools.py               Tool definitions (Read, Write, Edit, Bash, Glob, Grep)
    lang.py                Language detection and profiles (23 languages)
    config.py              YAML config loading (Pydantic models)
    event_bus.py           Pub-sub event bus with Unix socket server
    quality.py             TRUST 5 quality assessment
    quality_gates.py       Gate pass/fail logic
    runner.py              Workflow execution and status finalization
    message.py             Event codes and emission helpers
    loop.py                LSP diagnostics loop task
    init.py                Project initializer (trust5 init)
    viewer.py              Headless stdout event viewer
    auth/                  Provider authentication (Claude, Google, Ollama)
  tasks/
    validate_task.py       Syntax + lint + test runner with repair routing
    repair_task.py         LLM-driven code repair
    setup_task.py          Shell command execution for environment setup
    quality_task.py        TRUST 5 quality gate task
  workflows/
    pipeline.py            Serial develop workflow
    parallel_pipeline.py   Multi-module parallel workflow
    plan.py                Plan-only workflow
    run.py                 Run-from-SPEC workflow
    loop_workflow.py       LSP diagnostics loop workflow
  assets/
    prompts/               Agent system prompts (implementer, repairer, test-writer, planner)
  tui/
    app.py                 Textual TUI application
    widgets.py             Custom TUI widgets
    styles.tcss             TUI stylesheet
```

---

## Timeouts and Limits

### Workflow Timeouts

| Workflow    | Timeout    | Command              |
|-------------|------------|----------------------|
| Plan        | 10 min     | `trust5 plan`        |
| Develop     | 2 hours    | `trust5 develop`     |
| Run         | 20 min     | `trust5 run`         |
| Loop        | 1 hour     | `trust5 loop`        |

### Agent Limits

| Parameter              | Value   |
|------------------------|---------|
| Max turns per agent    | 20      |
| Message history cap    | 60      |
| Tool result truncation | 8000 ch |
| Agent timeout          | 30 min  |
| Per-turn timeout       | 10 min  |
| Idle detection         | 10 turns of read-only calls |

### Subprocess Timeouts

| Operation       | Timeout |
|-----------------|---------|
| Bash commands   | 120s    |
| Syntax checks   | 120s    |
| Test execution  | 120s    |
| Grep operations | 60s     |

---

## Troubleshooting

### Pipeline stuck in validate/repair loop

Check the jump count in `.trust5/trust5.log`:

```
grep "jump #" .trust5/trust5.log
```

If jumps are incrementing normally, the pipeline is working. If the jump
count is not advancing, check `repair_attempt` in the log. The absolute
ceiling is 50 jumps (configurable via `_max_jumps` in stage context).

### Tests pass locally but validate fails

Validate runs tests in a subprocess with a clean environment. Common causes:

- Missing source root configuration (non-flat layout needs path env var setup)
- Test files not discovered (check that test file names match `test_*` or `*_test.*` patterns)
- Dependencies not installed (setup stage must install them)

### Quality gate keeps failing

Check which pillar is below threshold:

```
grep "quality" .trust5/trust5.log
```

The quality gate retries up to 3 times. After exhaustion, it accepts
partial results and the pipeline continues with a warning.

### TUI shows no output

Logs go to `.trust5/trust5.log` when TUI is active. Check that file
for errors. To debug, run in headless mode:

```
trust5 --headless develop "request"
```

### Resume says "no resumable pipeline found"

The workflow database is at `.trust5/trust5.db`. Resume looks for
workflows with status TERMINAL, CANCELED, FAILED_CONTINUE, or RUNNING.
If none exist, the previous pipeline either completed successfully or
was never started.

### Provider authentication fails

```
trust5 auth-status
trust5 login claude
```

Ollama requires no login. Ensure the Ollama server is running at
`http://localhost:11434`.

---

## License

See LICENSE file for details.
