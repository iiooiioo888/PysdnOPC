"""配置安全掃描器 — 靈感來自 ECC AgentShield。

職責說明：
    對 OpenOPC 的配置檔案、已匯入技能、代理設定進行靜態安全分析，
    偵測潛在的密鑰洩露、權限過度、注入風險等安全問題。

關聯關係：
    - 被 opc/cli/app.py 的 security-scan 命令呼叫
    - 掃描目標：.opc/config/、.opc/skills/、.opc/prompts/
    - 純靜態分析，不依賴外部服務

使用範例：
    scanner = SecurityScanner(opc_home)
    report = scanner.scan_all()
    print(scanner.generate_report(report, format="terminal"))
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# 掃描嚴重等級
SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_INFO = "info"

# 掃描類別
CATEGORY_SECRETS = "secrets_detection"
CATEGORY_PERMISSIONS = "permission_auditing"
CATEGORY_HOOK_INJECTION = "hook_injection"
CATEGORY_SKILL_CONTENT = "skill_content_review"
CATEGORY_AGENT_CONFIG = "agent_config_review"


@dataclass
class ScanFinding:
    """單個掃描發現。"""

    category: str
    severity: str
    title: str
    description: str
    file_path: str = ""
    line_number: int = 0
    recommendation: str = ""


@dataclass
class ScanReport:
    """完整掃描報告。"""

    findings: list[ScanFinding] = field(default_factory=list)
    files_scanned: int = 0
    categories_scanned: list[str] = field(default_factory=list)
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def grade(self) -> str:
        """計算安全等級評分 (A-F)。"""
        if self.critical_count > 0:
            return "F"
        if self.high_count >= 3:
            return "D"
        if self.high_count >= 1:
            return "C"
        if self.medium_count >= 3:
            return "B"
        if self.medium_count >= 1 or self.low_count >= 3:
            return "B"
        return "A"


# 密鑰偵測正則模式
_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"][a-zA-Z0-9_\-]{16,}['\"]", "Hardcoded API key"),
    (r"(?i)(secret|token|password|passwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]", "Hardcoded secret/token"),
    (r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{20,}", "Bearer token in config"),
    (r"(?i)sk-[a-zA-Z0-9]{20,}", "OpenAI-style API key"),
    (r"(?i)ghp_[a-zA-Z0-9]{36}", "GitHub personal access token"),
    (r"(?i)AKIA[0-9A-Z]{16}", "AWS access key ID"),
    (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "Private key material"),
]

# 危險 shell 命令模式
_DANGEROUS_COMMANDS: list[tuple[str, str]] = [
    (r"\brm\s+-rf\s+/", "Recursive force delete from root"),
    (r"\bcurl\b.*\|\s*(ba)?sh", "Remote code execution via pipe"),
    (r"\bwget\b.*\|\s*(ba)?sh", "Remote code execution via pipe"),
    (r"\beval\s*\(", "Eval execution"),
    (r"\bexec\s*\(", "Exec execution"),
    (r"\.\./\.\./\.\./", "Path traversal pattern"),
    (r"\bchmod\s+777", "World-writable permission"),
    (r"\bsudo\b", "Privilege escalation"),
]


class SecurityScanner:
    """配置安全掃描器。

    掃描五大類別：
        1. Secrets detection — 偵測配置中的硬編碼密鑰/token
        2. Permission auditing — 審批模式是否過於寬鬆
        3. Hook injection analysis — 鉤子命令是否含注入風險
        4. Skill content review — 技能文件中的可疑 shell 命令
        5. Agent config review — 外部代理配置安全性
    """

    def __init__(self, opc_home: Path) -> None:
        self.opc_home = Path(opc_home)
        self.config_dir = self.opc_home / "config"
        self.skills_dir = self.opc_home / "skills"
        self.prompts_dir = self.opc_home / "prompts"

    def scan_all(self) -> ScanReport:
        """執行全面安全掃描。"""
        report = ScanReport()
        self._scan_config(report)
        self._scan_skills(report)
        self._scan_agents(report)
        self._scan_hooks(report)
        self._tally(report)
        logger.info(
            f"SecurityScanner: {report.total_findings} findings "
            f"(grade: {report.grade}) across {report.files_scanned} files"
        )
        return report

    def scan_config(self) -> ScanReport:
        """僅掃描配置目錄。"""
        report = ScanReport()
        self._scan_config(report)
        self._tally(report)
        return report

    def scan_skills(self) -> ScanReport:
        """僅掃描技能目錄。"""
        report = ScanReport()
        self._scan_skills(report)
        self._tally(report)
        return report

    def scan_agents(self) -> ScanReport:
        """僅掃描代理配置。"""
        report = ScanReport()
        self._scan_agents(report)
        self._tally(report)
        return report

    def generate_report(self, report: ScanReport, format: str = "terminal") -> str:
        """生成掃描報告。

        Args:
            report: 掃描報告物件。
            format: 輸出格式 ("terminal" | "json" | "markdown")。

        Returns:
            str — 格式化的報告文字。
        """
        if format == "json":
            return self._format_json(report)
        elif format == "markdown":
            return self._format_markdown(report)
        return self._format_terminal(report)

    # ------------------------------------------------------------------
    # Scan implementations
    # ------------------------------------------------------------------

    def _scan_config(self, report: ScanReport) -> None:
        """掃描 .opc/config/ 下的所有設定檔。"""
        report.categories_scanned.append(CATEGORY_SECRETS)
        report.categories_scanned.append(CATEGORY_PERMISSIONS)

        if not self.config_dir.is_dir():
            return

        for config_file in self.config_dir.rglob("*.yaml"):
            report.files_scanned += 1
            self._check_secrets_in_file(config_file, report)
            self._check_permissions_in_config(config_file, report)

        for config_file in self.config_dir.rglob("*.json"):
            report.files_scanned += 1
            self._check_secrets_in_file(config_file, report)

    def _scan_skills(self, report: ScanReport) -> None:
        """掃描已匯入技能中的潛在注入風險。"""
        report.categories_scanned.append(CATEGORY_SKILL_CONTENT)

        if not self.skills_dir.is_dir():
            return

        for skill_md in self.skills_dir.rglob("SKILL.md"):
            report.files_scanned += 1
            self._check_skill_content(skill_md, report)

    def _scan_agents(self, report: ScanReport) -> None:
        """掃描代理配置的安全性。"""
        report.categories_scanned.append(CATEGORY_AGENT_CONFIG)

        # Check agent_config.yaml
        agent_config = self.config_dir / "agent_config.yaml"
        if agent_config.exists():
            report.files_scanned += 1
            self._check_agent_config(agent_config, report)

        # Check prompt files for injection
        if self.prompts_dir.is_dir():
            for prompt_file in self.prompts_dir.rglob("*.md"):
                report.files_scanned += 1
                self._check_prompt_injection(prompt_file, report)

    def _scan_hooks(self, report: ScanReport) -> None:
        """掃描鉤子配置的注入風險。"""
        report.categories_scanned.append(CATEGORY_HOOK_INJECTION)

        hooks_config = self.config_dir / "hooks.json"
        if not hooks_config.exists():
            return

        report.files_scanned += 1
        try:
            data = json.loads(hooks_config.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        for hook in data.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command", ""))
            hook_id = str(hook.get("id", "unknown"))
            if command:
                self._check_command_injection(
                    command, f"hooks.json (hook: {hook_id})", report
                )

    # ------------------------------------------------------------------
    # Check helpers
    # ------------------------------------------------------------------

    def _check_secrets_in_file(self, file_path: Path, report: ScanReport) -> None:
        """偵測文件中的硬編碼密鑰。"""
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            return

        for line_num, line in enumerate(content.split("\n"), 1):
            # Skip comments and examples
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if "example" in line.lower() or "your_" in line.lower():
                continue
            if "${" in line or "{{" in line:
                continue  # Template variable, not hardcoded

            for pattern, description in _SECRET_PATTERNS:
                if re.search(pattern, line):
                    report.findings.append(ScanFinding(
                        category=CATEGORY_SECRETS,
                        severity=SEVERITY_CRITICAL,
                        title=f"Potential secret: {description}",
                        description=f"Found in {file_path.name} line {line_num}",
                        file_path=str(file_path),
                        line_number=line_num,
                        recommendation="Move to environment variable or .env file",
                    ))
                    break  # One finding per line

    def _check_permissions_in_config(self, file_path: Path, report: ScanReport) -> None:
        """檢查審批模式是否過於寬鬆。"""
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            return

        if "approval_mode" in content:
            for line_num, line in enumerate(content.split("\n"), 1):
                if "approval_mode" in line and "full-auto" in line:
                    report.findings.append(ScanFinding(
                        category=CATEGORY_PERMISSIONS,
                        severity=SEVERITY_MEDIUM,
                        title="Full-auto approval mode detected",
                        description=f"{file_path.name} line {line_num}: full-auto bypasses all safety checks",
                        file_path=str(file_path),
                        line_number=line_num,
                        recommendation="Consider using 'auto' mode with shell_safety rules",
                    ))

    def _check_skill_content(self, skill_md: Path, report: ScanReport) -> None:
        """檢查技能文件中的可疑命令。"""
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            return

        for line_num, line in enumerate(content.split("\n"), 1):
            for pattern, description in _DANGEROUS_COMMANDS:
                if re.search(pattern, line):
                    report.findings.append(ScanFinding(
                        category=CATEGORY_SKILL_CONTENT,
                        severity=SEVERITY_HIGH,
                        title=f"Dangerous command in skill: {description}",
                        description=f"{skill_md.parent.name}/SKILL.md line {line_num}",
                        file_path=str(skill_md),
                        line_number=line_num,
                        recommendation="Review and sanitize the command",
                    ))
                    break

    def _check_agent_config(self, config_path: Path, report: ScanReport) -> None:
        """檢查外部代理配置安全性。"""
        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError:
            return

        for line_num, line in enumerate(content.split("\n"), 1):
            # Check for full-auto approval
            if "approval_mode" in line and "full-auto" in line:
                report.findings.append(ScanFinding(
                    category=CATEGORY_AGENT_CONFIG,
                    severity=SEVERITY_MEDIUM,
                    title="External agent with full-auto approval",
                    description=f"agent_config.yaml line {line_num}",
                    file_path=str(config_path),
                    line_number=line_num,
                    recommendation="Use 'auto' mode for better safety",
                ))
            # Check for empty command (potential injection vector)
            if re.match(r"\s*command:\s*$", line):
                report.findings.append(ScanFinding(
                    category=CATEGORY_AGENT_CONFIG,
                    severity=SEVERITY_LOW,
                    title="Empty agent command field",
                    description=f"agent_config.yaml line {line_num}: empty command may cause fallback issues",
                    file_path=str(config_path),
                    line_number=line_num,
                    recommendation="Specify explicit command path",
                ))

    def _check_prompt_injection(self, prompt_file: Path, report: ScanReport) -> None:
        """檢查 prompt 文件中的注入風險。"""
        try:
            content = prompt_file.read_text(encoding="utf-8")
        except OSError:
            return

        injection_patterns = [
            (r"(?i)ignore\s+(all\s+)?previous\s+instructions", "Prompt injection: ignore instructions"),
            (r"(?i)you\s+are\s+now\s+", "Prompt injection: role override"),
            (r"(?i)system\s*:\s*", "Prompt injection: fake system message"),
            (r"(?i)<\|im_start\|>", "Prompt injection: ChatML injection"),
        ]

        for line_num, line in enumerate(content.split("\n"), 1):
            for pattern, description in injection_patterns:
                if re.search(pattern, line):
                    report.findings.append(ScanFinding(
                        category=CATEGORY_AGENT_CONFIG,
                        severity=SEVERITY_HIGH,
                        title=description,
                        description=f"{prompt_file.name} line {line_num}",
                        file_path=str(prompt_file),
                        line_number=line_num,
                        recommendation="Review prompt content for injection attempts",
                    ))
                    break

    def _check_command_injection(self, command: str, source: str, report: ScanReport) -> None:
        """檢查命令中的注入風險。"""
        injection_indicators = [
            (r"\$\(", "Command substitution"),
            (r"`[^`]+`", "Backtick execution"),
            (r";\s*(rm|curl|wget|nc)\b", "Chained dangerous command"),
            (r"\|\s*(ba)?sh", "Pipe to shell"),
        ]
        for pattern, description in injection_indicators:
            if re.search(pattern, command):
                report.findings.append(ScanFinding(
                    category=CATEGORY_HOOK_INJECTION,
                    severity=SEVERITY_HIGH,
                    title=f"Hook injection risk: {description}",
                    description=f"In {source}",
                    recommendation="Sanitize hook command, avoid dynamic execution",
                ))
                break

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    def _tally(self, report: ScanReport) -> None:
        """統計各嚴重等級的數量。"""
        for f in report.findings:
            if f.severity == SEVERITY_CRITICAL:
                report.critical_count += 1
            elif f.severity == SEVERITY_HIGH:
                report.high_count += 1
            elif f.severity == SEVERITY_MEDIUM:
                report.medium_count += 1
            elif f.severity == SEVERITY_LOW:
                report.low_count += 1

    def _format_terminal(self, report: ScanReport) -> str:
        """終端彩色輸出格式。"""
        lines = [
            f"Security Scan Report — Grade: {report.grade}",
            f"Files scanned: {report.files_scanned}",
            f"Findings: {report.total_findings} "
            f"(C:{report.critical_count} H:{report.high_count} "
            f"M:{report.medium_count} L:{report.low_count})",
            "",
        ]
        for f in report.findings:
            icon = {"critical": "!!", "high": "! ", "medium": "* ", "low": "  "}.get(f.severity, "  ")
            lines.append(f"  [{icon}] [{f.severity.upper()}] {f.title}")
            lines.append(f"       {f.description}")
            if f.recommendation:
                lines.append(f"       -> {f.recommendation}")
            lines.append("")
        if not report.findings:
            lines.append("  No security issues found.")
        return "\n".join(lines)

    def _format_json(self, report: ScanReport) -> str:
        """JSON 輸出格式（供 CI 管線使用）。"""
        data = {
            "grade": report.grade,
            "files_scanned": report.files_scanned,
            "total_findings": report.total_findings,
            "critical": report.critical_count,
            "high": report.high_count,
            "medium": report.medium_count,
            "low": report.low_count,
            "findings": [
                {
                    "category": f.category,
                    "severity": f.severity,
                    "title": f.title,
                    "description": f.description,
                    "file_path": f.file_path,
                    "line_number": f.line_number,
                    "recommendation": f.recommendation,
                }
                for f in report.findings
            ],
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _format_markdown(self, report: ScanReport) -> str:
        """Markdown 輸出格式。"""
        lines = [
            f"# Security Scan Report",
            f"",
            f"**Grade: {report.grade}** | Files: {report.files_scanned} | "
            f"Findings: {report.total_findings}",
            f"",
            f"| Severity | Count |",
            f"|----------|-------|",
            f"| Critical | {report.critical_count} |",
            f"| High | {report.high_count} |",
            f"| Medium | {report.medium_count} |",
            f"| Low | {report.low_count} |",
            f"",
            f"## Findings",
            f"",
        ]
        for f in report.findings:
            lines.append(f"### [{f.severity.upper()}] {f.title}")
            lines.append(f"- **Category**: {f.category}")
            lines.append(f"- **Location**: {f.file_path}:{f.line_number}")
            lines.append(f"- **Description**: {f.description}")
            if f.recommendation:
                lines.append(f"- **Recommendation**: {f.recommendation}")
            lines.append("")
        if not report.findings:
            lines.append("No security issues found.")
        return "\n".join(lines)
