"""KiCad plugin package initialization."""

try:
    import pcbnew  # noqa: F401
except ImportError:
    pass  # Running outside KiCad (CLI / tests)
else:
    try:
        from .plugin import JlcColorfulSilkscreenPlugin

        JlcColorfulSilkscreenPlugin().register()
    except Exception:
        import traceback

        traceback.print_exc()
