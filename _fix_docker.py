import pathlib

p = pathlib.Path("opc/layer4_tools/docker_ops.py")
content = p.read_text(encoding="utf-8")

# Fix 1: docker_image_build docstring
old1 = '    """Build a Docker image from a Dockerfile."""'
new1 = '    """Build a Docker image from a Dockerfile.\n\n    Security: validates the assembled command against policy before execution.\n    """'
assert old1 in content, "old1 not found"
content = content.replace(old1, new1, 1)

# Fix 1b: add check before return in docker_image_build
old1b = "    return await shell_exec(cmd, working_directory=working_directory, timeout=_DEFAULT_TIMEOUT, task=task)\n\n\nasync def docker_image_pull("
new1b = "    check_docker_command_security(cmd)\n    return await shell_exec(cmd, working_directory=working_directory, timeout=_DEFAULT_TIMEOUT, task=task)\n\n\nasync def docker_image_pull("
assert old1b in content, "old1b not found"
content = content.replace(old1b, new1b, 1)

# Fix 2: docker_container_run docstring + volume validation
old2 = '    """Run a Docker container."""\n    cmd = "docker run"'
new2 = '    """Run a Docker container.\n\n    Security: validates volume mounts against the whitelist and runs a\n    semantic security check on the assembled command before execution.\n    Raises DockerSecurityViolation on policy violations.\n    """\n    # --- Security: validate volume mounts before building the command ---\n    for v in volumes or []:\n        validate_volume_mount(v)\n\n    cmd = "docker run"'
assert old2 in content, "old2 not found"
content = content.replace(old2, new2, 1)

# Fix 2b: add check before return in docker_container_run
old2b = '    if command:\n        cmd += f" {command}"\n    return await shell_exec(cmd, timeout=_DEFAULT_TIMEOUT, task=task)\n\n\nasync def docker_container_ls('
new2b = '    if command:\n        cmd += f" {command}"\n\n    # --- Security: final semantic check on the assembled command ---\n    check_docker_command_security(cmd)\n\n    return await shell_exec(cmd, timeout=_DEFAULT_TIMEOUT, task=task)\n\n\nasync def docker_container_ls('
assert old2b in content, "old2b not found"
content = content.replace(old2b, new2b, 1)

p.write_text(content, encoding="utf-8")
print("All fixes applied successfully!")
