"""Compatibility shim: re-exports from agent_tether.subscriber and tether.bridges.glue.

Provides a backward-compatible BridgeSubscriber that can be constructed
with no arguments (uses global singletons from glue).
"""
# ruff: noqa: F401

import asyncio

from agent_tether.subscriber import BridgeSubscriber as _BridgeSubscriber


class BridgeSubscriber(_BridgeSubscriber):
    """Backward-compatible BridgeSubscriber that defaults to Tether's global singletons."""

    def __init__(
        self,
        bridge_manager=None,
        new_subscriber=None,
        remove_subscriber=None,
    ):
        if bridge_manager is None or new_subscriber is None or remove_subscriber is None:
            from tether.bridges.glue import (
                bridge_manager as _bm,
                _new_subscriber,
                _remove_subscriber,
            )

            bridge_manager = bridge_manager or _bm
            new_subscriber = new_subscriber or _new_subscriber
            remove_subscriber = remove_subscriber or _remove_subscriber

        super().__init__(
            bridge_manager=bridge_manager,
            new_subscriber=new_subscriber,
            remove_subscriber=remove_subscriber,
        )


# Re-export the global instances so that patching works
from tether.bridges.glue import bridge_subscriber, bridge_manager
