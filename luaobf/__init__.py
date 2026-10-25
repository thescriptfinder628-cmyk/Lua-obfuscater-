"""luaobf -- a Lua obfuscator.

Usage::

    from luaobf import obfuscate
    result = obfuscate(source_code, level="medium")
    print(result.code)
"""

from .obfuscator import Options, Result, obfuscate_source, preset

__version__ = "2.0.0"

__all__ = ["obfuscate", "obfuscate_source", "Options", "Result", "preset", "__version__"]


def obfuscate(source: str, level: str = "medium", **options) -> Result:
    """Obfuscate ``source`` using a preset plus keyword overrides."""
    return obfuscate_source(source, preset(level, **options))
