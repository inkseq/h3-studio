"""Install final HTTP projections and optional post-commit advice hooks."""


def install(app):
    from .inky_routes import register_routes
    from .quality_http import install as quality
    from .inky_hooks import install as inky

    register_routes(app)
    # No provider worker/configuration is created merely by mounting readers.
    quality(app)
    inky(app)
