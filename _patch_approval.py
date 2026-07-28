# -*- coding: utf-8 -*-
import pathlib

p = pathlib.Path("opc/layer2_organization/approval.py")
lines = p.read_text(encoding="utf-8").splitlines(True)

# Find the line: '            return "; ".join(part for part in parts if not part.endswith("="))'
target = '            return "; ".join(part for part in parts if not part.endswith("="))
'
idx = None
for i, line in enumerate(lines):
    if line == target:
        idx = i
        break

assert idx is not None, "target line not found"

new_lines = [
    '            summary = "; ".join(part for part in parts if not part.endswith("="))\n',
    '            # Append Docker-specific risk factors for detailed approval display\n',
    '            risk_factors = metadata.get("risk_factors")\n',
    '            if risk_factors and isinstance(risk_factors, list):\n',
    '                summary += "\\n  Risk factors:\\n"\n',
    '                for factor in risk_factors:\n',
    '                    summary += f"    - {factor}\\n"\n',
    '            recommendations = metadata.get("recommendations")\n',
    '            if recommendations and isinstance(recommendations, list):\n',
    '                summary += "  Recommendations:\\n"\n',
    '                for rec in recommendations:\n',
    '                    summary += f"    - {rec}\\n"\n',
    '            return summary\n',
]

lines[idx:idx+1] = new_lines
p.write_text("".join(lines), encoding="utf-8")
print("OK - patched approval.py")
