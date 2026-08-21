"""Fan-out of print events to connected clients.

Every job carries a reply_to token naming whoever submitted it, and every event
is tagged with it. Subscribers filter for their own token, so the bot and the
kiosk share one stream without either needing to run a listener of its own --
which matters for the kiosk, since it lives behind the unreliable link.

A subscriber that stops reading is dropped rather than allowed to block the
print worker: a wedged Discord connection must never stall the printer.
"""

import asyncio

QUEUE_LIMIT = 100


class EventBus:
    def __init__(self):
        self._subscribers = set()

    def subscribe(self):
        queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)

        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self):
        return len(self._subscribers)

    def publish(self, event):
        """Non-blocking by design, so the print worker never waits on a client."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
