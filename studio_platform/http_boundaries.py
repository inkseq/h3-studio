"""Install final HTTP projections and optional post-commit advice hooks."""


def install(app):
    from .inky_routes import register_routes
    from .quality_http import install as quality
    from .inky_hooks import install as inky
    from .inky_runtime import configure
    from .llm_tracing import configure as tracing

    tracing(app.state.settings)
    register_routes(app)
    configure(app)
    quality(app)
    inky(app)
