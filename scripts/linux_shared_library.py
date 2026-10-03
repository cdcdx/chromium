"""Enable V8's shared-library TLS model without changing snapshot delivery."""
from pathlib import Path


def prepare_v8_tls(source: Path, dry_run: bool = False) -> bool:
    path = source / "v8" / "BUILD.gn"
    original = path.read_text(encoding="utf-8")
    text = original
    argument = "  v8_tls_used_in_library = false"
    use = "  if (v8_tls_used_in_library) {"
    if argument not in text:
        anchor = "  v8_monolithic_for_shared_library = false\n"
        if text.count(anchor) != 1:
            raise ValueError(f"{path}: V8 shared-library argument anchor changed")
        text = text.replace(anchor, anchor + "\n"
                            "  # Arupa links V8 into a dlopen shared library.\n"
                            + argument + "\n", 1)
    if use not in text:
        anchor = ('  if (v8_monolithic && v8_monolithic_for_shared_library) {\n'
                  '    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n  }\n')
        if text.count(anchor) != 1:
            raise ValueError(f"{path}: V8 shared-library define anchor changed")
        text = text.replace(anchor, anchor + '\n' + use + '\n'
                            '    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n  }\n', 1)
    if text != original and not dry_run:
        path.write_text(text, encoding="utf-8")
    return text != original
