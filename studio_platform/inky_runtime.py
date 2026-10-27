"""Opt-in runtime construction, with no provider call at startup or on reads."""

import logging

from .inky_config import runtime_config
from .inky_providers import Jev, Terra
from .inky_worker import InkyWorker
from .inky_uploads import UploadAdvice

LOG = logging.getLogger(__name__)


def configure(app):
    app.state.inky_worker = None
    filename = app.state.settings.inky_config_file
    if filename is None:
        return
    try:
        credentials = runtime_config(filename)
        if credentials is None:
            return
        # No central registry fallback on a hosted deployment. The trusted
        # runtime contract pins profiles/origins; account/month caps are code.
        judge = Jev(loader=credentials.__getitem__)
        app.state.inky_worker = InkyWorker(
            app.state.inky_store,
            judge=judge,
            examples=Terra(loader=credentials.__getitem__),
            uploads=UploadAdvice(app.state.assets, judge),
        )
    except Exception:
        LOG.warning("inky_runtime_unavailable")
