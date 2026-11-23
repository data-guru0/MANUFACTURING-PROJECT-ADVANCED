# ADR 0006: Envoy egress allow-list to prevent data leaks

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Customer drawings and prices are confidential (W18). A compromised dependency or an injected instruction could try to send data to any host on the internet.

## Decision
Application containers sit on internal networks with **no internet route**. Only LiteLLM, ClamAV (virus-signature downloads, added in S1) and, from S5, the commodity-index fetcher are on the internal `egress` network. They reach the internet only through **envoy-egress**, an HTTP CONNECT proxy that allows `api.groq.com` and `database.clamav.net` (plus the commodity data host from S5) and answers 403 to everything else.

## Consequences
+ Data can only leave through one audited door, to named hosts.
+ An injected "fetch this URL" fails at the network layer (SSRF, S9 red-team).
− Every new external service is an explicit change to the allow-list (by design).
