import pathlib
p = pathlib.Path('opc/layer4_tools/docker_ops.py')
t = p.read_text('utf-8')
t = t.replace(
    'func=docker_image_ls,\n            category="infrastructure",\n            requires_confirmation=True,\n            read_only=True,',
    'func=docker_image_ls,\n            category="infrastructure",\n            requires_confirmation=False,\n            read_only=True,',
    1
)
t = t.replace(
    'func=docker_container_ls,\n            category="infrastructure",\n            requires_confirmation=True,\n            read_only=True,',
    'func=docker_container_ls,\n            category="infrastructure",\n            requires_confirmation=False,\n            read_only=True,',
    1
)
p.write_text(t, 'utf-8')
print('done')
