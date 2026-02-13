"""Compatibility shim: bridges now live in agent-tether package.

This module re-exports everything from agent_tether so that existing
imports like ``from tether.bridges.base import ...`` continue to work.
"""
