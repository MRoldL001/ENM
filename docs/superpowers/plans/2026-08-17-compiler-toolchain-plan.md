# Compiler Toolchain Locking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add compiler toolchain constraints to ENM projects, including schema 2 manifest support, `enm lock-compiler`, doctor constraint checks, and configure-time compiler injection.

**Architecture:** A new `src/enm/toolchain.py` module centralizes compiler family detection, version extraction, and range constraint parsing. `project.py` uses it to resolve constraints and inject `CMAKE_CXX_COMPILER` into `enm-config.cmake`. `doctor.py` uses it for family-specific checks and `doctor --project` constraint validation. `cli.py` adds `lock-compiler` and `--force` flags.

**Tech Stack:** Python 3.9+, argparse, subprocess, pathlib, CMake

---

### Task 1: Create toolchain module with version parsing and compiler resolution

**Files:**
- Create: `src/enm/toolchain.py`
- Modify: `src/enm/doctor.py` (later, after module exists)
- Test: `tests/test_toolchain.py`

- [ ] **Step 1: Write failing tests for version range parsing**

```python
from enm.toolchain import VersionConstraint

class VersionConstraintTests(unittest.TestCase):
    def test_single_minimum(self):
        c = VersionConstraint.parse(">=19.44")
        self.assertTrue(c.match((19, 44)))
        self.assertTrue(c.match((19, 45)))
        self.assertFalse(c.match((19, 43)))

    def test_range(self):
        c = VersionConstraint.parse(">=19.44,<20")
        self.assertTrue(c.match((19, 44)))
        self.assertTrue(c.match((19, 50)))
        self.assertFalse(c.match((20, 0)))
        self.assertFalse(c.match((19, 43)))

    def test_exact(self):
        c = VersionConstraint.parse("=14.0")
        self.assertTrue(c.match((14, 0)))
        self.assertFalse(c.match((14, 1)))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toolchain.py -v`
Expected: `ModuleNotFoundError: No module named 'enm.toolchain'`

- [ ] **Step 3: Implement VersionConstraint class**

```python
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class _Comparison:
    op: str
    version: tuple[int, ...]


class VersionConstraint:
    def __init__(self, comparisons: list[_Comparison]) -> None:
        self.comparisons = comparisons

    @classmethod
    def parse(cls, text: str) -> "VersionConstraint":
        comparisons: list[_Comparison] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            match = re.match(r"^(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)*)$", part)
            if not match:
                raise ValueError(f"invalid version constraint: {part!r}")
            op = match.group(1) or "="
            version = tuple(int(p) for p in match.group(2).split("."))
            comparisons.append(_Comparison(op, version))
        return cls(comparisons)

    def match(self, version: tuple[int, ...]) -> bool:
        for comp in self.comparisons:
            if comp.op == ">=" and not version >= comp.version:
                return False
            if comp.op == "<=" and not version <= comp.version:
                return False
            if comp.op == ">" and not version > comp.version:
                return False
            if comp.op == "<" and not version < comp.version:
                return False
            if comp.op == "=" and version != comp.version:
                return False
        return True

    def __str__(self) -> str:
        parts = []
        for comp in self.comparisons:
            version = ".".join(str(p) for p in comp.version)
            parts.append(f"{comp.op}{version}")
        return ",".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toolchain.py -v`
Expected: PASS

- [ ] **Step 5: Add compiler family detection and version extraction tests**

```python
from pathlib import Path
from unittest import mock
from enm.toolchain import detect_compilers, resolve_compiler

class CompilerDetectionTests(unittest.TestCase):
    def test_detect_compilers_returns_priority_order(self):
        with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in ("cl", "g++", "clang++") else None):
            compilers = detect_compilers()
        self.assertEqual([c.family for c in compilers], ["msvc", "gcc", "clang"])

    def test_resolve_compiler_prefers_cxx_env(self):
        with mock.patch.dict("os.environ", {"CXX": "/usr/bin/clang++"}):
            with mock.patch("shutil.which", return_value="/usr/bin/clang++"):
                compiler = resolve_compiler()
        self.assertEqual(compiler.family, "clang")
```

- [ ] **Step 6: Implement detect_compilers and resolve_compiler**

```python
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Compiler:
    path: Path
    family: str
    version: tuple[int, ...]
    detail: str


def _compiler_family(name: str) -> str:
    lower = name.lower()
    if "cl" in lower and "clang" not in lower:
        return "msvc"
    if "clang" in lower:
        return "clang"
    if "g++" in lower or "gcc" in lower:
        return "gcc"
    return "unknown"


def _compiler_version(path: Path, family: str) -> tuple[tuple[int, ...] | None, str]:
    args = ["--version"] if family != "msvc" else []
    try:
        result = subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    if family == "msvc":
        match = re.search(r"Version\s+(\d+)\.(\d+)", output)
    elif family == "gcc":
        match = re.search(r"\bg\+\+\s+.*?(\d+)\.(\d+)", output)
    elif family == "clang":
        match = re.search(r"clang\s+version\s+(\d+)\.(\d+)", output)
    else:
        match = None
    if match:
        return tuple(int(p) for p in match.groups()), output.splitlines()[0]
    return None, output.splitlines()[0] if output else f"exit code {result.returncode}"


def detect_compilers() -> list[Compiler]:
    candidates: list[tuple[str, str]] = []
    if os.name == "nt":
        candidates.append(("cl", "msvc"))
    candidates.extend([("g++", "gcc"), ("clang++", "clang")])

    compilers: list[Compiler] = []
    for name, expected_family in candidates:
        path = shutil.which(name)
        if not path:
            continue
        family = _compiler_family(name)
        if family != expected_family:
            continue
        version, detail = _compiler_version(Path(path), family)
        compilers.append(Compiler(Path(path), family, version or (0,), detail))
    return compilers


def resolve_compiler() -> Compiler | None:
    for env_var in ("CXX", "CC"):
        value = os.environ.get(env_var)
        if value:
            path = shutil.which(value)
            if path:
                family = _compiler_family(Path(path).name)
                version, detail = _compiler_version(Path(path), family)
                return Compiler(Path(path), family, version or (0,), detail)
    compilers = detect_compilers()
    return compilers[0] if compilers else None
```

- [ ] **Step 7: Run tests to verify compiler detection passes**

Run: `python -m pytest tests/test_toolchain.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/enm/toolchain.py tests/test_toolchain.py
git commit -m "feat: add toolchain module for compiler detection and version constraints"
```

---

### Task 2: Update manifest schema and create_project

**Files:**
- Modify: `src/enm/project.py:36-52` (create_project manifest), `src/enm/project.py:103-110` (load_manifest schema validation)
- Test: `tests/test_project.py`

- [ ] **Step 1: Write failing test for schema 2 manifest creation**

```python
def test_create_project_uses_schema_two(self):
    import tempfile
    from pathlib import Path
    from enm.project import create_project, load_manifest
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        create_project(root, "My App", "v0.5.6")
        manifest = load_manifest(root)
    self.assertEqual(manifest["schema"], 2)
    self.assertIn("toolchain", manifest)
    self.assertEqual(manifest["toolchain"], {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_project.py::ProjectTests::test_create_project_uses_schema_two -v`
Expected: FAIL (assertion error, schema is 1)

- [ ] **Step 3: Update create_project to emit schema 2 with empty toolchain**

In `src/enm/project.py`:

```python
manifest = {
    "schema": 2,
    "name": name,
    "version": "0.1.0",
    "target": target,
    "eui": {"version": version},
    "build_dir": "build/default",
    "toolchain": {},
}
```

- [ ] **Step 4: Update load_manifest to accept schema 2**

In `src/enm/project.py`:

```python
if manifest.get("schema") not in (1, 2) or not manifest.get("target") or not manifest.get("version"):
    raise ReleaseError(f"unsupported or incomplete {MANIFEST}")
```

- [ ] **Step 5: Run tests to verify schema 2 creation passes**

Run: `python -m pytest tests/test_project.py::ProjectTests::test_create_project_uses_schema_two -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/enm/project.py tests/test_project.py
git commit -m "feat: bump manifest schema to 2 with empty toolchain object"
```

---

### Task 3: Add compiler resolution and enm-config.cmake injection

**Files:**
- Modify: `src/enm/project.py:113-135` (_write_cmake_initial_cache)
- Modify: `src/enm/project.py:184-260` (configure)
- Test: `tests/test_project.py`

- [ ] **Step 1: Add resolve_toolchain_compiler helper in project.py**

```python
from .toolchain import Compiler, VersionConstraint, detect_compilers, resolve_compiler


def _resolve_toolchain_compiler(manifest: dict[str, Any]) -> Compiler | None:
    toolchain = manifest.get("toolchain") or {}
    family = toolchain.get("compiler")
    if not family:
        return None
    constraint_text = toolchain.get("version", "")
    constraint = VersionConstraint.parse(constraint_text) if constraint_text else None

    compilers = detect_compilers()
    if family == resolve_compiler().family if resolve_compiler() else "":
        current = resolve_compiler()
        if current and (constraint is None or constraint.match(current.version)):
            return current
    for compiler in compilers:
        if compiler.family == family and (constraint is None or constraint.match(compiler.version)):
            return compiler
    return None
```

- [ ] **Step 2: Update _write_cmake_initial_cache to inject CMAKE_CXX_COMPILER**

```python
def _write_cmake_initial_cache(root: Path, manifest: dict[str, Any], compiler: Compiler | None = None) -> Path:
    cache = root / manifest.get("build_dir", "build/default") / "enm-config.cmake"
    values = {
        "ENM_TARGET": str(manifest["target"]),
        "ENM_PROJECT_VERSION": str(manifest["version"]),
        "ENM_EUI_VERSION": str(manifest["eui"]["version"]).removeprefix("v"),
    }
    lines = ["# Auto-generated by ENM. Do not edit manually.", ""]
    for key, value in values.items():
        if not re.match(r"^[A-Za-z0-9_./\\:-]+$", value):
            raise ReleaseError(f"unsafe {key} value in {MANIFEST}: {value}")
        escaped = value.replace("\\", "/")
        lines.append(f'set({key} "{escaped}" CACHE STRING "" FORCE)')
    if compiler:
        escaped = str(compiler.path).replace("\\", "/")
        lines.append(f'set(CMAKE_CXX_COMPILER "{escaped}" CACHE FILEPATH "" FORCE)')
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cache
```

- [ ] **Step 3: Update configure to validate toolchain and EUI SDK**

In `src/enm/project.py` `configure` function, before running CMake:

```python
compiler = _resolve_toolchain_compiler(manifest)
toolchain = manifest.get("toolchain") or {}
if toolchain.get("compiler") and not compiler and not force:
    raise ReleaseError(
        f"project requires {toolchain['compiler']} {toolchain.get('version', '')} "
        "but no matching compiler was found. "
        "Run 'enm configure --force' to ignore the toolchain constraint."
    )

# Existing EUI SDK validation
sdk = _release_version_from_manifest(...)
active = store.active().version
if sdk != active and not force:
    raise ReleaseError(
        f"active SDK is {active} but project requires {sdk}. "
        "Run 'enm configure --force' to ignore the SDK mismatch."
    )

cache = _write_cmake_initial_cache(root, manifest, compiler)
```

Note: Add `force: bool = False` parameter to `configure`.

- [ ] **Step 4: Write tests for compiler injection**

```python
def test_configure_injects_compiler_when_toolchain_constrained(self):
    import tempfile
    from pathlib import Path
    from unittest import mock
    from enm.project import create_project, _write_cmake_initial_cache
    from enm.toolchain import Compiler

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        create_project(root, "App", "v0.5.6")
        manifest = {
            "schema": 2,
            "target": "App",
            "version": "0.1.0",
            "eui": {"version": "v0.5.6"},
            "build_dir": "build/default",
            "toolchain": {"compiler": "gcc", "version": ">=12"},
        }
        compiler = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++ 13.2")
        cache = _write_cmake_initial_cache(root, manifest, compiler)
        content = cache.read_text(encoding="utf-8")
    self.assertIn('set(CMAKE_CXX_COMPILER "/usr/bin/g++" CACHE FILEPATH "" FORCE)', content)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_project.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/enm/project.py tests/test_project.py
git commit -m "feat: resolve toolchain compiler and inject CMAKE_CXX_COMPILER into enm-config.cmake"
```

---

### Task 4: Add doctor compiler sub-checks and constraint checking

**Files:**
- Modify: `src/enm/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Replace single c++17 compiler check with three family checks**

In `_compiler_checks` (refactor existing code):

```python
def _compiler_checks(temp_root: Path | None = None) -> list[Check]:
    from .toolchain import detect_compilers

    checks: list[Check] = []
    compilers = {c.family: c for c in detect_compilers()}
    for family, minimum in (("msvc", MSVC_MINIMUM), ("gcc", GCC_MINIMUM), ("clang", CLANG_MINIMUM)):
        compiler = compilers.get(family)
        if not compiler:
            checks.append(Check(f"compiler-{family}", "missing", f"{family} compiler not found", required=False))
            continue
        label = ".".join(str(p) for p in compiler.version)
        if compiler.version >= minimum:
            checks.append(Check(f"compiler-{family}", "ok", f"{label} ({compiler.path})", str(compiler.path), required=False))
        else:
            checks.append(Check(f"compiler-{family}", "unsupported", f"{label} requires >= {_minimum_label(minimum)}", str(compiler.path), required=False))
    return checks
```

- [ ] **Step 2: Add toolchain constraint check in _sdk_check or new _toolchain_check**

Add in `run_doctor` after `_compiler_checks`:

```python
if project_root:
    checks.extend(_toolchain_constraint_checks(project_root))
```

Implement:

```python
def _toolchain_constraint_checks(project_root: Path) -> list[Check]:
    from .toolchain import VersionConstraint, resolve_compiler

    try:
        manifest = load_manifest(project_root)
    except ReleaseError:
        return []
    toolchain = manifest.get("toolchain") or {}
    family = toolchain.get("compiler")
    if not family:
        return []

    compiler = resolve_compiler()
    if not compiler:
        return [Check("toolchain", "missing", f"project requires {family} but no compiler was found", required=True)]
    if compiler.family != family:
        return [Check("toolchain", "unsupported", f"project requires {family} but current compiler is {compiler.family}", str(compiler.path), required=True)]

    version_text = toolchain.get("version", "")
    if version_text:
        constraint = VersionConstraint.parse(version_text)
        if not constraint.match(compiler.version):
            return [Check("toolchain", "unsupported", f"project requires {family} {version_text} but current is {compiler.family} {'.'.join(map(str, compiler.version))}", str(compiler.path), required=True)]
    return [Check("toolchain", "ok", f"{compiler.family} {'.'.join(map(str, compiler.version))} matches {family} {version_text}", str(compiler.path), required=True)]
```

- [ ] **Step 3: Write tests for doctor compiler checks**

```python
def test_doctor_reports_compiler_family_checks(self):
    from unittest import mock
    from enm.doctor import _compiler_checks
    from enm.toolchain import Compiler

    compilers = [
        Compiler(Path("/usr/bin/cl"), "msvc", (19, 44), "cl"),
        Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++"),
    ]
    with mock.patch("enm.doctor.detect_compilers", return_value=compilers):
        checks = _compiler_checks()
    names = {c.name: c.status for c in checks}
    self.assertEqual(names["compiler-msvc"], "ok")
    self.assertEqual(names["compiler-gcc"], "ok")
    self.assertEqual(names["compiler-clang"], "missing")

def test_doctor_project_toolchain_mismatch(self):
    import tempfile
    from pathlib import Path
    from unittest import mock
    from enm.doctor import _toolchain_constraint_checks
    from enm.project import create_project
    from enm.toolchain import Compiler

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        create_project(root, "App", "v0.5.6")
        manifest_path = root / "enm-project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["toolchain"] = {"compiler": "msvc", "version": ">=19.44,<20"}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        current = Compiler(Path("/usr/bin/g++"), "gcc", (13, 2), "g++")
        with mock.patch("enm.doctor.resolve_compiler", return_value=current):
            checks = _toolchain_constraint_checks(root)
    self.assertEqual(len(checks), 1)
    self.assertEqual(checks[0].status, "unsupported")
    self.assertIn("msvc", checks[0].detail)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_doctor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/enm/doctor.py tests/test_doctor.py
git commit -m "feat: add family-specific compiler checks and project toolchain constraint validation"
```

---

### Task 5: Add doctor fix suggestions for toolchain mismatches

**Files:**
- Modify: `src/enm/doctor.py` (`fix_missing_dependencies`)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Add toolchain fix logic in fix_missing_dependencies**

When a `toolchain` check is `unsupported` or `missing`:

```python
if check.name == "toolchain" and check.status in ("missing", "unsupported"):
    if "msvc" in check.detail:
        print("MSVC must be installed manually. Download Visual Studio Build Tools from:")
        print("  https://visualstudio.microsoft.com/downloads/")
    elif "gcc" in check.detail:
        print("Install a matching GCC version, for example:")
        print("  Windows: winget install GnuWin32.GCC")
        print("  Ubuntu:  sudo apt install g++")
        print("  macOS:   brew install gcc")
    elif "clang" in check.detail:
        print("Install a matching Clang version, for example:")
        print("  Windows: winget install LLVM.LLVM")
        print("  Ubuntu:  sudo apt install clang")
        print("  macOS:   brew install llvm")
    print("Alternatively, run 'enm lock-compiler' to update the manifest or 'enm configure --force' to ignore the constraint.")
    continue
```

- [ ] **Step 2: Add test for toolchain fix suggestion**

```python
def test_fix_suggests_manual_install_for_msvc(self):
    from io import StringIO
    from unittest import mock
    from enm.doctor import Check, fix_missing_dependencies

    checks = [Check("toolchain", "unsupported", "project requires msvc >=19.44,<20 but current compiler is gcc", required=True)]
    with mock.patch("sys.stdout", new=StringIO()) as output:
        fix_missing_dependencies(checks, yes=False, force=False)
    text = output.getvalue()
    self.assertIn("MSVC must be installed manually", text)
    self.assertIn("visualstudio.microsoft.com", text)
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_doctor.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/enm/doctor.py tests/test_doctor.py
git commit -m "feat: add doctor fix guidance for toolchain constraints"
```

---

### Task 6: Add `enm lock-compiler` CLI command

**Files:**
- Modify: `src/enm/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Implement cmd_lock_compiler**

```python
def cmd_lock_compiler(args: argparse.Namespace) -> int:
    root = _project(args)
    manifest = load_manifest(root)
    compilers = detect_compilers()
    if not compilers:
        print("error: no supported compiler found (msvc, gcc, clang)", file=sys.stderr)
        return 2
    if len(compilers) == 1:
        selected = compilers[0]
    else:
        print("Multiple compilers detected. Select one:")
        for index, compiler in enumerate(compilers, 1):
            version = ".".join(str(p) for p in compiler.version)
            print(f"  {index}. {compiler.family} {version} ({compiler.path})")
        while True:
            try:
                choice = int(input("Enter number: ")) - 1
                if 0 <= choice < len(compilers):
                    selected = compilers[choice]
                    break
            except ValueError:
                continue
            print("Invalid choice.")

    manifest.setdefault("toolchain", {})
    manifest["toolchain"]["compiler"] = selected.family
    manifest["toolchain"]["version"] = f"={selected.version[0]}.{selected.version[1]}"
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"locked toolchain: {selected.family} {manifest['toolchain']['version']}")
    return 0
```

- [ ] **Step 2: Register lock-compiler subparser**

```python
lock_compiler = commands.add_parser("lock-compiler", help="lock the current compiler into the project manifest")
lock_compiler.add_argument("--project", help="project directory")
lock_compiler.set_defaults(func=cmd_lock_compiler)
```

- [ ] **Step 3: Write test for parser registration**

```python
def test_lock_compiler_parser_registered(self):
    parser_obj = parser()
    args = parser_obj.parse_args(["lock-compiler"])
    self.assertEqual(args.func.__name__, "cmd_lock_compiler")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/enm/cli.py tests/test_cli.py
git commit -m "feat: add enm lock-compiler command"
```

---

### Task 7: Add `--force` to configure and build

**Files:**
- Modify: `src/enm/cli.py` (parser for configure/build/test)
- Modify: `src/enm/project.py` (configure/build/test signatures)

- [ ] **Step 1: Add --force argument to configure/build/test parsers**

```python
for name, function, help_text in (
    ("configure", cmd_configure, "configure the current project"),
    ("build", cmd_build, "build the current project"),
    ("test", cmd_test, "test the current project"),
):
    command = commands.add_parser(name, help=help_text)
    command.add_argument("--project")
    command.add_argument("--force", action="store_true", help="ignore toolchain and EUI SDK mismatch warnings")
    command.add_argument("extra", nargs=argparse.REMAINDER)
    command.set_defaults(func=function)
```

- [ ] **Step 2: Pass force through cmd_configure/cmd_build/cmd_test**

```python
def cmd_configure(args: argparse.Namespace) -> int:
    return configure(_project(args), _store(args), _extra(args), force=args.force)
```

Similarly for build and test.

- [ ] **Step 3: Update configure/build/test signatures in project.py**

```python
def configure(store: StateStore, root: Path, extra: list[str], force: bool = False) -> int:
    ...
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_cli.py tests/test_project.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/enm/cli.py src/enm/project.py tests/test_cli.py tests/test_project.py
git commit -m "feat: add --force to configure/build/test to ignore toolchain and SDK constraints"
```

---

### Task 8: Update version to 0.3.0

**Files:**
- Modify: `src/enm/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 1: Update version strings**

```python
# src/enm/__init__.py
__version__ = "0.3.0"
```

```toml
# pyproject.toml
version = "0.3.0"
```

```markdown
# README.md: replace all 0.2.3 with 0.3.0
```

- [ ] **Step 2: Commit**

```bash
git add src/enm/__init__.py pyproject.toml README.md
git commit -m "chore: bump version to 0.3.0"
```

---

### Task 9: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add lock-compiler to command reference**

After `enm about` section:

```markdown
### `enm lock-compiler` — 锁定编译器

检测当前可用编译器，将选中的编译器家族和版本写入 `enm-project.json` 的 `toolchain` 字段。检测到多个编译器时会提示选择。
```

- [ ] **Step 2: Add --force to configure/build/test tables**

Add row to each table:

```markdown
| `--force` | 忽略 toolchain 与 EUI SDK 版本不匹配警告，继续构建 |
```

- [ ] **Step 3: Add schema 2 note**

In the manifest description section, add:

```markdown
`enm-project.json` 从 schema 2 开始支持可选的 `toolchain` 字段，用于约束编译器家族和版本范围（如 `>=19.44,<20`）。schema 1 项目仍然兼容。
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: update README for lock-compiler, --force, and schema 2"
```

---

### Task 10: Full test run and final verification

- [ ] **Step 1: Run full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 2: Verify enm about/version still work**

Run: `python -m enm --version`
Expected: `enm 0.3.0`

Run: `echo "" | python -m enm about`
Expected: banner and credits printed, exit 0

- [ ] **Step 3: Manual smoke test lock-compiler**

Run in a temp project:

```bash
rm -rf /tmp/enm-smoke
python -m enm init Smoke --path /tmp/enm-smoke
cat /tmp/enm-smoke/enm-project.json | grep toolchain
python -m enm lock-compiler --project /tmp/enm-smoke
cat /tmp/enm-smoke/enm-project.json | grep -A2 toolchain
rm -rf /tmp/enm-smoke
```

- [ ] **Step 4: Commit any remaining changes**

```bash
git add .
git commit -m "test: verify full suite and smoke tests for compiler toolchain feature"
```

---

## Self-Review Coverage

- Schema 2 manifest: Task 2
- `enm lock-compiler`: Task 6
- Three compiler sub-checks: Task 4
- `doctor --project` constraint check: Task 4
- `doctor fix --project` guidance: Task 5
- Configure compiler injection: Task 3
- Build/configure `--force`: Task 7
- EUI SDK mismatch warning with `--force`: Task 3/7
- Version 0.3.0: Task 8
- README: Task 9
