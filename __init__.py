"""KiCad plugin package initialization."""

try:
    from .plugin import JlcColorfulSilkscreenPlugin

    JlcColorfulSilkscreenPlugin().register()
except Exception:
    # Allow importing submodules outside KiCad (e.g. CLI / tests)
    pass
