"""Strict, account-scoped classic QQ keys, separate from QQ NT credentials."""
from __future__ import annotations

import re


def legacy_key_candidates(keys, *, account=None, relative_path=None, source=None):
    """Return only explicit 16-byte classic keys; never truncate NT keys.

    Automatic classic key recovery is deliberately not claimed. The known
    interception-based method needs client injection, outside this adapter's
    read-only boundary. Callers provide a matching classic key in memory.
    """
    fields = ("legacy_key", "key", "raw_key", "enc_key")

    def entries(value, scoped=True):
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from entries(item, scoped)
        elif isinstance(value, dict):
            # Explicit classic credential wins; a passphrase is not this cipher.
            if "legacy_key" in value:
                yield value["legacy_key"]
            elif any(field in value for field in fields):
                for field in fields[1:]:
                    if field in value:
                        yield value[field]
            elif scoped:
                normalized = {str(k).replace("\\", "/"): v for k, v in value.items()}
                selectors = (str(source) if source else None, relative_path, account, "default", "*")
                for selector in dict.fromkeys(selectors):
                    selector = str(selector).replace("\\", "/") if selector else None
                    if selector in normalized:
                        nested = normalized[selector]
                        # Account-scoped objects may contain database selectors.
                        yield from entries(nested, True)
        else:
            yield value

    result = []
    for entry in entries(keys):
        key = None
        if isinstance(entry, bytes):
            key = entry if len(entry) == 16 else None
        elif isinstance(entry, str):
            match = re.fullmatch(r"(?:([xX]')([0-9a-fA-F]{32})'|([0-9a-fA-F]{32}))", entry.strip())
            if match:
                key = bytes.fromhex(match[2] or match[3])
        if key and any(key) and key not in result:
            result.append(key)
    return result
