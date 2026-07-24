from __future__ import annotations

from pathlib import Path
import unittest

from opc.core.config import OPCConfig
from opc.core.models import AgentInfo, Task
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer2_organization.talent_market import resolve_prompt_refs
from opc.layer3_agent.native_agent import PromptProfileManager


def _make_pm(*, role_prompts: list[str]) -> PromptProfileManager:
    role = AgentInfo(
        role_id="cmo",
        name="CMO",
        responsibility="Marketing strategy and brand oversight.",
        prompt_refs=role_prompts,
    )
    return PromptProfileManager(role=role, config=OPCConfig())


def _make_assembler() -> ContextAssembler:
    return ContextAssembler(memory=None, store=None, communication=None)


def _base_prompt(prompt: str) -> str:
    addendum_headers = [
        "\n\n## Task Tracking",
        "\n\n## 公司工作項目契約",
        "\n\n## 組織運行時契約",
        "\n\n## 工作項目輪次：報告產出",
        "\n\n## 看板審查輪次",
        "\n\n## 審查要求",
        "\n\n## 任務模式編排",
        "\n\n## 角色操作指令",
        "\n\n## 運行時設定檔覆寫",
    ]
    indexes = [prompt.find(header) for header in addendum_headers if prompt.find(header) >= 0]
    base = prompt[:min(indexes)] if indexes else prompt
    return base.rstrip()


def _base_prompt_contract(prompt: str) -> str:
    prompt = _base_prompt(prompt)
    marker = "\n\n## 核心運作原則"
    index = prompt.find(marker)
    return prompt[index:].strip() if index >= 0 else _base_prompt(prompt)


class RoleOperatingInstructionsSectionTest(unittest.TestCase):
    def test_role_prompt_appears_under_role_operating_instructions_section(self) -> None:
        pm = _make_pm(role_prompts=["Optimize for audience fit and brand consistency."])
        task = Task(title="Plan launch")
        _, prompt = pm.build_prompt(task)
        self.assertIn(
            "## 角色操作指令\nOptimize for audience fit and brand consistency.",
            prompt,
        )

    def test_multiple_role_prompts_joined_with_blank_line(self) -> None:
        pm = _make_pm(role_prompts=["First directive.", "Second directive."])
        task = Task(title="Plan launch")
        _, prompt = pm.build_prompt(task)
        self.assertIn(
            "## 角色操作指令\nFirst directive.\n\nSecond directive.",
            prompt,
        )

    def test_empty_role_prompts_omits_section(self) -> None:
        pm = _make_pm(role_prompts=[])
        task = Task(title="Plan launch")
        self.assertNotIn("## 角色操作指令", pm.build_prompt(task)[1])


class UnifiedNativePromptTest(unittest.TestCase):
    def test_prompt_profile_metadata_no_longer_changes_base_contract(self) -> None:
        pm = _make_pm(role_prompts=[])
        cases = [
            Task(title="Plan launch", metadata={"prompt_profile": "plan"}),
            Task(title="Review launch", metadata={"prompt_profile": "review"}),
            Task(title="Verify launch", metadata={"subagent_profile": "verify"}),
            Task(title="Draft launch", metadata={"_subagent_mode": "plan"}),
            Task(title="Execute launch", metadata={"prompt_profile": "coding"}),
        ]

        base_prompt = ""
        for task in cases:
            profile, prompt = pm.build_prompt(task)
            self.assertEqual(profile, "unified")
            self.assertIn("## 核心運作原則", prompt)
            self.assertNotIn("## Core Beliefs", prompt)
            self.assertNotIn("because it's worth it", prompt)
            self.assertNotIn("out of love for completeness", prompt)
            self.assertIn("## 原生工作契約", prompt)
            self.assertNotIn("## Plan Profile", prompt)
            self.assertNotIn("## Planning Principles", prompt)
            self.assertNotIn("## Review Principles", prompt)
            self.assertNotIn("## Verification Contract", prompt)
            current_base = _base_prompt(prompt)
            if not base_prompt:
                base_prompt = current_base
            self.assertEqual(current_base, base_prompt)

    def test_company_and_task_mode_share_unified_base_with_context_addenda(self) -> None:
        pm = _make_pm(role_prompts=[])
        _, task_prompt = pm.build_prompt(
            Task(title="Task turn", metadata={"mode": "task", "execution_mode": "task_mode"})
        )
        _, company_prompt = pm.build_prompt(
            Task(
                title="Review turn",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_turn_type": "review",
                    "work_item_projection_title": "Review turn",
                },
            )
        )

        self.assertNotEqual(_base_prompt(task_prompt).split("\n\n", 1)[0], _base_prompt(company_prompt).split("\n\n", 1)[0])
        self.assertEqual(_base_prompt_contract(task_prompt), _base_prompt_contract(company_prompt))
        self.assertIn("## 任務模式編排", task_prompt)
        self.assertNotIn("## 任務模式編排", company_prompt)
        self.assertIn("## 公司工作項目契約", company_prompt)

    def test_task_generalist_role_prompt_refs_are_not_injected_as_persona(self) -> None:
        role = AgentInfo(
            role_id="task_generalist",
            name="Task Generalist",
            responsibility="Primary session agent for task mode.",
            prompt_refs=["Legacy task-mode persona text."],
        )
        pm = PromptProfileManager(role=role, config=OPCConfig())
        _, prompt = pm.build_prompt(
            Task(title="Task turn", metadata={"mode": "task", "execution_mode": "task_mode"})
        )

        self.assertIn("## 任務模式編排", prompt)
        self.assertIn("公司組織、招募流程、員工角色", prompt)
        self.assertNotIn("## 角色操作指令", prompt)
        self.assertNotIn("Legacy task-mode persona text.", prompt)


class PersonaSectionTest(unittest.TestCase):
    def test_employee_prompt_context_appears_under_persona_subsection(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="Plan launch",
            metadata={
                "employee_prompt_context": "I focus on emotional engagement and growth metrics.",
            },
        )
        block = ca._build_self_section(task)
        self.assertIn("## Self", block)
        self.assertIn(
            "### Employee Persona\nI focus on emotional engagement and growth metrics.",
            block,
        )

    def test_empty_employee_prompt_context_skips_persona_subsection(self) -> None:
        ca = _make_assembler()
        task = Task(title="Plan launch", metadata={"employee_prompt_context": ""})
        self.assertNotIn("### Employee Persona", ca._build_self_section(task))

    def test_persona_renders_alongside_role_and_employee_when_assignment_present(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="Plan launch",
            metadata={
                "employee_assignment": {
                    "name": "Sarah",
                    "employee_id": "cmo-sarah",
                    "role_id": "cmo",
                    "category": "marketing",
                    "domains": ["growth"],
                    "experience_score": 0,
                },
                "employee_prompt_context": "I love clean code.",
            },
        )
        block = ca._build_self_section(task)
        self.assertEqual(block.count("## Self\n"), 1)
        self.assertIn("### Role", block)
        self.assertIn("- Role: cmo", block)
        self.assertIn("### Employee", block)
        self.assertIn("- Employee: Sarah", block)
        self.assertIn("### Employee Persona\nI love clean code.", block)

    def test_self_role_uses_current_seat_name_and_responsibility(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="CEO intake",
            assigned_to="ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::ceo",
                            "role_id": "ceo",
                            "metadata": {
                                "role_name": "CEO",
                                "responsibility": "Own final delivery and coordinate direct reports.",
                            },
                        }
                    ]
                },
                "employee_assignment": {
                    "name": "CEO Fallback Empty Employee",
                    "employee_id": "ceo-fallback-empty-employee",
                    "role_id": "ceo",
                    "category": "fallback",
                    "domains": [],
                    "experience_score": 0.0,
                },
            },
        )
        block = ca._build_self_section(task)
        self.assertIn("### Role", block)
        self.assertIn("- Role: ceo (CEO)", block)
        self.assertIn("- Responsibility: Own final delivery and coordinate direct reports.", block)
        self.assertIn("### Employee", block)
        self.assertIn("- Assignment: fallback employee profile", block)
        self.assertNotIn("- Domains:", block)
        self.assertNotIn("- Experience score:", block)


class PromptRefResolutionTest(unittest.TestCase):
    def test_literal_multiline_prompt_is_not_treated_as_path(self) -> None:
        prompt = (
            "You are a Principal Investigator.\n"
            "Leads research direction, formulates hypotheses, oversees publications, "
            "and mentors researchers.\n"
            "Working style: See what others miss, ask what others won't.\n"
            "Domains of expertise: research-direction, hypothesis, publication."
        )
        self.assertEqual(resolve_prompt_refs([prompt], Path(".opc")), [prompt])


class DualChannelCoexistenceTest(unittest.TestCase):
    def test_real_hire_populates_both_role_and_persona_sections(self) -> None:
        pm = _make_pm(
            role_prompts=["Optimize for audience fit and brand consistency."],
        )
        task = Task(
            title="Plan launch",
            metadata={
                "employee_prompt_context": "I focus on emotional engagement.",
            },
        )
        _, role_prompt = pm.build_prompt(task)

        ca = _make_assembler()
        self_block = ca._build_self_section(task)

        self.assertIn(
            "## 角色操作指令\nOptimize for audience fit and brand consistency.",
            role_prompt,
        )
        self.assertIn("### Employee Persona\nI focus on emotional engagement.", self_block)
        self.assertNotIn("I focus on emotional engagement", role_prompt)
        self.assertNotIn("Optimize for audience fit", self_block)


if __name__ == "__main__":
    unittest.main()
