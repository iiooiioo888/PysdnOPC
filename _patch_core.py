# -*- coding: utf-8 -*-
import pathlib

p = pathlib.Path("opc/engine/_core.py")
lines = p.read_text(encoding="utf-8").splitlines(True)
insert_at = 1211  # 0-based index: after "        }" closing metadata dict
new_lines = [
    "        # --- Docker-specific risk assessment for detailed approval prompts ---\n",
    '        if tool.name.startswith("docker_"):\n',
    "            from opc.layer4_tools.docker_ops import assess_docker_risk\n",
    "            docker_risk = assess_docker_risk(tool.name, arguments)\n",
    '            metadata["docker_risk_assessment"] = docker_risk\n',
    '            metadata["risk_summary"] = docker_risk["summary"]\n',
    '            metadata["risk_factors"] = docker_risk["risk_factors"]\n',
    '            if docker_risk["recommendations"]:\n',
    '                metadata["recommendations"] = docker_risk["recommendations"]\n',
]
lines[insert_at:insert_at] = new_lines
p.write_text("".join(lines), encoding="utf-8")
print("OK - patched _core.py")
