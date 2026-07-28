"""Structured Docker operations tools.

Provides Docker image build/pull/ls, container run/ls/stop/rm,
Docker Compose up/down. All operations require approval confirmation.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# Risk assessment for approval callback
# ---------------------------------------------------------------------------

# Sensitive host ports that should not be exposed without explicit approval.
_SENSITIVE_PORTS: frozenset[str] = frozenset({
    "22",     # SSH
    "2375",   # Docker daemon (unencrypted)
    "2376",   # Docker daemon (TLS)
    "3306",   # MySQL
    "5432",   # PostgreSQL
    "6379",   # Redis
    "27017",  # MongoDB
    "9200",   # Elasticsearch
    "11211",  # Memcached
})


def _assess_image_risk(image: str, risk_factors: list[str], recommendations: list[str]) -> None:
    """Assess risk factors related to the image reference."""
    if not image:
        return
    # Check for :latest tag or missing tag
    if ":" not in image:
        risk_factors.append(
            f"Image '{image}' has no explicit tag (defaults to ':latest')."
        )
        recommendations.append(
            f"Pin a specific version tag for image '{image}' instead of relying on ':latest'."
        )
    elif image.endswith(":latest"):
        risk_factors.append(
            f"Image '{image}' uses the ':latest' tag which is not reproducible."
        )
        recommendations.append(
            f"Pin a specific version tag for image '{image}' instead of ':latest'."
        )


def _assess_volumes_risk(
    volumes: list[str] | None,
    risk_factors: list[str],
    recommendations: list[str],
) -> None:
    """Assess risk factors related to volume mounts."""
    for vol in volumes or []:
        try:
            validate_volume_mount(vol)
        except DockerSecurityViolation as exc:
            risk_factors.append(f"Blocked volume mount '{vol}': {exc}")
            recommendations.append(
                f"Remove or replace volume mount '{vol}' — it violates security policy."
            )
            continue
        # Even allowed volumes carry some risk — flag bind mounts to host paths
        parts = vol.split(":")
        if len(parts) >= 2 and parts[0].startswith("/"):
            risk_factors.append(
                f"Bind mount exposes host path '{parts[0]}' to the container."
            )
            recommendations.append(
                f"Consider using a named volume instead of bind-mounting '{parts[0]}' "
                f"to limit host filesystem exposure."
            )


def _assess_ports_risk(
    ports: list[str] | None,
    risk_factors: list[str],
    recommendations: list[str],
) -> None:
    """Assess risk factors related to port mappings."""
    for port_spec in ports or []:
        # Port specs can be: "8080:80", "127.0.0.1:8080:80", "8080"
        parts = port_spec.split(":")
        host_port = parts[0] if len(parts) >= 1 else ""
        # Strip IP prefix if present (e.g. "127.0.0.1" -> ignore, take numeric)
        if not host_port.isdigit() and len(parts) >= 3:
            host_port = parts[1]
        if host_port in _SENSITIVE_PORTS:
            risk_factors.append(
                f"Port mapping exposes sensitive host port '{host_port}'."
            )
            recommendations.append(
                f"Avoid exposing sensitive port '{host_port}' to the container, "
                f"or bind it to 127.0.0.1 only."
            )
        # Flag binding to all interfaces (no 127.0.0.1 prefix)
        if not port_spec.startswith("127.0.0.1") and not port_spec.startswith("localhost"):
            container_port = parts[-1] if parts else ""
            if container_port.isdigit():
                risk_factors.append(
                    f"Port '{port_spec}' binds to all network interfaces (0.0.0.0)."
                )


def _assess_capabilities_risk(
    risk_factors: list[str],
    recommendations: list[str],
) -> None:
    """Note that --privileged and dangerous capabilities are blocked at security level."""
    risk_factors.append(
        "Container execution grants kernel capabilities to the container process."
    )
    recommendations.append(
        "Run containers with the minimum necessary capabilities; prefer '--cap-drop ALL' "
        "and add only the specific capabilities needed."
    )


def assess_docker_risk(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Assess the risk profile of a docker tool call for approval prompts.

    This function generates a detailed risk assessment that the approval
    callback uses to present actionable information to the user. It does NOT
    block operations — blocking is handled by :func:`check_docker_command_security`
    and :func:`validate_volume_mount`. Instead, it identifies risk factors
    and provides recommendations so the user can make an informed decision.

    Returns a dict with three keys:
        - ``summary``: str — one-line human-readable description of the operation.
        - ``risk_factors``: list[str] — identified risk factors (may be empty).
        - ``recommendations``: list[str] — actionable mitigation suggestions.
    """
    risk_factors: list[str] = []
    recommendations: list[str] = []
    args = arguments or {}

    # --- Read-only tools: minimal risk ---
    if tool_name in ("docker_image_ls", "docker_container_ls"):
        summary = f"Read-only Docker inspection: {tool_name}"
        return {
            "summary": summary,
            "risk_factors": [],
            "recommendations": [],
        }

    # --- docker_container_run: full risk analysis ---
    if tool_name == "docker_container_run":
        image = str(args.get("image", ""))
        volumes = args.get("volumes")
        ports = args.get("ports")
        command = str(args.get("command", ""))
        name = str(args.get("name", ""))

        _assess_image_risk(image, risk_factors, recommendations)
        _assess_volumes_risk(volumes, risk_factors, recommendations)
        _assess_ports_risk(ports, risk_factors, recommendations)
        _assess_capabilities_risk(risk_factors, recommendations)

        if command:
            risk_factors.append(
                f"Container will execute command: '{command}'."
            )
        if not args.get("detach", True):
            risk_factors.append(
                "Container runs in foreground (attached) mode."
            )

        summary = f"Run container from image '{image}'"
        if name:
            summary += f" as '{name}'"
        if volumes:
            summary += f" with {len(volumes)} volume mount(s)"
        if ports:
            summary += f", {len(ports)} port mapping(s)"

    # --- docker_image_build ---
    elif tool_name == "docker_image_build":
        context = str(args.get("context", "."))
        tag = str(args.get("tag", ""))
        dockerfile = str(args.get("dockerfile", "Dockerfile"))

        risk_factors.append(
            f"Building image from context '{context}' with Dockerfile '{dockerfile}'."
        )
        if tag:
            _assess_image_risk(tag, risk_factors, recommendations)
        else:
            risk_factors.append("Build will produce an untagged image.")
            recommendations.append("Specify a meaningful tag for the built image.")
        recommendations.append(
            "Verify the Dockerfile does not embed secrets (e.g. API keys, passwords) "
            "before building."
        )
        summary = f"Build Docker image from '{context}'" + (f" as '{tag}'" if tag else "")

    # --- docker_image_pull ---
    elif tool_name == "docker_image_pull":
        image = str(args.get("image", ""))
        _assess_image_risk(image, risk_factors, recommendations)
        # Flag pulling from unknown registries
        if image and "/" in image and "." not in image.split("/")[0]:
            risk_factors.append(
                f"Image '{image}' may originate from an untrusted registry."
            )
            recommendations.append(
                "Verify the image provenance and scan for vulnerabilities after pulling."
            )
        summary = f"Pull Docker image '{image}'"

    # --- docker_container_stop ---
    elif tool_name == "docker_container_stop":
        container = str(args.get("container", ""))
        risk_factors.append(f"Stopping container '{container}'.")
        recommendations.append(
            f"Confirm container '{container}' is not a production-critical service before stopping."
        )
        summary = f"Stop container '{container}'"

    # --- docker_container_rm ---
    elif tool_name == "docker_container_rm":
        container = str(args.get("container", ""))
        force = bool(args.get("force", False))
        risk_factors.append(f"Removing container '{container}'.")
        if force:
            risk_factors.append(
                f"Force removal (--force) will kill and remove '{container}' if it is running."
            )
            recommendations.append(
                "Stop the container gracefully before removing; use --force only as a last resort."
            )
        recommendations.append(
            f"Verify no persistent data in container '{container}' will be lost."
        )
        summary = f"Remove container '{container}'" + (" (force)" if force else "")

    # --- docker_compose_up ---
    elif tool_name == "docker_compose_up":
        compose_file = str(args.get("compose_file", "docker-compose.yml"))
        services = args.get("services")
        risk_factors.append(
            f"Starting services from compose file '{compose_file}'."
        )
        recommendations.append(
            "Review the compose file for volume mounts, privileged flags, and exposed ports "
            "before starting."
        )
        if services:
            summary = f"Start {len(services)} service(s) from '{compose_file}'"
        else:
            summary = f"Start all services from '{compose_file}'"

    # --- docker_compose_down ---
    elif tool_name == "docker_compose_down":
        compose_file = str(args.get("compose_file", "docker-compose.yml"))
        risk_factors.append(
            f"Stopping and removing services from compose file '{compose_file}'."
        )
        recommendations.append(
            "Verify no critical work is in progress on the compose stack before tearing down."
        )
        summary = f"Tear down compose stack from '{compose_file}'"

    # --- Unknown docker tool ---
    else:
        summary = f"Docker operation: {tool_name}"
        risk_factors.append(
            f"Unknown docker tool '{tool_name}' — risk profile not fully assessed."
        )

    return {
        "summary": summary,
        "risk_factors": risk_factors,
        "recommendations": recommendations,
    }


_DEFAULT_TIMEOUT = 600


def _quote(value: str) -> str:
    """Shell-quote a user-supplied value to prevent injection."""
    return shlex.quote(str(value))


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

# Docker image reference: [registry/]name[:tag][@digest]
_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$")

# Docker container name: [a-zA-Z0-9][a-zA-Z0-9_.-]+
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")

# Port mapping: [host_ip:]host_port:container_port[/protocol]
_PORT_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")


def _validate_image_name(image: str) -> None:
    """Validate that the image name is non-empty and well-formed.

    Raises ValueError on invalid input.
    """
    if not image or not image.strip():
        raise ValueError(
            "Docker container 'image' parameter must be a non-empty string."
        )
    image = image.strip()
    if not _IMAGE_RE.match(image):
        raise ValueError(
            f"Docker image name '{image}' contains invalid characters. "
            f"Allowed: alphanumeric, '.', '/', ':', '-', '_', '@'."
        )


def _validate_container_name(name: str) -> None:
    """Validate that the container name follows Docker naming rules.

    Raises ValueError on invalid input. Only validated when a name is
    explicitly provided (empty string is allowed — Docker auto-generates
    a name).
    """
    if not name:
        return
    name = name.strip()
    if not _CONTAINER_NAME_RE.match(name):
        raise ValueError(
            f"Docker container name '{name}' is invalid. "
            f"Names must start with [a-zA-Z0-9] and may contain "
            f"[a-zA-Z0-9_.-] thereafter."
        )


def _validate_port_mapping(port: str) -> None:
    """Validate that a port mapping string is well-formed.

    Raises ValueError on invalid input.
    """
    if not port or not port.strip():
        raise ValueError(
            "Docker port mapping must be a non-empty string (e.g. '8080:80')."
        )
    port = port.strip()
    if not _PORT_RE.match(port):
        raise ValueError(
            f"Docker port mapping '{port}' contains invalid characters. "
            f"Expected format: [host_ip:]host_port:container_port[/protocol]."
        )


def _validate_container_id(container: str) -> None:
    """Validate that the container identifier is non-empty and well-formed.

    Raises ValueError on invalid input. Accepts both container names and
    container IDs (hex strings).
    """
    if not container or not container.strip():
        raise ValueError(
            "Docker 'container' parameter must be a non-empty string "
            "(container name or ID)."
        )
    container = container.strip()
    # Container IDs are hex strings; container names follow Docker naming rules.
    # Accept both forms plus abbreviated IDs.
    if not (_CONTAINER_NAME_RE.match(container) or re.match(r"^[a-fA-F0-9]+$", container)):
        raise ValueError(
            f"Docker container identifier '{container}' is invalid. "
            f"Must be a valid container name or hex container ID."
        )


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

    Parameter validation: image name, container name, port mappings, and
    command are validated before command assembly to ensure correct format
    and prevent injection.

    Security: validates volume mounts against the whitelist and runs a
    semantic security check on the assembled command before execution.
    Raises ValueError on invalid parameters, DockerSecurityViolation on
    policy violations.
    """
    # --- Parameter validation ---
    _validate_image_name(image)
    _validate_container_name(name)
    for p in ports or []:
        _validate_port_mapping(p)

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
        # Split and re-quote each token to prevent shell injection while
        # preserving the argument structure for the container entrypoint.
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(
                f"Docker container 'command' parameter could not be parsed: {exc}"
            ) from exc
        cmd += " " + " ".join(shlex.quote(p) for p in parts)

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
    """Stop a running Docker container.

    Raises ValueError if the container identifier is empty or invalid.
    """
    _validate_container_id(container)
    cmd = f"docker container stop {_quote(container)}"
    return await shell_exec(cmd, timeout=60, task=task)


async def docker_container_rm(
    container: str,
    force: bool = False,
    task: Any | None = None,
) -> dict[str, Any]:
    """Remove a Docker container.

    Raises ValueError if the container identifier is empty or invalid.
    """
    _validate_container_id(container)
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
