# Compiler Toolchain Locking Design

## Overview

Add first-class compiler toolchain constraints to ENM projects. Projects can declare which compiler family and version range they require; ENM will enforce the constraint during `doctor`, `configure`, and `build`.

## Goals

1. Split the single `c++17` doctor check into three compiler-family sub-checks: `compiler-msvc`, `compiler-gcc`, `compiler-clang`.
2. Introduce manifest schema 2 with an optional `toolchain` section:
   ```json
   {
     "schema": 2,
     "toolchain": {
       "compiler": "msvc",
       "version": ">=19.44,<20"
     }
   }
   ```
   Schema 1 projects remain fully supported.
3. Add `enm lock-compiler` to capture the current compiler into the manifest. When multiple compilers are present, offer an interactive picker.
4. `enm doctor --project` checks the manifest toolchain constraint and reports mismatches.
5. `enm doctor fix --project` suggests concrete remediation:
   - gcc/clang: package-manager install command
   - msvc: manual download pointer (cannot be auto-installed)
6. `enm configure` resolves the constrained compiler and writes `CMAKE_CXX_COMPILER` into `enm-config.cmake` once, mirroring how `ENM_EUI_VERSION` is injected. If no matching compiler is found, print a warning and tell the user to run `enm configure --force` to ignore the constraint.
7. `enm configure` also warns when the active EUI-NEO SDK does not match the project pin, with the same `--force` escape hatch.
8. Update version to 0.3.0 and README with minimal additions.

## Non-goals

- Downloading or installing MSVC automatically (still manual).
- Rewriting user `CMakeLists.txt` to reference the compiler; injection happens through `enm-config.cmake` only.
- Supporting compilers other than msvc/gcc/clang.

## Manifest Schema

New projects created by `enm init` will use schema 2 and include an empty `toolchain` object:

```json
{
  "schema": 2,
  "name": "My App",
  "version": "0.1.0",
  "target": "My_App",
  "eui": {"version": "v0.5.6"},
  "build_dir": "build/default",
  "toolchain": {}
}
```

`toolchain` is optional; when absent or empty, no compiler constraint is enforced. Old schema 1 manifests continue to load without modification.

### Version Constraint Grammar

The `version` field is a comma-separated list of comparisons. Each comparison is one of:

- `>=MAJOR.MINOR`
- `>MAJOR.MINOR`
- `<=MAJOR.MINOR`
- `<MAJOR.MINOR`
- `=MAJOR.MINOR` (same as no operator on a single value)

All comparisons are ANDed together. Examples: `>=19.44,<20`, `>=12`, `=14.0`.

## New Command: `enm lock-compiler`

Behavior:

1. Load the project manifest from the current directory (or `--project`).
2. Detect available compilers using the existing detection logic.
3. If exactly one compiler is found, use it.
4. If multiple are found, print a numbered list and let the user pick with arrow keys/Enter.
5. Write `"toolchain": {"compiler": "<family>", "version": "=<major>.<minor>"}` into the manifest. The version is pinned to the exact major.minor of the selected compiler.

No `--force` flag is required for this command.

## Doctor Changes

### Compiler Sub-checks

Replace the current `c++17` single check with three family-specific checks plus the existing capability probe:

- `compiler-msvc` — msvc present and version sufficient for C++17
- `compiler-gcc` — gcc/g++ present and version >= 12.0
- `compiler-clang` — clang++ present and version >= 14.0

The existing `_cpp17_probe` stays as a general `c++17` capability check but is no longer the primary compiler indicator.

### `--project` Constraint Check

When `project_root` is provided:

1. Load manifest.
2. If `toolchain` is empty/absent, skip.
3. Detect the compiler CMake would actually use (env `CXX`/`CC`, then cl, g++, clang++).
4. Compare family and version range.
5. Emit a `toolchain` check:
   - `ok` if matching
   - `unsupported` if wrong family or version out of range
   - `missing` if no compiler detected

### `doctor fix --project`

For `toolchain` mismatches:

- gcc/clang wrong/missing: suggest `sudo apt install g++` / `brew install gcc` / `winget install GnuWin32.GCC` etc. based on package manager.
- msvc: print a message pointing to Visual Studio Build Tools download page.
- Also mention `enm lock-compiler` or `enm configure --force` as alternatives.

## Configure / Build Changes

### Configure

`enm configure` already writes `enm-config.cmake` with `ENM_TARGET`, `ENM_PROJECT_VERSION`, and `ENM_EUI_VERSION`. It will now also write:

```cmake
set(CMAKE_CXX_COMPILER "/path/to/compiler" CACHE FILEPATH "" FORCE)
```

when a toolchain constraint is present and a matching compiler is found.

If no matching compiler is found:

- Print a warning like: `warning: project requires msvc >=19.44,<20 but no matching compiler was found. Run 'enm configure --force' to ignore the toolchain constraint.`
- Exit non-zero unless `--force` is passed.

If the active EUI-NEO SDK does not match the manifest pin:

- Print a warning like: `warning: active SDK is v0.5.5 but project requires v0.5.6. Run 'enm configure --force' to ignore.`
- Exit non-zero unless `--force` is passed.

### Build

`enm build` relies on the compiler already set in the CMake cache by `configure`. It does not re-validate the toolchain unless the user passes `--project` explicitly or we choose to re-check on every build. For simplicity, validation happens at configure time.

### `--force`

`enm configure --force` and `enm build --force` ignore both toolchain and EUI SDK mismatch warnings.

## Additional Changes

- `load_manifest` accepts schema 2.
- `create_project` emits schema 2 with an empty `toolchain` object.
- Add unit tests for version range parsing, constraint matching, and `lock-compiler`.
- Update README command reference with `lock-compiler`, `--force` on `configure`/`build`, and schema 2 note.
- Update version to 0.3.0 in `pyproject.toml`, `src/enm/__init__.py`, and `README.md`.
