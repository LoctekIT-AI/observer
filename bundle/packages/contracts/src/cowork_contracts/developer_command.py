"""Versioned shape rules for Desktop local developer commands."""

from __future__ import annotations

from os.path import basename
DEVELOPER_COMMAND_CONTRACT_VERSION = "cowork.developer_command.v1"

DEVELOPER_COMMAND_OPERATIONS = frozenset({
    "developer_command.run",
    "developer_command.inspect_project",
    "developer_command.review_changes",
    "developer_command.install_python_dependencies",
    "developer_command.start_dev_server",
    "developer_command.stop_dev_server",
})

_OPERATION_ALIASES = {
    "run": "developer_command.run",
    "inspect_project": "developer_command.inspect_project",
    "review_changes": "developer_command.review_changes",
    "install_python_dependencies": "developer_command.install_python_dependencies",
    "start_dev_server": "developer_command.start_dev_server",
    "stop_dev_server": "developer_command.stop_dev_server",
}


def canonical_developer_command_operation(value: object) -> str:
    operation = str(value or "developer_command.run").strip().lower()
    return _OPERATION_ALIASES.get(operation, operation)


def normalize_developer_command_argv(operation: object, raw_argv: object) -> list[str]:
    """Return canonical argv without changing the executable allow-list.

    A few providers have emitted the structured operation a second time as
    ``argv[0]``. Removing that marker belongs at the provider boundary; the
    final Desktop Host still validates the resulting executable and arguments.
    """

    values = raw_argv if isinstance(raw_argv, (list, tuple)) else []
    argv = [str(item or "").strip() for item in values if str(item or "").strip()]
    canonical_operation = canonical_developer_command_operation(operation)
    if canonical_operation in {
        "developer_command.run",
        "developer_command.start_dev_server",
    } and argv:
        marker = canonical_developer_command_operation(argv[0])
        if marker == canonical_operation or (
            argv[0].strip().lower() == "run"
            and canonical_operation == "developer_command.start_dev_server"
        ):
            argv = argv[1:]
    if len(argv) == 2 and basename(argv[0]).lower() == "node" and argv[1] == "-V":
        argv[1] = "--version"
    return argv


def short_developer_command_operation(value: object) -> str:
    canonical = canonical_developer_command_operation(value)
    return canonical.removeprefix("developer_command.")
