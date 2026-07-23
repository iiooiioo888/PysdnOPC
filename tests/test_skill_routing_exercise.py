"""Exercise the collaboration-playbook skill routing in a simulated company-mode session.

Validates that the skill candidate has complete trigger + procedure + output +
validation routing, and exercises it under realistic company-mode conditions.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from opc.layer5_memory.skill_library import Skill, SkillLibrary


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_PATH = REPO_ROOT / ".opc" / "skills" / "collaboration-playbook" / "SKILL.md"


class CollaborationPlaybookRoutingExercise(unittest.TestCase):
    """Exercise the collaboration-playbook complete routing in a company-mode session."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.playbook_text = PLAYBOOK_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # 1. Trigger conditions are present and well-formed
    # ------------------------------------------------------------------

    def test_trigger_section_present(self) -> None:
        """The skill has an explicit Trigger Conditions section."""
        self.assertIn("### Trigger Conditions", self.playbook_text)
        self.assertIn("execution_mode == \"company_mode\"", self.playbook_text)
        self.assertIn("role has at least one active work item", self.playbook_text)

    def test_trigger_frontmatter_present(self) -> None:
        """Frontmatter contains structured trigger metadata."""
        self.assertIn("trigger:", self.playbook_text)
        self.assertIn("conditions:", self.playbook_text)
        self.assertIn("priority: high", self.playbook_text)
        self.assertIn("frequency: every company-mode turn", self.playbook_text)

    # ------------------------------------------------------------------
    # 2. Procedure steps are present and ordered
    # ------------------------------------------------------------------

    def test_procedure_steps_present(self) -> None:
        """The skill has numbered Procedure Steps."""
        self.assertIn("### Procedure Steps", self.playbook_text)
        self.assertIn("1. **Read inbox**", self.playbook_text)
        self.assertIn("2. **Load upstream handoffs**", self.playbook_text)
        self.assertIn("3. **Assess coordination need**", self.playbook_text)
        self.assertIn("4. **Execute work-item slice**", self.playbook_text)
        self.assertIn("5. **Coordinate if blocked**", self.playbook_text)
        self.assertIn("6. **Leave handoff artifact**", self.playbook_text)
        self.assertIn("7. **Update shared memory**", self.playbook_text)

    # ------------------------------------------------------------------
    # 3. Output format is specified
    # ------------------------------------------------------------------

    def test_output_format_present(self) -> None:
        """The skill defines a structured Output Format."""
        self.assertIn("### Output Format", self.playbook_text)
        self.assertIn("**Status**:", self.playbook_text)
        self.assertIn("**Summary**:", self.playbook_text)
        self.assertIn("**Artifacts**:", self.playbook_text)
        self.assertIn("**Decisions made**:", self.playbook_text)
        self.assertIn("**Verification**:", self.playbook_text)

    # ------------------------------------------------------------------
    # 4. Validation criteria are present
    # ------------------------------------------------------------------

    def test_validation_criteria_present(self) -> None:
        """The skill defines explicit Validation Criteria."""
        self.assertIn("### Validation Criteria", self.playbook_text)
        self.assertIn("**Inbox processed**", self.playbook_text)
        self.assertIn("**Scope respected**", self.playbook_text)
        self.assertIn("**Handoff complete**", self.playbook_text)
        self.assertIn("**Messages justified**", self.playbook_text)
        self.assertIn("**Memory updated**", self.playbook_text)
        self.assertIn("**No orphan blocks**", self.playbook_text)

    # ------------------------------------------------------------------
    # 5. Exercise: skill loads and injects in a simulated company-mode session
    # ------------------------------------------------------------------

    def test_skill_exercised_in_company_mode_session(self) -> None:
        """Simulate a company-mode session: skill is loaded, triggered, and produces output."""
        # Load the real skill library from .opc/
        library = SkillLibrary(REPO_ROOT / ".opc")
        library.load_all()

        # Verify skill is present
        skill = library.get("collaboration-playbook")
        self.assertIsNotNone(skill, "collaboration-playbook must exist in skill library")
        assert skill is not None

        # Verify trigger metadata parsed from frontmatter
        self.assertTrue(skill.always, "Skill must be always-on")
        self.assertEqual(skill.modes, ["company_mode"])

        # Exercise: build skills summary as the runtime would in company_mode
        company_summary = library.build_skills_summary(execution_mode="company_mode")

        # The skill body is injected (always: true in company_mode)
        self.assertIn("## Skill: collaboration-playbook", company_summary)
        self.assertIn("### Trigger Conditions", company_summary)
        self.assertIn("### Procedure Steps", company_summary)
        self.assertIn("### Output Format", company_summary)
        self.assertIn("### Validation Criteria", company_summary)

        # Verify the skill is NOT visible in task_mode (mode filtering works)
        task_summary = library.build_skills_summary(execution_mode="task_mode")
        self.assertNotIn("collaboration-playbook", task_summary)

        # Simulate a role receiving the skill in a company-mode turn:
        # The routing tells the agent to check inbox, load handoffs, execute, leave handoff.
        # We verify the complete routing chain is present in the injected content.
        routing_chain = [
            "Read inbox",
            "Load upstream handoffs",
            "Assess coordination need",
            "Execute work-item slice",
            "Coordinate if blocked",
            "Leave handoff artifact",
            "Update shared memory",
        ]
        for step in routing_chain:
            self.assertIn(step, company_summary, f"Routing step missing: {step}")

    def test_skill_routing_produces_handoff_template(self) -> None:
        """The output format section provides a usable handoff template."""
        library = SkillLibrary(REPO_ROOT / ".opc")
        library.load_all()
        company_summary = library.build_skills_summary(execution_mode="company_mode")

        # The handoff template fields are all present for a role to fill in
        handoff_fields = [
            "**Status**: completed | blocked | partial",
            "**Summary**:",
            "**Artifacts**:",
            "**Decisions made**:",
            "**Risks / Open questions**:",
            "**Verification**:",
            "**Downstream notes**:",
        ]
        for field in handoff_fields:
            self.assertIn(field, company_summary, f"Handoff field missing: {field}")


if __name__ == "__main__":
    unittest.main()
