"""Advocate package — trusted parent process components.

The Advocate holds all secrets and network access; the LLM agent runs in a
child process with a cleared environment (no secrets), reaching the Advocate
over a loopback-TCP proxy at 127.0.0.1:<port> (see spec §4, §5, §8). Note: V0 is
non-root single-UID — see the spec for the honest isolation limits.
"""
