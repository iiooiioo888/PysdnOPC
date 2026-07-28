import pathlib
p = pathlib.Path('opc/engine/_core.py')
t = p.read_text(encoding='utf-8')
# Remove duplicate import
dup_import = 'from opc.layer4_tools.docker_ops import create_docker_tools  # Docker \u64cd\u4f5c\u5de5\u5177\nfrom opc.layer4_tools.docker_ops import create_docker_tools  # Docker \u64cd\u4f5c\u5de5\u5177'
single_import = 'from opc.layer4_tools.docker_ops import create_docker_tools  # Docker \u64cd\u4f5c\u5de5\u5177'
t = t.replace(dup_import, single_import)
# Remove duplicate registration
dup_reg = '''        for tool in create_docker_tools():
            self.tool_registry.register(tool)
        for tool in create_docker_tools():
            self.tool_registry.register(tool)'''
single_reg = '''        for tool in create_docker_tools():
            self.tool_registry.register(tool)'''
t = t.replace(dup_reg, single_reg)
p.write_text(t, encoding='utf-8')
print('duplicates removed')
