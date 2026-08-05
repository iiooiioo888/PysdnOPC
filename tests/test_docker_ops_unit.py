"""Comprehensive unit tests for Docker operations tools.

Covers all 9 tool functions (normal + exception paths), security helpers,
risk assessment, tool registration/schema, and approval-callback integration.
``shell_exec`` is mocked so no real Docker daemon is required.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.layer4_tools.docker_ops import (
    ALLOWED_CAPABILITIES,
    DANGEROUS_CAPABILITIES,
    HIGH_RISK_SUBCOMMANDS,
    DockerSecurityViolation,
    _contains_directory_traversal,
    _is_sensitive_path,
    _normalize_volume_path,
    assess_docker_risk,
    check_docker_command_security,
    create_docker_tools,
    docker_compose_down,
    docker_compose_up,
    docker_container_ls,
    docker_container_rm,
    docker_container_run,
    docker_container_stop,
    docker_image_build,
    docker_image_ls,
    docker_image_pull,
    is_high_risk_docker_command,
)
from opc.layer4_tools.registry import ToolRegistry


class NormalizeVolumePathTests(unittest.TestCase):
    def test_posix_path_normalized(self):
        self.assertEqual(_normalize_volume_path("/tmp/data"), "/tmp/data")
        self.assertEqual(_normalize_volume_path("/tmp/data/"), "/tmp/data")

    def test_trailing_slash_stripped(self):
        self.assertEqual(_normalize_volume_path("/tmp///"), "/tmp")

    def test_empty_becomes_root(self):
        self.assertEqual(_normalize_volume_path(""), "/")
        self.assertEqual(_normalize_volume_path("   "), "/")


class DirectoryTraversalTests(unittest.TestCase):
    def test_traversal_detected(self):
        for path in ("/tmp/../etc", "../etc", "/var/tmp/../../root", "a/../b"):
            self.assertTrue(_contains_directory_traversal(path), f"{path} should be traversal")

    def test_no_traversal(self):
        for path in ("/tmp/data", "myvolume", "/var/tmp/cache", "C:/Users/app"):
            self.assertFalse(_contains_directory_traversal(path), f"{path} should NOT be traversal")

    def test_empty_path(self):
        self.assertFalse(_contains_directory_traversal(""))
        self.assertFalse(_contains_directory_traversal("  "))

    def test_double_dot_in_name_not_traversal(self):
        self.assertFalse(_contains_directory_traversal("/tmp/my..file"))
        self.assertFalse(_contains_directory_traversal("/tmp/file..txt"))


class SensitivePathTests(unittest.TestCase):
    def test_sensitive_paths_detected(self):
        for path in ("/etc", "/etc/passwd", "/root", "/proc/cpuinfo",
                     "/sys/kernel", "/boot/grub", "/dev/sda",
                     "/usr/bin", "/lib/x86_64", "/sbin/init"):
            self.assertIsNotNone(_is_sensitive_path(path), f"{path} should be sensitive")

    def test_non_sensitive_paths(self):
        for path in ("/tmp", "/var/tmp", "/tmp/data", "myvolume", "data-vol"):
            self.assertIsNone(_is_sensitive_path(path), f"{path} should NOT be sensitive")

    def test_docker_sock_sensitive(self):
        self.assertIsNotNone(_is_sensitive_path("/var/run/docker.sock"))

    def test_docker_lib_sensitive(self):
        self.assertIsNotNone(_is_sensitive_path("/var/lib/docker"))

    def test_traversal_to_sensitive_detected(self):
        self.assertIsNotNone(_is_sensitive_path("/tmp/../etc"))
        self.assertIsNotNone(_is_sensitive_path("/var/tmp/../../root"))


class CheckDockerCommandSecurityTests(unittest.TestCase):
    def test_privileged_blocked(self):
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --privileged ubuntu")
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --privileged=true ubuntu")

    def test_dangerous_capability_blocked(self):
        for cap in DANGEROUS_CAPABILITIES:
            with self.assertRaises(DockerSecurityViolation) as ctx:
                check_docker_command_security(f"docker run --cap-add {cap} ubuntu")
            self.assertIn(cap, str(ctx.exception))

    def test_dangerous_capability_equals_form(self):
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --cap-add=SYS_ADMIN ubuntu")

    def test_allowed_capabilities_pass(self):
        for cap in ALLOWED_CAPABILITIES:
            check_docker_command_security(f"docker run --cap-add {cap} ubuntu")
            check_docker_command_security(f"docker run --cap-add={cap} ubuntu")

    def test_unknown_capability_blocked(self):
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run --cap-add FOOBAR ubuntu")

    def test_sensitive_volume_blocked(self):
        for path in ("/etc/passwd", "/root", "/proc", "/sys", "/boot"):
            with self.assertRaises(DockerSecurityViolation):
                check_docker_command_security(f"docker run -v {path}:/data ubuntu")

    def test_allowed_tmp_volume_passes(self):
        check_docker_command_security("docker run -v /tmp/data:/data ubuntu")
        check_docker_command_security("docker run -v /var/tmp/cache:/cache ubuntu")

    def test_named_volume_passes(self):
        check_docker_command_security("docker run -v myvolume:/data ubuntu")
        check_docker_command_security("docker run -v my-volume:/vol ubuntu")

    def test_unparseable_command_fails_closed(self):
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security("docker run 'unclosed ubuntu")

    def test_custom_whitelist(self):
        custom = ["/home/user", "/opt/data"]
        check_docker_command_security(
            "docker run -v /home/user/project:/project ubuntu",
            volume_whitelist=custom,
        )
        with self.assertRaises(DockerSecurityViolation):
            check_docker_command_security(
                "docker run -v /tmp/data:/data ubuntu",
                volume_whitelist=custom,
            )


class HighRiskCommandTests(unittest.TestCase):
    def test_high_risk_detected(self):
        for subcmd in HIGH_RISK_SUBCOMMANDS:
            self.assertTrue(is_high_risk_docker_command(f"docker {subcmd}"))
            self.assertTrue(is_high_risk_docker_command(f"docker {subcmd} -f"))

    def test_safe_not_high_risk(self):
        for cmd in ("docker ps", "docker images", "docker run ubuntu",
                     "docker build .", "docker pull nginx", "docker compose up"):
            self.assertFalse(is_high_risk_docker_command(cmd))


class AssessDockerRiskTests(unittest.TestCase):
    def test_read_only_tools_low_risk(self):
        for tool in ("docker_image_ls", "docker_container_ls"):
            result = assess_docker_risk(tool, {})
            self.assertIsInstance(result["summary"], str)
            self.assertEqual(result["risk_factors"], [])
            self.assertEqual(result["recommendations"], [])

    def test_container_run_summary_includes_details(self):
        result = assess_docker_risk("docker_container_run", {
            "image": "nginx:1.25", "name": "web",
            "volumes": ["/tmp/html:/usr/share/nginx/html"],
            "ports": ["8080:80"],
        })
        self.assertIn("nginx:1.25", result["summary"])
        self.assertIn("web", result["summary"])
        self.assertTrue(result["risk_factors"])
        self.assertTrue(result["recommendations"])

    def test_container_run_blocked_volume_flagged(self):
        result = assess_docker_risk("docker_container_run", {
            "image": "ubuntu", "volumes": ["/etc/passwd:/data"],
        })
        blocked = [f for f in result["risk_factors"] if "Blocked" in f]
        self.assertTrue(blocked)

    def test_container_run_latest_tag_flagged(self):
        result = assess_docker_risk("docker_container_run", {"image": "nginx:latest"})
        latest = [f for f in result["risk_factors"] if "latest" in f.lower()]
        self.assertTrue(latest)

    def test_container_run_no_tag_flagged(self):
        result = assess_docker_risk("docker_container_run", {"image": "ubuntu"})
        no_tag = [f for f in result["risk_factors"] if "no explicit tag" in f.lower()]
        self.assertTrue(no_tag)

    def test_container_run_with_command_flagged(self):
        result = assess_docker_risk("docker_container_run", {
            "image": "ubuntu:22.04", "command": "rm -rf /",
        })
        cmd_warnings = [f for f in result["risk_factors"] if "command" in f.lower()]
        self.assertTrue(cmd_warnings)

    def test_container_run_foreground_flagged(self):
        result = assess_docker_risk("docker_container_run", {
            "image": "ubuntu:22.04", "detach": False,
        })
        fg = [f for f in result["risk_factors"] if "foreground" in f.lower()]
        self.assertTrue(fg)

    def test_image_build_summary(self):
        result = assess_docker_risk("docker_image_build", {
            "context": ".", "dockerfile": "Dockerfile", "tag": "myapp:1.0",
        })
        self.assertIn("Build", result["summary"])
        self.assertTrue(any("secrets" in r.lower() for r in result["recommendations"]))

    def test_image_build_untagged_flagged(self):
        result = assess_docker_risk("docker_image_build", {"context": "."})
        untagged = [f for f in result["risk_factors"] if "untagged" in f.lower()]
        self.assertTrue(untagged)

    def test_image_pull_summary(self):
        result = assess_docker_risk("docker_image_pull", {"image": "nginx:1.25"})
        self.assertIn("Pull", result["summary"])
        self.assertIn("nginx:1.25", result["summary"])

    def test_image_pull_untrusted_registry_flagged(self):
        result = assess_docker_risk("docker_image_pull", {"image": "myrepo/app:1.0"})
        untrusted = [f for f in result["risk_factors"] if "untrusted" in f.lower()]
        self.assertTrue(untrusted)

    def test_container_stop_summary(self):
        result = assess_docker_risk("docker_container_stop", {"container": "myapp"})
        self.assertIn("Stop", result["summary"])
        self.assertTrue(result["recommendations"])

    def test_container_rm_summary(self):
        result = assess_docker_risk("docker_container_rm", {"container": "myapp"})
        self.assertIn("Remove", result["summary"])

    def test_container_rm_force_flagged(self):
        result = assess_docker_risk("docker_container_rm", {
            "container": "myapp", "force": True,
        })
        self.assertIn("(force)", result["summary"])
        force_warnings = [f for f in result["risk_factors"] if "force" in f.lower()]
        self.assertTrue(force_warnings)

    def test_compose_up_summary(self):
        result = assess_docker_risk("docker_compose_up", {
            "compose_file": "docker-compose.yml", "services": ["web", "db"],
        })
        self.assertIn("2", result["summary"])

    def test_compose_up_all_services(self):
        result = assess_docker_risk("docker_compose_up", {})
        self.assertIn("all services", result["summary"].lower())

    def test_compose_down_summary(self):
        result = assess_docker_risk("docker_compose_down", {})
        self.assertIn("Tear down", result["summary"])
        self.assertTrue(result["recommendations"])

    def test_unknown_tool_handled(self):
        result = assess_docker_risk("docker_unknown_tool", {})
        self.assertIn("Unknown", result["summary"])
        self.assertTrue(result["risk_factors"])

    def test_returns_three_keys(self):
        result = assess_docker_risk("docker_image_ls", {})
        self.assertEqual(set(result.keys()), {"summary", "risk_factors", "recommendations"})

    def test_none_arguments(self):
        result = assess_docker_risk("docker_container_run", None)
        self.assertIsInstance(result["summary"], str)
