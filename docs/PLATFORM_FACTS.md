# Dynamic platform facts

Revalidated against primary sources on 2026-09-02. Code must still discover actual authenticated
capabilities and schemas at runtime.

- Robinhood continues to publish `https://agent.robinhood.com/mcp/trading` as the Streamable HTTP
  endpoint. Its options surface includes quote/chain/instrument/history/position/order reads,
  `review_option_order`, placement/cancel, and `get_option_level_upgrade_info`. The MCP also has
  non-order mutations such as watchlist and scan changes, so shadow uses a narrow read/review
  allowlist. See [overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
  and [tool list](https://robinhood.com/us/en/support/articles/trading-with-your-agent/).
- The official MCP Python SDK stable line is v2; this project pins `mcp==2.1.1` and uses its `Client`
  transport/OAuth architecture. OAuth discovery, PKCE, registration, token refresh, and local
  browser callback are SDK-managed; endpoints are never hardcoded. See the
  [v2.1.1 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.1.1),
  [transport docs](https://py.sdk.modelcontextprotocol.io/client/transports/), and
  [OAuth docs](https://py.sdk.modelcontextprotocol.io/client/oauth-clients/).
- Ollama supports native Windows/NVIDIA and localhost API operation. `qwen3.5:9b` is published as
  9.65B parameters, Q4_K_M, approximately 6.6 GB; model-advertised context is not assumed to fit
  the GPU. See [Windows support](https://docs.ollama.com/windows),
  [model entry](https://ollama.com/library/qwen3.5:9b), and
  [context guidance](https://docs.ollama.com/context-length).
- Docker Desktop requires a current WSL2 backend for Windows GPU access. V1 keeps Ollama native and
  does not expose the GPU to the application container. See [WSL2 backend](https://docs.docker.com/desktop/features/wsl/)
  and [GPU support](https://docs.docker.com/desktop/features/gpu/).

Public Robinhood documentation does not publish authoritative JSON schemas or prove that an
unfunded account's review call will succeed. Those remain authenticated qualification evidence,
not assumptions.
