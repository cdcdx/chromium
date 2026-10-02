"""Platform names and supported target architectures shared by all entrypoints."""
MATRIX = {
    'win': ('x86', 'x64', 'arm64'), 
    'mac': ('x64', 'arm64'),
    'linux': ('x86', 'x64', 'arm64'), 
    'android': ('x64', 'arm64')
}


def normalize_os(value):
    return {'windows': 'win', 'macos': 'mac'}.get(value, value)


def host_for(target_os):
    return 'linux' if target_os == 'android' else target_os


def architectures(target_os, requested):
    supported = MATRIX[target_os]
    if requested == 'all':
        return supported
    if requested not in supported:
        raise ValueError(f'{target_os} 支持的架构: {", ".join(supported)}')
    return (requested,)
