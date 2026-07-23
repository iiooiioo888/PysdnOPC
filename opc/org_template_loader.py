"""組織模板載入器 — 從模板庫載入並應用組織配置。

職責說明：
    掃描 org_templates 目錄，載入 YAML 模板，
    並將其轉換為 OPC 引擎可用的組織配置。

使用範例：
    from opc.engine.org_template_loader import OrgTemplateLoader
    loader = OrgTemplateLoader()
    templates = loader.list_templates()
    org_config = loader.load_template("finance/research_report")
    loader.apply_to_engine(engine, org_config)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger


# 模板搜索路徑
_TEMPLATE_SEARCH_PATHS = [
    Path(__file__).parent.parent / "config" / "org_templates",  # 項目級
    Path.home() / ".opc" / "org_templates",  # 用戶級
]


class OrgTemplateLoader:
    """組織模板載入器。"""

    def __init__(self, extra_paths: list[Path] | None = None) -> None:
        self._search_paths = list(_TEMPLATE_SEARCH_PATHS)
        if extra_paths:
            self._search_paths.extend(extra_paths)
        self._cache: dict[str, dict[str, Any]] = {}

    def list_templates(self) -> list[dict[str, str]]:
        """列出所有可用模板。

        返回：
            list[dict] — 每個包含 id, name, description, path
        """
        templates = []
        seen_ids = set()

        for search_path in self._search_paths:
            if not search_path.is_dir():
                continue
            for yaml_file in search_path.rglob("*.yaml"):
                try:
                    with open(yaml_file, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if not isinstance(data, dict):
                        continue

                    # 計算相對 ID
                    rel = yaml_file.relative_to(search_path)
                    template_id = str(rel.with_suffix("")).replace("\\", "/")

                    if template_id in seen_ids:
                        continue
                    seen_ids.add(template_id)

                    templates.append({
                        "id": template_id,
                        "name": data.get("name", template_id.split("/")[-1]),
                        "description": data.get("description", ""),
                        "path": str(yaml_file),
                        "roles": len(data.get("roles", [])),
                    })
                except Exception as e:
                    logger.debug(f"Skipping template {yaml_file}: {e}")

        return sorted(templates, key=lambda t: t["id"])

    def load_template(self, template_id: str) -> dict[str, Any] | None:
        """載入指定模板。

        參數：
            template_id: 模板 ID（如 "finance/research_report"）

        返回：
            dict — 模板配置數據，未找到返回 None
        """
        if template_id in self._cache:
            return self._cache[template_id]

        for search_path in self._search_paths:
            yaml_path = search_path / f"{template_id}.yaml"
            if yaml_path.exists():
                try:
                    with open(yaml_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if isinstance(data, dict):
                        self._cache[template_id] = data
                        logger.info(f"Loaded org template: {template_id}")
                        return data
                except Exception as e:
                    logger.error(f"Failed to load template {template_id}: {e}")

        logger.warning(f"Template not found: {template_id}")
        return None

    def apply_to_engine(
        self,
        engine: Any,
        template_id: str,
        organization_id: str | None = None,
    ) -> bool:
        """將模板應用到引擎。

        參數：
            engine: OPCEngine 實例
            template_id: 模板 ID
            organization_id: 組織 ID（None 時從模板推斷）

        返回：
            bool — 是否成功應用
        """
        template = self.load_template(template_id)
        if not template:
            return False

        org_id = organization_id or template.get("organization_id", template_id.split("/")[-1])

        try:
            from opc.core.config import write_company_org_payload, get_opc_home

            config_dir = get_opc_home() / "config"
            config_dir.mkdir(parents=True, exist_ok=True)

            # 轉換模板為引擎組織配置格式
            org_payload = self._template_to_org_payload(template, org_id)

            # 寫入配置
            write_company_org_payload(config_dir, org_id, org_payload)

            logger.info(f"Applied org template '{template_id}' as organization '{org_id}'")
            return True

        except Exception as e:
            logger.error(f"Failed to apply template {template_id}: {e}")
            return False

    def _template_to_org_payload(
        self, template: dict[str, Any], org_id: str
    ) -> dict[str, Any]:
        """將模板轉換為引擎組織配置格式。"""
        payload: dict[str, Any] = {
            "organization_id": org_id,
            "kind": "opc_org_architecture",
            "schema_version": 2,
            "company": template.get("company", {"name": org_id}),
            "roles": [],
            "employees": [],
            "talent_templates": [],
            "runtime_policies": {},
        }

        # 轉換角色
        for role_def in template.get("roles", []):
            role = {
                "id": role_def.get("id", ""),
                "name": role_def.get("name", role_def.get("id", "")),
                "description": role_def.get("description", ""),
                "responsibilities": role_def.get("responsibilities", []),
            }

            # 添加運行時策略
            if "runtime_policy" in role_def:
                role["runtime_policy"] = role_def["runtime_policy"]
            if "tools" in role_def:
                role["tools"] = role_def["tools"]
            if "model_tier" in role_def:
                role["model_tier"] = role_def["model_tier"]

            payload["roles"].append(role)

        # 轉換人才模板
        for talent_def in template.get("talent_templates", []):
            talent = {
                "id": talent_def.get("id", ""),
                "name": talent_def.get("name", ""),
                "system_prompt": talent_def.get("system_prompt", ""),
            }
            payload["talent_templates"].append(talent)

        return payload

    def get_template_info(self, template_id: str) -> dict[str, Any] | None:
        """獲取模板詳細信息（不載入完整配置）。"""
        template = self.load_template(template_id)
        if not template:
            return None

        return {
            "id": template_id,
            "name": template.get("name", ""),
            "description": template.get("description", ""),
            "roles": [
                {
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "description": r.get("description", ""),
                    "model_tier": r.get("model_tier", "medium"),
                }
                for r in template.get("roles", [])
            ],
            "talent_templates": len(template.get("talent_templates", [])),
        }

    def search_templates(self, query: str) -> list[dict[str, str]]:
        """搜索模板。

        參數：
            query: 搜索關鍵詞

        返回：
            list[dict] — 匹配的模板列表
        """
        query_lower = query.lower()
        all_templates = self.list_templates()

        results = []
        for t in all_templates:
            score = 0
            if query_lower in t["id"].lower():
                score += 3
            if query_lower in t["name"].lower():
                score += 2
            if query_lower in t["description"].lower():
                score += 1
            if score > 0:
                results.append({**t, "score": score})

        return sorted(results, key=lambda x: -x.get("score", 0))


def format_template_list(templates: list[dict[str, str]]) -> str:
    """格式化模板列表為人類可讀文本。"""
    if not templates:
        return "📭 暫無可用的組織模板"

    lines = ["🏗️ 可用組織模板：\n"]

    # 按目錄分組
    by_dir: dict[str, list[dict[str, str]]] = {}
    for t in templates:
        dir_name = t["id"].split("/")[0] if "/" in t["id"] else "other"
        by_dir.setdefault(dir_name, []).append(t)

    for dir_name, dir_templates in sorted(by_dir.items()):
        lines.append(f"  📁 {dir_name}/")
        for t in dir_templates:
            name = t["name"]
            desc = t["description"][:50] if t["description"] else ""
            roles = t.get("roles", "0")
            lines.append(f"    📄 {t['id']:<35} {name:<20} ({roles} roles)")
            if desc:
                lines.append(f"       {desc}")

    return "\n".join(lines)
