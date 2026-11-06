---
name: setup-ai-feedback-loops
description: Set up AI coding feedback loops for any project — static analysis, tests, pre-commit hooks, and auto-formatting. Supports TypeScript, .NET, Python, Go, and more. Use when user wants to add feedback loops, set up a project for AI-assisted development, or ensure AI agents can self-verify their work.
---

# Setup AI Feedback Loops

Sets up 4 feedback loops so AI agents can self-verify: **static analysis**, **tests**, **pre-commit hooks**, and **auto-formatting**. Framework-agnostic — detects the project type and uses the right tools.

## Steps

### 1. Detect project type

Check root directory for these markers (in order):

| Marker | Ecosystem | Set `$ECO` |
|---|---|---|
| `package.json` + `tsconfig.json` | TypeScript (recommended) | `ts` |
| `package.json` (no tsconfig) | JavaScript/Node | `js` |
| `*.csproj` or `*.sln` | .NET | `dotnet` |
| `pyproject.toml` or `requirements.txt` | Python | `python` |
| `go.mod` | Go | `go` |
| `Cargo.toml` | Rust | `rust` |

If multiple match, ask the user which is primary. If none match, ask.

### 2. Audit existing setup

Before installing anything, check what already exists for the detected ecosystem. See [REFERENCE.md](REFERENCE.md) for ecosystem-specific markers.

**Only install/configure what's missing.** Report what was skipped and why.

### 3. Set up each feedback loop

For each of the 4 loops, apply the ecosystem-specific tooling from [REFERENCE.md](REFERENCE.md):

1. **Static analysis / type checking** — catches type errors, syntax issues
2. **Test runner** — catches logical errors
3. **Auto-formatting** — enforces consistent style
4. **Pre-commit hook** — wires 1-3 to run before every commit

### 4. Wire pre-commit hook

Create a git pre-commit hook (method depends on ecosystem) that runs, in order:

1. Auto-format staged files
2. Static analysis / type check
3. Run tests

Only include commands for tools that are actually set up.

### 5. Verify

Run each check individually. Report results. If any fail, diagnose and fix before committing.

### 6. Commit

Stage all changes and commit: `chore: add AI feedback loops (<tools installed>)`

## What NOT to do

- Don't create project config files (`tsconfig.json`, `.csproj`, etc.) — too project-specific
- Don't create test files — that's a separate concern
- Don't override existing test frameworks, linters, or formatters
- Don't install tools the user hasn't agreed to
