import pathlib

p = pathlib.Path("tests/test_docker_security.py")
content = p.read_text(encoding="utf-8")

# Add new test classes at the end (before the __main__ block)
new_tests = '''

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
        for tool in create_docker_tools():
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

'''

# Insert before the __main__ block
main_block = '\nif __name__ == "__main__":\n    unittest.main()\n'
assert main_block in content, "main block not found"
content = content.replace(main_block, new_tests + main_block)

p.write_text(content, encoding="utf-8")
print("Integration tests added successfully!")
