import pathlib
p = pathlib.Path('opc/layer4_tools/docker_ops.py')
t = p.read_text(encoding='utf-8')
t = t.replace("chr(61)", "'='")
p.write_text(t, encoding='utf-8')
print('fixed')
