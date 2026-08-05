"""一次性补丁脚本：为 BudgetConfig 增加角色预算上限配置。"""
from pathlib import Path

path = Path(r"e:\Jerry_python\OpenOPC\opc\core\config.py")
text = path.read_text(encoding="utf-8")

# 1) hard_stop 字段后新增 role_limits_usd
old1 = "    hard_stop: bool = False  # 超限後是否硬停止（False=降級到廉价模型繼續）\n"
new1 = (
    "    hard_stop: bool = False  # 超限後是否硬停止（False=降級到廉价模型繼續）\n"
    "    role_limits_usd: dict[str, float] = Field(default_factory=dict)  # 按角色預算上限（美元，0/缺省=不限制）\n"
)
assert text.count(old1) == 1, f"old1 count={text.count(old1)}"
text = text.replace(old1, new1)

# 2) 移除先前误加的多余空行
old2 = "        return limits.get(level, 0.0)\n\n\n    def should_warn"
new2 = "        return limits.get(level, 0.0)\n\n    def should_warn"
assert text.count(old2) == 1, f"old2 count={text.count(old2)}"
text = text.replace(old2, new2)

# 3) is_exceeded 之后新增 get_role_limit / has_any_limit
old3 = (
    "    def is_exceeded(self, level: str, spent: float) -> bool:\n"
    "        \"\"\"檢查是否超過預算。\"\"\"\n"
    "        limit = self.get_effective_limit(level)\n"
    "        if limit <= 0:\n"
    "            return False\n"
    "        return spent >= limit\n"
)
new3 = old3 + (
    "\n"
    "    def get_role_limit(self, role: str) -> float:\n"
    "        \"\"\"取得指定角色的預算上限（美元，0 表示不限制）。\"\"\"\n"
    "        return float(self.role_limits_usd.get(role, 0.0) or 0.0)\n"
    "\n"
    "    def has_any_limit(self) -> bool:\n"
    "        \"\"\"檢查是否配置了任何預算限制（層級或角色）。\"\"\"\n"
    "        return (\n"
    "            self.task_limit_usd > 0\n"
    "            or self.session_limit_usd > 0\n"
    "            or self.monthly_limit_usd > 0\n"
    "            or any(v > 0 for v in self.role_limits_usd.values())\n"
    "        )\n"
)
assert text.count(old3) == 1, f"old3 count={text.count(old3)}"
text = text.replace(old3, new3)

# 4) docstring 使用范例补充 role_limits_usd
old4 = "          hard_stop: false         # 超限後降級而非停止\n    \"\"\"\n"
new4 = (
    "          hard_stop: false         # 超限後降級而非停止\n"
    "          role_limits_usd:         # 按角色預算上限（可選）\n"
    "            researcher: 20.0\n"
    "    \"\"\"\n"
)
assert text.count(old4) == 1, f"old4 count={text.count(old4)}"
text = text.replace(old4, new4)

path.write_text(text, encoding="utf-8")
print("patched OK")
