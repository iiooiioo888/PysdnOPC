"""Structured Docker operations tools.

Provides Docker image build/pull/ls, container run/ls/stop/rm,
Docker Compose up/down. All operations require approval confirmation.
"""

from __future__ import annotations

import shlex
from typing import Any

from opc.layer4_tools.registry import ToolDefinition
from opc.layer4_tools.shell import shell_exec



# ---------------------------------------------------------------------------
# Security policy constants and checks
# ---------------------------------------------------------------------------

# Capabilities that are considered safe to add to containers.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({
    "NET_BIND_SERVICE", "CHOWN", "SETUID", "SETGID", "KILL",
    "NET_RAW", "SYS_CHROOT", "MKNOD", "AUDIT_WRITE", "SETFCAP",
})

# Capabilities that are explicitly dangerous and always blocked with a
# clear security violation (superset of what is simply "not allowed").
DANGEROUS_CAPABILITIES: frozenset[str] = frozenset({
    "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_RAWIO",
    "SYS_BOOT", "NET_ADMIN", "DAC_OVERRIDE", "DAC_READ_SEARCH",
    "LINUX_IMMUTABLE", "IPC_LOCK", "IPC_OWNER", "SYS_NICE",
    "SYS_RESOURCE", "SYS_TIME", "SYS_TTY_CONFIG", "LEASE",
    "WAKE_ALARM", "BLOCK_SUSPEND", "AUDIT_CONTROL", "MAC_ADMIN",
    "MAC_OVERRIDE", "SYSLOG",
})

# Default volume path whitelist. Operators can extend via config.
DEFAULT_VOLUME_WHITELIST: tuple[str, ...] = (
    "/tmp",
    "/var/tmp",
)

# Sensitive system paths that must NEVER be mounted regardless of whitelist.
SENSITIVE_PATH_BLACKLIST: tuple[str, ...] = (
    "/etc",
    "/root",
    "/proc",
    "/sys",
    "/boot",
    "/dev",
    "/var/run/docker.sock",
    "/var/lib/docker",
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
)

# Docker subcommands classified as high-risk (require explicit confirmation).
HIGH_RISK_SUBCOMMANDS: frozenset[str] = frozenset({
    "system prune", "volume rm", "volume prune", "image prune",
    "container prune", "network prune", "builder prune",
})


class DockerSecurityViolation(Exception):
    """Raised when a docker command violates the security policy."""


def _normalize_volume_path(path_str: str) -> str:
    """Normalize a volume path for comparison."""
    import re
    from pathlib import PurePosixPath, PureWindowsPath
    path_str = path_str.strip()
    if re.match(r"^[A-Za-z]:\\\\", path_str):
        return str(PureWindowsPath(path_str)).lower().rstrip(chr(92))
    return str(PurePosixPath(path_str)).rstrip("/") or "/"


def _contains_directory_traversal(path_str: str) -> bool:
    """Detect directory traversal sequences in a path.

    Returns True if the path contains '..' components that could be used
    to escape an allowed prefix (e.g. /tmp/../etc/passwd).
    """
    import re
    # Normalize backslashes for Windows-style paths
    normalized = path_str.replace("\\", "/")
    # Check for '..' as a path component
    parts = normalized.split("/")
    return ".." in parts


def _is_sensitive_path(path_str: str) -> str | None:
    """Check if a path targets a sensitive system location.

    Returns the matched sensitive prefix if the path is sensitive, else None.
    This check runs AFTER normalization and traversal resolution.
    """
    import posixpath
    # Resolve any remaining traversal to get the canonical target
    normalized = posixpath.normpath(path_str.replace("\\", "/"))
    normalized = normalized.rstrip("/") or "/"
    for sensitive in SENSITIVE_PATH_BLACKLIST:
        s_norm = sensitive.rstrip("/") or "/"
        if normalized == s_norm or normalized.startswith(s_norm + "/"):
            return sensitive
    return None


def validate_volume_mount(
    volume_spec: str,
    whitelist: tuple[str, ...] | list[str] = DEFAULT_VOLUME_WHITELIST,
) -> None:
    """Validate a volume mount specification against security policy.

    Security layers:
    1. Directory traversal detection (blocks '..' components)
    2. Sensitive path blacklist (blocks /etc, /root, /proc, etc.)
    3. Whitelist enforcement (only explicitly allowed prefixes pass)

    Raises DockerSecurityViolation on any policy violation.
    """
    import re
    parts = volume_spec.split(chr(58))
    # Handle Windows drive letters
    if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
        host_path = parts[0] + chr(58) + parts[1]
    elif len(parts) >= 2:
        host_path = parts[0]
        # Check if first part is a named volume (not a path)
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", host_path):
            return  # Named volume, allowed
    else:
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume_spec):
            return  # Named volume, allowed
        host_path = parts[0]

    # --- Layer 1: Directory traversal detection ---
    if _contains_directory_traversal(host_path):
        raise DockerSecurityViolation(
            f"Volume mount path '{host_path}' contains directory traversal '..' "
            f"which is prohibited by security policy."
        )

    # --- Layer 2: Sensitive path blacklist ---
    sensitive_match = _is_sensitive_path(host_path)
    if sensitive_match:
        raise DockerSecurityViolation(
            f"Volume mount path '{host_path}' targets sensitive system location "
            f"'{sensitive_match}' which is prohibited by security policy."
        )

    # --- Layer 3: Whitelist enforcement ---
    normalized = _normalize_volume_path(host_path)
    for allowed in whitelist:
        allowed_norm = _normalize_volume_path(allowed)
        if normalized == allowed_norm or normalized.startswith(allowed_norm + "/") or normalized.startswith(allowed_norm + chr(92)):
            return
    raise DockerSecurityViolation(
        f"Volume mount path '{host_path}' is not in the allowed whitelist. "
        f"Allowed prefixes: {', '.join(whitelist)}"
    )


def check_docker_command_security(
    command: str,
    volume_whitelist: tuple[str, ...] | list[str] = DEFAULT_VOLUME_WHITELIST,
) -> None:
    """Perform semantic security checks on a docker command.

    Raises DockerSecurityViolation for policy violations:
    - --privileged is always prohibited
    - --cap-add is restricted to the allowed capabilities whitelist
    - Volume mounts (-v / --volume) must use whitelisted host paths
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        raise DockerSecurityViolation("Cannot parse docker command (unbalanced quotes)")
    # Check for --privileged
    for token in tokens:
        if token == "--privileged" or token.startswith("--privileged="):
            raise DockerSecurityViolation(
                "The --privileged flag is prohibited by security policy. "
                "Use specific --cap-add capabilities instead."
            )
    # Check --cap-add values
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--cap-add" and i + 1 < len(tokens):
            cap = tokens[i + 1].upper()
            if cap not in ALLOWED_CAPABILITIES:
                if cap in DANGEROUS_CAPABILITIES:
                    raise DockerSecurityViolation(
                        f"Capability '{tokens[i + 1]}' is a DANGEROUS capability "
                        f"that grants elevated host access and is strictly prohibited. "
                        f"Allowed capabilities: {', '.join(sorted(ALLOWED_CAPABILITIES))}"
                    )
                raise DockerSecurityViolation(
                    f"Capability '{tokens[i + 1]}' is not in the allowed list. "
                    f"Allowed capabilities: {', '.join(sorted(ALLOWED_CAPABILITIES))}"
                )
            i += 2
            continue
        if token.startswith("--cap-add="):
            cap = token.split("=", 1)[1].upper()
            if cap not in ALLOWED_CAPABILITIES:
                if cap in DANGEROUS_CAPABILITIES:
                    raise DockerSecurityViolation(
                        f"Capability '{token.split(chr(61), 1)[1]}' is a DANGEROUS capability "
                        f"that grants elevated host access and is strictly prohibited. "
                        f"Allowed capabilities: {', '.join(sorted(ALLOWED_CAPABILITIES))}"
                    )
                raise DockerSecurityViolation(
                    f"Capability '{token.split(chr(61), 1)[1]}' is not in the allowed list. "
                    f"Allowed capabilities: {', '.join(sorted(ALLOWED_CAPABILITIES))}"
                )
        i += 1
    # Check volume mounts
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-v", "--volume") and i + 1 < len(tokens):
            validate_volume_mount(tokens[i + 1], volume_whitelist)
            i += 2
            continue
        if token.startswith("--volume="):
            validate_volume_mount(token.split("=", 1)[1], volume_whitelist)
        i += 1


def is_high_risk_docker_command(command: str) -> bool:
    """Check if a docker command is classified as high-risk."""
    normalized = " ".join(command.split()).lower()
    if normalized.startswith("docker "):
        normalized = normalized[7:]
    for subcmd in HIGH_RISK_SUBCOMMANDS:
        if normalized.startswith(subcmd):
            return True
    return False

_DEFAULT_TIMEOUT = 600


def _quote(value: str) -> str:
    """Shell-quote a user-supplied value to prevent injection."""
    return shlex.quote(str(value))


# ---------------------------------------------------------------------------
# Image operations
# ---------------------------------------------------------------------------


async def docker_image_build(
    context: str = ".",
    dockerfile: str = "Dockerfile",
    tag: str = "",
    target: str = "",
    working_directory: str | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    """Build a Docker image from a Dockerfile.

    Security: validates the assembled command against policy before execution.
    """
    cmd = f"docker build {_quote(context)} -f {_quote(dockerfile)}"
    if tag:
        cmd += f" -t {_quote(tag)}"
    if target:
        cmd += f" --target {_quote(target)}"
    check_docker_command_security(cmd)
    return await shell_exec(cmd, working_directory=working_directory, timeout=_DEFAULT_TIMEOUT, task=task)


async def docker_image_pull(
    image: str,
    task: Any | None = None,
) -> dict[str, Any]:
    """Pull a Docker image from a registry."""
    cmd = f"docker pull {_quote(image)}"
    return await shell_exec(cmd, timeout=_DEFAULT_TIMEOUT, task=task)


async def docker_image_ls(
    task: Any | None = None,
) -> dict[str, Any]:
    """List local Docker images."""
    return await shell_exec("docker image ls", task=task)


# ---------------------------------------------------------------------------
# Container operations
# ---------------------------------------------------------------------------


async def docker_container_run(
    image: str,
    command: str = "",
    ports: list[str] | None = None,
    volumes: list[str] | None = None,
    env: dict[str, str] | None = None,
    detach: bool = True,
    name: str = "",
    task: Any | None = None,
) -> dict[str, Any]:
    """Run a Docker container.

    Security: validates volume mounts against the whitelist and runs a
    semantic security check on the assembled command before execution.
    Raises DockerSecurityViolation on policy violations.
    """
    # --- Security: validate volume mounts before building the command ---
    for v in volumes or []:
        validate_volume_mount(v)

    cmd = "docker run"
    if detach:
        cmd += " -d"
    if name:
        cmd += f" --name {_quote(name)}"
    for p in ports or []:
        cmd += f" -p {_quote(p)}"
    for v in volumes or []:
        cmd += f" -v {_quote(v)}"
    for k, val in (env or {}).items():
        cmd += f" -e {_quote(k + '=' + val)}"
    cmd += f" {_quote(image)}"
    if command:
        cmd += f" {command}"

    # --- Security: final semantic check on the assembled command ---
    check_docker_command_security(cmd)

    return await shell_exec(cmd, timeout=_DEFAULT_TIMEOUT, task=task)


async def docker_container_ls(
    all_containers: bool = True,
    task: Any | None = None,
) -> dict[str, Any]:
    """List Docker containers."""
    cmd = "docker container ls"
    if all_containers:
        cmd += " -a"
    return await shell_exec(cmd, task=task)


async def docker_container_stop(
    container: str,
    task: Any | None = None,
) -> dict[str, Any]:
    """Stop a running Docker container."""
    cmd = f"docker container stop {_quote(container)}"
    return await shell_exec(cmd, timeout=60, task=task)


async def docker_container_rm(
    container: str,
    force: bool = False,
    task: Any | None = None,
) -> dict[str, Any]:
    """Remove a Docker container."""
    cmd = "docker container rm"
    if force:
        cmd += " -f"
    cmd += f" {_quote(container)}"
    return await shell_exec(cmd, task=task)


# ---------------------------------------------------------------------------
# Compose operations
# ---------------------------------------------------------------------------


async def docker_compose_up(
    compose_file: str = "docker-compose.yml",
    services: list[str] | None = None,
    detach: bool = True,
    working_directory: str | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    """Start services defined in a Docker Compose file."""
    cmd = f"docker compose -f {_quote(compose_file)} up"
    if detach:
        cmd += " -d"
    for svc in services or []:
        cmd += f" {_quote(svc)}"
    return await shell_exec(cmd, working_directory=working_directory, timeout=_DEFAULT_TIMEOUT, task=task)


async def docker_compose_down(
    compose_file: str = "docker-compose.yml",
    working_directory: str | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    """Stop and remove services defined in a Docker Compose file."""
    cmd = f"docker compose -f {_quote(compose_file)} down"
    return await shell_exec(cmd, working_directory=working_directory, timeout=120, task=task)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def create_docker_tools() -> list[ToolDefinition]:
    """Create all Docker structured tool definitions."""
    return [
        ToolDefinition(
            name="docker_image_build",
            description="Build a Docker image from a Dockerfile in the given context directory.",
            parameters={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "Build context directory", "default": "."},
                    "dockerfile": {"type": "string", "description": "Path to Dockerfile", "default": "Dockerfile"},
                    "tag": {"type": "string", "description": "Image tag (e.g. myapp:latest)"},
                    "target": {"type": "string", "description": "Multi-stage build target stage"},
                    "working_directory": {"type": "string", "description": "Working directory for the command"},
                },
                "required": [],
            },
            func=docker_image_build,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_image_pull",
            description="Pull a Docker image from a registry.",
            parameters={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Image reference (e.g. nginx:latest)"},
                },
                "required": ["image"],
            },
            func=docker_image_pull,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_image_ls",
            description="List local Docker images.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            func=docker_image_ls,
            category="infrastructure",
            requires_confirmation=False,
            read_only=True,
        ),
        ToolDefinition(
            name="docker_container_run",
            description="Run a Docker container with optional ports, volumes, and environment variables.",
            parameters={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Docker image to run"},
                    "command": {"type": "string", "description": "Command to execute in the container"},
                    "ports": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Port mappings (e.g. [\'8080:80\'])",
                    },
                    "volumes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Volume mounts (e.g. [\'/host/path:/container/path\'])",
                    },
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Environment variables as key-value pairs",
                    },
                    "detach": {"type": "boolean", "description": "Run in detached mode", "default": True},
                    "name": {"type": "string", "description": "Container name"},
                },
                "required": ["image"],
            },
            func=docker_container_run,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_container_ls",
            description="List Docker containers.",
            parameters={
                "type": "object",
                "properties": {
                    "all_containers": {"type": "boolean", "description": "Include stopped containers", "default": True},
                },
                "required": [],
            },
            func=docker_container_ls,
            category="infrastructure",
            requires_confirmation=False,
            read_only=True,
        ),
        ToolDefinition(
            name="docker_container_stop",
            description="Stop a running Docker container.",
            parameters={
                "type": "object",
                "properties": {
                    "container": {"type": "string", "description": "Container ID or name"},
                },
                "required": ["container"],
            },
            func=docker_container_stop,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_container_rm",
            description="Remove a Docker container.",
            parameters={
                "type": "object",
                "properties": {
                    "container": {"type": "string", "description": "Container ID or name"},
                    "force": {"type": "boolean", "description": "Force removal of running container", "default": False},
                },
                "required": ["container"],
            },
            func=docker_container_rm,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_compose_up",
            description="Start services defined in a Docker Compose file.",
            parameters={
                "type": "object",
                "properties": {
                    "compose_file": {"type": "string", "description": "Path to compose file", "default": "docker-compose.yml"},
                    "services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific services to start (empty = all)",
                    },
                    "detach": {"type": "boolean", "description": "Run in detached mode", "default": True},
                    "working_directory": {"type": "string", "description": "Working directory for the command"},
                },
                "required": [],
            },
            func=docker_compose_up,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_compose_down",
            description="Stop and remove services defined in a Docker Compose file.",
            parameters={
                "type": "object",
                "properties": {
                    "compose_file": {"type": "string", "description": "Path to compose file", "default": "docker-compose.yml"},
                    "working_directory": {"type": "string", "description": "Working directory for the command"},
                },
                "required": [],
            },
            func=docker_compose_down,
            category="infrastructure",
            requires_confirmation=True,
        ),
    ]
