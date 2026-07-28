import pathlib
p = pathlib.Path('opc/layer4_tools/docker_ops.py')
t = p.read_text('utf-8')

old = (
    'SENSITIVE_PATH_BLACKLIST: tuple[str, ...] = (\n'
    '    "/etc",\n'
    '    "/root",\n'
    '    "/proc",\n'
    '    "/sys",\n'
    '    "/boot",\n'
    '    "/dev",\n'
    '    "/var/run/docker.sock",\n'
    '    "/var/lib/docker",\n'
    '    "/usr",\n'
    '    "/lib",\n'
    '    "/lib64",\n'
    '    "/bin",\n'
    '    "/sbin",\n'
    '    "/home",\n'
    '    "/opt",\n'
    '    "/srv",\n'
    '    "/mnt",\n'
    '    "/media",\n'
    ')'
)
new = (
    'SENSITIVE_PATH_BLACKLIST: tuple[str, ...] = (\n'
    '    "/etc",\n'
    '    "/root",\n'
    '    "/proc",\n'
    '    "/sys",\n'
    '    "/boot",\n'
    '    "/dev",\n'
    '    "/var/run/docker.sock",\n'
    '    "/var/lib/docker",\n'
    '    "/usr",\n'
    '    "/lib",\n'
    '    "/lib64",\n'
    '    "/bin",\n'
    '    "/sbin",\n'
    ')'
)
assert old in t, 'pattern not found'
t = t.replace(old, new, 1)
p.write_text(t, 'utf-8')
print('done')
