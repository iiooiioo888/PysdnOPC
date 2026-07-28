import pathlib
content = '''        ToolDefinition(
            name="docker_compose_up",
            description="Start services defined in a Docker Compose file.",
            parameters={
                "type": "object",
                "properties": {
                    "compose_file": {"type": "string", "description": "Path to compose file", "default": "docker-compose.yml"},
                    "services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific services to start (empty = all)",
                    },
                    "detach": {"type": "boolean", "description": "Run in detached mode", "default": True},
                    "working_directory": {"type": "string", "description": "Working directory for the command"},
                },
                "required": [],
            },
            func=docker_compose_up,
            category="infrastructure",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="docker_compose_down",
            description="Stop and remove services defined in a Docker Compose file.",
            parameters={
                "type": "object",
                "properties": {
                    "compose_file": {"type": "string", "description": "Path to compose file", "default": "docker-compose.yml"},
                    "working_directory": {"type": "string", "description": "Working directory for the command"},
                },
                "required": [],
            },
            func=docker_compose_down,
            category="infrastructure",
            requires_confirmation=True,
        ),
    ]
'''
p = pathlib.Path('opc/layer4_tools/docker_ops.py')
p.write_text(p.read_text(encoding='utf-8') + content, encoding='utf-8')
print('part7 done')
