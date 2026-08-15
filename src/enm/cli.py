from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .doctor import checks_as_dict, run_doctor
from .github import ReleaseClient, ReleaseError, normalize_arch, normalize_platform, normalize_tag
from .package import deploy, package_stage
from .project import (
    build,
    configure,
    create_project,
    find_project,
    generate_ci,
    load_manifest,
    supports_external_apps,
    test,
)
from .state import StateStore
from .ui import Spinner, level_label, yes_no


def _store(args: argparse.Namespace) -> StateStore:
    root = Path(args.home).expanduser() if getattr(args, "home", None) else None
    return StateStore(root)


def _release_version(args: argparse.Namespace, store: StateStore) -> str:
    if getattr(args, "version", None):
        return normalize_tag(args.version)
    try:
        return store.active().version
    except ReleaseError:
        return ReleaseClient().get_release("latest").tag


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(_store(args))
    if args.json:
        print(json.dumps(checks_as_dict(checks), indent=2))
    else:
        symbols = {"ok": "OK", "optional": "--", "missing": "!!", "unsupported": "!!", "error": "!!"}
        for check in checks:
            location = f" [{check.path}]" if check.path else ""
            print(f"{symbols.get(check.status, '??'):>2} {check.name:<12} {check.detail}{location}")
    return 1 if any(check.status in {"error", "unsupported"} for check in checks) else 0


def cmd_sdk_list(args: argparse.Namespace) -> int:
    store = _store(args)
    with Spinner("Reading EUI-NEO Releases"):
        client = ReleaseClient()
        releases = (
            [client.get_release(args.version, include_prerelease=args.include_prerelease)]
            if args.version
            else client.list_releases(include_prerelease=args.include_prerelease)
        )
    state = store.load()
    platform_name = normalize_platform()
    arch = normalize_arch()
    key = f"{platform_name}-{arch}"
    rows = []
    for release in releases:
        available = any(f"-{key}-sdk." in asset.name.lower() for asset in release.assets)
        rows.append(
            {
                "version": release.tag,
                "published_at": release.published_at,
                "prerelease": release.prerelease,
                "sdk_available": available,
                "installed": key in state["installed"].get(release.tag, {}),
                "active": state["active"].get(key) == release.tag,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'VERSION':<18} {'PUBLISHED':<12} {'HOST SDK':<8} {'INSTALLED':<9} ACTIVE")
        for row in rows:
            print(
                f"{row['version']:<18} {row['published_at'][:10]:<12} "
                f"{yes_no(row['sdk_available']):<17} "
                f"{yes_no(row['installed']):<18} "
                f"{'*' if row['active'] else ''}"
            )
    return 0


def cmd_sdk_installed(args: argparse.Namespace) -> int:
    state = _store(args).load()
    key = f"{normalize_platform()}-{normalize_arch()}"
    active = state["active"].get(key)
    rows = [
        {
            "version": version,
            "active": version == active,
            "path": details[key]["path"],
        }
        for version, details in sorted(state["installed"].items(), reverse=True)
        if key in details
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"No SDKs are installed for {key}.")
    else:
        print(f"{'VERSION':<18} {'ACTIVE':<7} PATH")
        for row in rows:
            print(f"{row['version']:<18} {('*' if row['active'] else ''):<7} {row['path']}")
    return 0


def cmd_sdk_install(args: argparse.Namespace) -> int:
    store = _store(args)
    platform_name = normalize_platform()
    arch = normalize_arch()
    with Spinner("Downloading and installing EUI-NEO SDK"):
        release = ReleaseClient().get_release(
            args.version, include_prerelease=args.include_prerelease
        )
        installed = store.install(
            release,
            platform_name,
            arch,
            force=args.force,
            allow_unverified=args.allow_unverified,
        )
    print(f"installed and activated {installed.version} at {installed.path}")
    return 0


def cmd_sdk_use(args: argparse.Namespace) -> int:
    installed = _store(args).set_active(
        normalize_tag(args.version), normalize_platform(), normalize_arch()
    )
    print(f"active SDK: {installed.version} ({installed.platform}-{installed.arch})")
    print(installed.path)
    return 0


def cmd_sdk_path(args: argparse.Namespace) -> int:
    print(_store(args).active().path)
    return 0


def cmd_sdk_uninstall(args: argparse.Namespace) -> int:
    version = normalize_tag(args.version)
    removed = _store(args).uninstall(
        version,
        normalize_platform(),
        normalize_arch(),
        force=args.force,
    )
    print(f"removed SDK {version}: {removed}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    if args.ci and not args.install_spec:
        raise ReleaseError("--ci github requires --install-spec with a real ENM source")
    if args.install_spec and not args.ci:
        raise ReleaseError("--install-spec is only valid together with --ci github")
    store = _store(args)
    version = _release_version(args, store)
    try:
        selected_sdk = store.get_installed(version, normalize_platform(), normalize_arch())
    except ReleaseError:
        selected_sdk = None
    if selected_sdk and not supports_external_apps(selected_sdk):
        raise ReleaseError(
            f"EUI-NEO SDK {version} cannot create an external application because it exports "
            "neither eui_neo_configure_app() nor eui::neo. Install a compatible SDK, activate it with "
            "'enm sdk use VERSION', or pass 'enm init --version VERSION'."
        )
    destination = Path(args.path or args.name)
    result = create_project(destination, args.name, version, force=args.force)
    print(f"created {result}")
    print(f"pinned EUI-NEO release: {version}")
    if args.ci:
        path = generate_ci(result, version, install_spec=args.install_spec)
        print(f"created GitHub Actions workflow: {path}")
    return 0


def _project(args: argparse.Namespace) -> Path:
    return find_project(Path(args.project) if args.project else None)


def _extra(args: argparse.Namespace) -> list[str]:
    return args.extra[1:] if args.extra[:1] == ["--"] else args.extra


def cmd_configure(args: argparse.Namespace) -> int:
    return configure(_project(args), _store(args), _extra(args))


def cmd_build(args: argparse.Namespace) -> int:
    return build(_project(args), _store(args), _extra(args))


def cmd_test(args: argparse.Namespace) -> int:
    return test(_project(args), _store(args), _extra(args))


def cmd_deploy(args: argparse.Namespace) -> int:
    root = _project(args)
    result = deploy(
        root,
        _store(args),
        destination=Path(args.destination) if args.destination else None,
        binary=Path(args.binary) if args.binary else None,
        force=args.force,
    )
    print(result)
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    root = _project(args)
    stage = deploy(root, _store(args), binary=Path(args.binary) if args.binary else None, force=True)
    archive, sidecar = package_stage(stage, args.format)
    print(archive)
    print(sidecar)
    return 0


def cmd_ci_init(args: argparse.Namespace) -> int:
    root = _project(args)
    version = load_manifest(root)["eui"]["version"]
    path = generate_ci(root, version, install_spec=args.install_spec)
    print(path)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="enm", description="ENM - unofficial EUI-NEO toolchain manager")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--home", help="override ENM state directory")
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="inspect the local build environment")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    sdk = commands.add_parser("sdk", help="manage SDKs from GitHub Releases")
    sdk_commands = sdk.add_subparsers(dest="sdk_command", required=True)
    sdk_list = sdk_commands.add_parser("list", help="list versions from GitHub Releases")
    sdk_list.add_argument("version", nargs="?", help="show only this EUI release tag")
    sdk_list.add_argument("--include-prerelease", action="store_true")
    sdk_list.add_argument("--json", action="store_true")
    sdk_list.set_defaults(func=cmd_sdk_list)
    installed = sdk_commands.add_parser("installed", help="list SDKs installed for this computer")
    installed.add_argument("--json", action="store_true")
    installed.set_defaults(func=cmd_sdk_installed)
    install = sdk_commands.add_parser("install", help="download and verify a release SDK")
    install.add_argument("version", nargs="?", default="latest")
    install.add_argument("--include-prerelease", action="store_true")
    install.add_argument("--force", action="store_true")
    install.add_argument("--allow-unverified", action="store_true")
    install.set_defaults(func=cmd_sdk_install)
    use = sdk_commands.add_parser("use", help="activate an installed SDK")
    use.add_argument("version")
    use.set_defaults(func=cmd_sdk_use)
    sdk_path = sdk_commands.add_parser("path", help="print the active SDK path")
    sdk_path.set_defaults(func=cmd_sdk_path)
    uninstall = sdk_commands.add_parser("uninstall", help="remove one installed SDK version")
    uninstall.add_argument("version", help="required EUI release tag, for example v0.5.5")
    uninstall.add_argument("--force", action="store_true", help="allow removing the active SDK")
    uninstall.set_defaults(func=cmd_sdk_uninstall)

    init = commands.add_parser("init", help="create an external EUI application")
    init.add_argument("name")
    init.add_argument("--path")
    init.add_argument("--version", help="EUI release tag; defaults to active or latest")
    init.add_argument("--force", action="store_true")
    init.add_argument("--ci", choices=("github",), help="also initialize a CI provider")
    init.add_argument("--install-spec", help="pip source for ENM; required with --ci")
    init.set_defaults(func=cmd_init)

    for name, function, help_text in (
        ("configure", cmd_configure, "configure the current project"),
        ("build", cmd_build, "build the current project"),
        ("test", cmd_test, "test the current project"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project")
        command.add_argument("extra", nargs=argparse.REMAINDER)
        command.set_defaults(func=function)

    deploy_parser = commands.add_parser("deploy", help="stage a built application")
    deploy_parser.add_argument("--project")
    deploy_parser.add_argument("--destination")
    deploy_parser.add_argument("--binary")
    deploy_parser.add_argument("--force", action="store_true")
    deploy_parser.set_defaults(func=cmd_deploy)

    package_parser = commands.add_parser("package", help="stage and archive a built application")
    package_parser.add_argument("--project")
    package_parser.add_argument("--binary")
    package_parser.add_argument("--format", choices=("zip", "tar.gz"), default="zip")
    package_parser.set_defaults(func=cmd_package)

    ci = commands.add_parser("ci", help="manage consumer CI")
    ci_commands = ci.add_subparsers(dest="ci_command", required=True)
    ci_init = ci_commands.add_parser("init", help="initialize CI for a project")
    ci_init.add_argument("provider", choices=("github",))
    ci_init.add_argument("--project")
    ci_init.add_argument("--install-spec", required=True, help="pip source for ENM")
    ci_init.set_defaults(func=cmd_ci_init)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        print(f"{level_label('error', sys.stderr)}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
