# Ecosystem Reference

Tooling for each feedback loop, per ecosystem.

---

## TypeScript (recommended)

The strongest AI feedback loop. TypeScript's compiler catches errors an AI would only find by running in a browser.

### Static analysis
- **Tool**: `tsc --noEmit`
- **Script**: `"typecheck": "tsc --noEmit"` in package.json
- **Prerequisite**: `tsconfig.json` must exist — don't create it, tell user

### Tests
- **Tool**: Vitest (preferred) or existing framework (Jest, Mocha)
- **Install**: `$PM install --save-dev vitest`
- **Script**: `"test": "vitest run"` — use `run` so it exits, not watch mode
- **Skip if**: jest, mocha, or vitest already in devDependencies

### Formatting
- **Tool**: Prettier via lint-staged
- **Install**: `$PM install --save-dev lint-staged prettier`
- **Config `.lintstagedrc`**:
  ```json
  { "*": "prettier --ignore-unknown --write" }
  ```
- **Config `.prettierrc`** (only if no prettier config exists):
  ```json
  {
    "useTabs": false,
    "tabWidth": 2,
    "printWidth": 80,
    "singleQuote": false,
    "trailingComma": "es5",
    "semi": true,
    "arrowParens": "always"
  }
  ```

### Pre-commit hook
- **Tool**: Husky v9+
- **Install**: `$PM install --save-dev husky && $PMX husky init`
- **`.husky/pre-commit`**:
  ```
  npx lint-staged
  $PM run typecheck
  $PM run test
  ```

### Package manager detection
Check for: `pnpm-lock.yaml` (pnpm), `package-lock.json` (npm), `yarn.lock` (yarn), `bun.lockb` (bun). Default to npm.

---

## JavaScript / Node (no TypeScript)

Same as TypeScript but skip type checking. Consider suggesting TypeScript adoption.

### Static analysis
- **Tool**: ESLint
- **Install**: `$PM install --save-dev eslint`
- **Script**: `"lint": "eslint ."`

### Tests, Formatting, Pre-commit hook
Same as TypeScript section above. Replace `typecheck` line with `lint` in pre-commit hook.

---

## .NET

### Static analysis
- **Command**: `dotnet build --no-incremental`
- C# is statically typed — `dotnet build` is your type checker
- If solution file exists: `dotnet build MySolution.sln`

### Tests
- **Command**: `dotnet test`
- Check for existing test projects (`*.Tests.csproj`, `*Tests/*.csproj`)
- If no test project exists, tell user to create one (don't guess test framework — xUnit, NUnit, MSTest are all common)

### Formatting
- **Tool**: `dotnet format`
- Built into .NET SDK 6+, no install needed
- Or CSharpier: `dotnet tool install csharpier -g` then `dotnet csharpier .`

### Pre-commit hook
- **Option A**: Shell-based `.git/hooks/pre-commit` script:
  ```bash
  #!/bin/sh
  dotnet format --verify-no-changes
  dotnet build --no-incremental
  dotnet test --no-build
  ```
- **Option B**: Husky.Net — `dotnet tool install husky`
  - Create `.husky/pre-commit`:
    ```bash
    dotnet format --verify-no-changes
    dotnet build --no-incremental
    dotnet test --no-build
    ```
- Prefer Option A (no extra dependency). Use Option B if user prefers.

### Notes
- `dotnet test --no-build` avoids double-building (build already ran)
- `dotnet format --verify-no-changes` fails if formatting is needed (CI-friendly)

---

## Python

### Static analysis
- **Tool**: mypy (preferred) or pyright
- **Install**: `pip install mypy` or add to `pyproject.toml` dev dependencies
- **Command**: `mypy .` or `mypy src/`
- Skip if project has no type hints and user doesn't want to add them — suggest ruff for linting instead

### Tests
- **Tool**: pytest (standard)
- **Install**: `pip install pytest`
- **Command**: `pytest`
- Skip if pytest/unittest already configured

### Formatting
- **Tool**: Ruff (preferred — fast, replaces black + isort + flake8)
- **Install**: `pip install ruff`
- **Command**: `ruff format . && ruff check --fix .`

### Pre-commit hook
- **Tool**: [pre-commit](https://pre-commit.com/) framework (Python ecosystem standard)
- **Install**: `pip install pre-commit`
- **Config `.pre-commit-config.yaml`**:
  ```yaml
  repos:
    - repo: https://github.com/astral-sh/ruff-pre-commit
      rev: v0.8.0
      hooks:
        - id: ruff-format
        - id: ruff
          args: [--fix]
    - repo: local
      hooks:
        - id: mypy
          name: mypy
          entry: mypy .
          language: system
          types: [python]
          pass_filenames: false
        - id: pytest
          name: pytest
          entry: pytest
          language: system
          types: [python]
          pass_filenames: false
  ```
- **Install hooks**: `pre-commit install`

### Notes
- Use virtual environment — check for `venv/`, `.venv/`, or `poetry.lock`
- If using Poetry: replace `pip install` with `poetry add --group dev`
- If using uv: replace `pip install` with `uv add --dev`

---

## Go

### Static analysis
- **Command**: `go vet ./...`
- Optional: `staticcheck` (`go install honnef.co/go/tools/cmd/staticcheck@latest`)

### Tests
- **Command**: `go test ./...`
- Built-in, no install needed

### Formatting
- **Command**: `gofmt -w .` or `goimports -w .`
- Built-in, no install needed

### Pre-commit hook
- Shell-based `.git/hooks/pre-commit`:
  ```bash
  #!/bin/sh
  gofmt -w .
  go vet ./...
  go test ./...
  ```

---

## Rust

### Static analysis
- **Command**: `cargo clippy -- -D warnings`
- Clippy comes with rustup

### Tests
- **Command**: `cargo test`
- Built-in, no install needed

### Formatting
- **Command**: `cargo fmt --check`
- Rustfmt comes with rustup

### Pre-commit hook
- Shell-based `.git/hooks/pre-commit`:
  ```bash
  #!/bin/sh
  cargo fmt --check
  cargo clippy -- -D warnings
  cargo test
  ```
