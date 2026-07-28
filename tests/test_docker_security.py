"""Tests for Docker semantic security policy and shell_safety docker classification."""

from __future__ import annotations

import unittest

from opc.layer2_organization.shell_safety import is_read_only_shell_command
from opc.layer4_tools.docker_ops import (
    ALLOWED_CAPABILITIES,
    DEFAULT_VOLUME_WHITELIST,
    HIGH_RISK_SUBCOMMANDS,
    DockerSecurityViolation,
    check_docker_command_security,
    is_high_risk_docker_command,
    validate_volume_mount,
)


class DockerShellSafetyClassificationTests(unittest.TestCase):
    """Test docker subcommand read-only classification in shell_safety."""

    def _assert_safe(self, command: str) -> None:
        safe, reason = is_read_only_shell_command(command, [])
        self.assertTrue(safe, f"{command!r} should be safe: {reason}")

    def _assert_unsafe(self, command: str) -> None:
        safe, _ = is_read_only_shell_command(command, [])
        self.assertFalse(safe, f"{command!r} should NOT be safe")

    def test_read_only_docker_commands(self) -> None:
        for command in (
            "docker ps", "docker ps -a", "docker images", "docker inspect mycontainer",
            "docker logs mycontainer", "docker info", "docker version", "docker stats",
            "docker top mycontainer", "docker port mycontainer", "docker diff mycontainer",
            "docker history nginx", "docker search nginx", "docker events",
            "docker container ls", "docker image ls", "docker volume ls",
            "docker network ls", "docker system df", "docker system info",
        ):
            self._assert_safe(command)

    def test_mutating_docker_commands_not_read_only(self) -> None:
        for command in (
            "docker run ubuntu", "docker build .", "docker pull nginx",
            "docker push myimage", "docker rm mycontainer", "docker rmi myimage",
            "docker stop mycontainer", "docker start mycontainer",
            "docker exec -it mycontainer bash", "docker compose up", "docker compose down",
        ):
            self._assert_unsafe(command)

    def test_high_risk_docker_commands_not_read_only(self) -> None:
        for command in (
            "docker system prune", "docker system prune -a", "docker volume rm myvolume",
            "docker volume prune", "docker image prune", "docker container prune",
            "docker network prune", "docker builder prune",
        ):
            self._assert_unsafe(command)

    def test_docker_with_global_options(self) -> None:
        self._assert_safe("docker --host tcp://localhost:2375 ps")
        self._assert_safe("docker -D ps")
        self._assert_unsafe("docker --host tcp://localhost:2375 run ubuntu")


class DockerSecurityPolicyTests(unittest.TestCase):
    """Test the semantic security policy in docker_ops."""

    def test_privileged_flag_blocked(self) -> None:
        with self.assertRaises(DockerSecurityViolation) as ctx:
            check_docker_command_security("docker run --privileged ubuntu")
        self.assertIn("--privileged", str(ctx.exception))

    def test_privileged_flag_with_value_blocked(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --privileged=true ubuntu")

    def test_cap_add_restricted(self) -> None:
        with self.assertRaises(DockerSecurityViolation) as ctx:
            check_docker_command_security("docker run --cap-add SYS_ADMIN ubuntu")
        self.assertIn("SYS_ADMIN", str(ctx.exception))

    def test_cap_add_equals_form_restricted(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --cap-add=SYS_ADMIN ubuntu")

    def test_cap_add_allowed_capabilities(self) -> None:
        for cap in ALLOWED_CAPABILITIES:
            check_docker_command_security(f"docker run --cap-add {cap} ubuntu")
            check_docker_command_security(f"docker run --cap-add={cap} ubuntu")

    def test_volume_whitelist_blocks_unauthorized_paths(self) -> None:
        with self.assertRaises(DockerSecurityViolation) as ctx:
            check_docker_command_security("docker run -v /etc/passwd:/data ubuntu")
        self.assertIn("/etc/passwd", str(ctx.exception))

    def test_volume_whitelist_blocks_root(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run -v /:/host ubuntu")

    def test_volume_whitelist_allows_tmp(self) -> None:
        check_docker_command_security("docker run -v /tmp/data:/data ubuntu")
        check_docker_command_security("docker run -v /tmp:/tmp ubuntu")

    def test_volume_whitelist_allows_var_tmp(self) -> None:
        check_docker_command_security("docker run -v /var/tmp/cache:/cache ubuntu")

    def test_volume_long_form(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --volume /etc/shadow:/data ubuntu")
        check_docker_command_security("docker run --volume /tmp/data:/data ubuntu")

    def test_volume_equals_form(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --volume=/etc/shadow:/data ubuntu")

    def test_named_volumes_allowed(self) -> None:
        check_docker_command_security("docker run -v myvolume:/data ubuntu")
        check_docker_command_security("docker run -v my-volume:/data ubuntu")

    def test_custom_whitelist(self) -> None:
        custom = ["/home/user", "/opt/data"]
        check_docker_command_security("docker run -v /home/user/project:/project ubuntu", volume_whitelist=custom)
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run -v /tmp/data:/data ubuntu", volume_whitelist=custom)

    def test_unparseable_command_fails_closed(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run 'unclosed ubuntu")


class DockerHighRiskDetectionTests(unittest.TestCase):
    """Test high-risk docker command detection."""

    def test_high_risk_commands_detected(self) -> None:
        for subcmd in HIGH_RISK_SUBCOMMANDS:
            self.assertTrue(is_high_risk_docker_command(f"docker {subcmd}"))
            self.assertTrue(is_high_risk_docker_command(f"docker {subcmd} -f"))

    def test_safe_commands_not_high_risk(self) -> None:
        for command in ("docker ps", "docker images", "docker run ubuntu", "docker build .", "docker pull nginx", "docker compose up"):
            self.assertFalse(is_high_risk_docker_command(command))


class DockerVolumeValidationTests(unittest.TestCase):
    """Test volume mount validation edge cases."""

    def test_windows_path_handling(self) -> None:
        with self.assertRaises(DockerSecurityViolation):
            validate_volume_mount("C:\\Users\\secret:/data", ["/tmp"])

    def test_subpath_allowed(self) -> None:
        validate_volume_mount("/tmp/subdir/file.txt:/data", ["/tmp"])
        validate_volume_mount("/var/tmp/cache:/cache", ["/var/tmp"])



class DockerToolRegistrationTests(unittest.TestCase):
    """Test docker tool registration, schema, and metadata."""

    def test_create_docker_tools_returns_all_expected_tools(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        tools = create_docker_tools()
        names = {t.name for t in tools}
        expected = {
            "docker_image_build", "docker_image_pull", "docker_image_ls",
            "docker_container_run", "docker_container_ls",
            "docker_container_stop", "docker_container_rm",
            "docker_compose_up", "docker_compose_down",
        }
        self.assertEqual(names, expected)

    def test_all_tools_have_infrastructure_category(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        for tool in create_docker_tools():
            self.assertEqual(tool.category, "infrastructure", f"{tool.name} category")

    def test_all_tools_require_confirmation(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        read_only_names = {"docker_container_ls", "docker_image_ls"}
        for tool in create_docker_tools():
            if tool.name in read_only_names:
                self.assertFalse(tool.requires_confirmation, f"{tool.name} should NOT require confirmation")
            else:
                self.assertTrue(tool.requires_confirmation, f"{tool.name} should require confirmation")

    def test_read_only_tools_marked(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        tools = {t.name: t for t in create_docker_tools()}
        self.assertTrue(tools["docker_container_ls"].read_only)
        self.assertTrue(tools["docker_image_ls"].read_only)
        # Mutating tools should NOT be read_only
        self.assertIsNone(tools["docker_container_run"].read_only)
        self.assertIsNone(tools["docker_image_build"].read_only)

    def test_tool_schemas_parseable(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        for tool in create_docker_tools():
            schema = tool.to_schema()
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("properties", params)

    def test_registry_registration_and_lookup(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        from opc.layer4_tools.registry import ToolRegistry
        registry = ToolRegistry()
        for tool in create_docker_tools():
            registry.register(tool)
        # Verify lookup
        self.assertIsNotNone(registry.get("docker_container_run"))
        self.assertIsNotNone(registry.get("docker_image_build"))
        # Verify category filter
        infra_tools = registry.list_tools(category="infrastructure")
        self.assertEqual(len(infra_tools), 9)

    def test_registry_schemas_for_agent(self) -> None:
        from opc.layer4_tools.docker_ops import create_docker_tools
        from opc.layer4_tools.registry import ToolRegistry
        registry = ToolRegistry()
        for tool in create_docker_tools():
            registry.register(tool)
        schemas = registry.get_schemas()
        self.assertEqual(len(schemas), 9)
        for schema in schemas:
            self.assertIsInstance(schema["name"], str)
            self.assertIsInstance(schema["parameters"], dict)


class DockerToolSecurityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Test that structured docker tools enforce security policies."""

    async def test_container_run_rejects_unauthorized_volume(self) -> None:
        from opc.layer4_tools.docker_ops import DockerSecurityViolation, docker_container_run
        with self.assertRaises(DockerSecurityViolation):
            await docker_container_run(image="ubuntu", volumes=["/etc/passwd:/data"])

    async def test_container_run_rejects_root_volume(self) -> None:
        from opc.layer4_tools.docker_ops import DockerSecurityViolation, docker_container_run
        with self.assertRaises(DockerSecurityViolation):
            await docker_container_run(image="ubuntu", volumes=["/:/host"])

    async def test_container_run_allows_tmp_volume(self) -> None:
        """Allowed volume should pass validation (will fail at shell_exec since no docker)."""
        from opc.layer4_tools.docker_ops import docker_container_run
        # This will pass security but fail at shell_exec (no docker daemon in test)
        # We just verify no DockerSecurityViolation is raised
        try:
            await docker_container_run(image="ubuntu", volumes=["/tmp/data:/data"])
        except DockerSecurityViolation:
            self.fail("DockerSecurityViolation should not be raised for /tmp volume")
        except Exception:
            pass  # shell_exec failure is expected in test environment

    async def test_container_run_allows_named_volume(self) -> None:
        from opc.layer4_tools.docker_ops import docker_container_run
        try:
            await docker_container_run(image="ubuntu", volumes=["myvolume:/data"])
        except DockerSecurityViolation:
            self.fail("DockerSecurityViolation should not be raised for named volume")
        except Exception:
            pass  # shell_exec failure is expected

    async def test_approval_callback_blocks_docker_tool(self) -> None:
        """Verify approval callback can intercept docker tool execution."""
        from opc.layer4_tools.docker_ops import create_docker_tools
        from opc.layer4_tools.registry import ToolRegistry
        from types import SimpleNamespace

        registry = ToolRegistry()
        for tool in create_docker_tools():
            registry.register(tool)

        # Simulate a deny-all approval callback
        async def deny_all(tool_def, arguments, task, on_progress):
            decision = SimpleNamespace(
                action=SimpleNamespace(value="deny"),
                risk_level=SimpleNamespace(value="high"),
                confidence=1.0,
                policy_source="test",
                rationale="Blocked by test policy",
                metadata={},
            )
            return False, decision

        registry.set_approval_callback(deny_all)
        result = await registry.execute("docker_container_ls", {})
        self.assertFalse(result["success"])
        self.assertIn("blocked", result["error"].lower())

    async def test_approval_callback_allows_read_only(self) -> None:
        """Verify approval callback can allow read-only docker tools."""
        from opc.layer4_tools.docker_ops import create_docker_tools
        from opc.layer4_tools.registry import ToolRegistry
        from types import SimpleNamespace

        registry = ToolRegistry()
        for tool in create_docker_tools():
            registry.register(tool)

        # Simulate an allow-all approval callback
        async def allow_all(tool_def, arguments, task, on_progress):
            decision = SimpleNamespace(
                action=SimpleNamespace(value="allow"),
                risk_level=SimpleNamespace(value="low"),
                confidence=1.0,
                policy_source="test",
                rationale="Allowed by test policy",
                metadata={},
            )
            return True, decision

        registry.set_approval_callback(allow_all)
        # docker_image_ls is read-only, will pass approval but fail at shell_exec
        result = await registry.execute("docker_image_ls", {})
        # Should not be blocked by approval (may fail at shell level)
        if not result["success"]:
            self.assertNotIn("blocked", result.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
