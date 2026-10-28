"""Content-free webhook observations, never payment processing authority."""

import threading
import time
import uuid

PATH = "/internal/payment-alert-counters"
WINDOW_SECONDS = 300
MAX_WINDOW_SECONDS = 3600
CATEGORIES = ("signature_failures", "processing_failures")


class PaymentAlertCounters:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.epoch = str(uuid.uuid4())
        self.buckets = {}
        self.lock = threading.Lock()

    def _prune(self, now):
        for second in list(self.buckets):
            if not now - MAX_WINDOW_SECONDS < second <= now:
                self.buckets.pop(second)

    def record(self, category):
        if category not in CATEGORIES:
            raise ValueError("payment_counter_category_invalid")
        now = int(self.clock())
        with self.lock:
            self._prune(now)
            counts = self.buckets.setdefault(now, [0, 0])
            counts[CATEGORIES.index(category)] += 1

    def snapshot(self, *, window_seconds=WINDOW_SECONDS):
        if (
            type(window_seconds) is not int
            or not 60 <= window_seconds <= MAX_WINDOW_SECONDS
        ):
            raise ValueError("payment_counter_window_invalid")
        now = int(self.clock())
        with self.lock:
            self._prune(now)
            return {
                "epoch": self.epoch,
                "window_seconds": window_seconds,
                **{
                    name: sum(
                        counts[index]
                        for second, counts in self.buckets.items()
                        if now - window_seconds < second <= now
                    )
                    for index, name in enumerate(CATEGORIES)
                },
            }


def receive_webhook(service, raw, signature):
    """Preserve the original verify/receipt path; never retain raw or errors."""
    from .stripe_client import StripeGatewayError

    try:
        event = service.gateway.verify_event(raw, signature)
    except StripeGatewayError as error:
        service.payment_alert_counters.record(
            "signature_failures"
            if error.code == "billing_signature_invalid"
            else "processing_failures"
        )
        raise
    except Exception:
        service.payment_alert_counters.record("processing_failures")
        raise
    try:
        if event is not None:
            service.core.receive_event(event)
    except Exception:
        service.payment_alert_counters.record("processing_failures")
        raise
    return {"received": True}
