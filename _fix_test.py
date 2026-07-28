import pathlib
p = pathlib.Path('tests/test_docker_security.py')
t = p.read_text('utf-8')

old = (
    '    def test_all_tools_require_confirmation(self) -> None:\n'
    '        from opc.layer4_tools.docker_ops import create_docker_tools\n'
    '        for tool in create_docker_tools():\n'
    '            self.assertTrue(tool.requires_confirmation, f"{tool.name} should require confirmation")'
)
new = (
    '    def test_all_tools_require_confirmation(self) -> None:\n'
    '        from opc.layer4_tools.docker_ops import create_docker_tools\n'
    '        read_only_names = {"docker_container_ls", "docker_image_ls"}\n'
    '        for tool in create_docker_tools():\n'
    '            if tool.name in read_only_names:\n'
    '                self.assertFalse(tool.requires_confirmation, f"{tool.name} should NOT require confirmation")\n'
    '            else:\n'
    '                self.assertTrue(tool.requires_confirmation, f"{tool.name} should require confirmation")'
)
assert old in t, 'pattern not found'
t = t.replace(old, new, 1)
p.write_text(t, 'utf-8')
print('done')
