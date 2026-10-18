"""Standalone Sixnine entrypoint; independent of the original H3 data directory."""

from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.google_titles import configured_generator

settings = Settings.from_environment()
app = create_app(settings, title_generator=configured_generator(settings))
