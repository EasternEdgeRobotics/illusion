"""Fan-out of service events to connected clients.

Every event carries enough context for a subscriber to decide whether it cares.
Subscribers filter client side, so several consumers share one stream without
any of them needing to run a listener of its own -- which matters for the
kiosk, which lives behind the unreliable link.

Publishing never blocks: a wedged client must not be able to stall the print
worker or a database write. A subscriber that falls too far behind is evicted
instead, and told so, which is the important part -- an evicted subscriber that
was left holding an open connection would sit there receiving keepalives and
nothing else, looking healthy while being permanently deaf.
"""

import asyncio

QUEUE_LIMIT = 100


class Subscription:
    def __init__(self, maxsize=QUEUE_LIMIT):
        self.queue = asyncio.Queue(maxsize=maxsize)
        # Set when this subscriber fell behind and was evicted. The stream
        # watches it and closes, so the client reconnects and re-syncs rather
        # than waiting forever on a bus it is no longer part of.
        self.dropped = asyncio.Event()


class EventBus:
    def __init__(self):
        self._subscribers = set()

    def subscribe(self):
        subscription = Subscription()
        self._subscribers.add(subscription)

        return subscription

    def unsubscribe(self, subscription):
        self._subscribers.discard(subscription)

    @property
    def subscriber_count(self):
        return len(self._subscribers)

    def publish(self, event):
        for subscription in list(self._subscribers):
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.dropped.set()
                self._subscribers.discard(subscription)
