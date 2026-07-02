"""Tool brokers — MCP and GitHub/HTTP.

Each broker is a server-side handler that:
- receives a narrow, strictly-validated RPC from the agent over the loopback-TCP
  proxy (127.0.0.1:<port>);
- injects the appropriate secret (MCP env/headers, GitHub token) server-side;
- forwards the call to the real upstream;
- returns a sanitised result, never echoing secret values back.

The agent's cleared environment carries ZERO secrets; it only knows the loopback
proxy address.
"""
