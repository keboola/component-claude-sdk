"""Advocate package — trusted parent process components.

The Advocate holds all secrets and network access; the LLM agent runs in a
sandboxed child with no secrets and no network, communicating only over a
unix-domain socket (see spec §4, §5).
"""
