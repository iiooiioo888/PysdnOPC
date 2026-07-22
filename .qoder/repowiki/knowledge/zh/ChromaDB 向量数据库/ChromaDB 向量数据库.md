---
kind: external_dependency
name: ChromaDB 向量数据库
slug: chromadb
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### ChromaDB
- 角色：OpenOPC 的本地向量存储后端，用于语义检索、记忆压缩与技能索引。
- 集成点：作为 `opc/database/store.py` 的可选后端之一，配合 Markdown 记忆系统实现向量化检索。