# Context — Layr8 Python SDK

## Ubiquitous Language

| Term | Definition |
|---|---|
| **Agent** | A software process that connects to a cloud-node and exchanges DIDComm v2 messages. An agent is identified by a DID. |
| **Cloud-node** | A Layr8 infrastructure component that routes DIDComm messages between agents. Agents connect via WebSocket using the Phoenix Channel V2 protocol. |
| **DID** | Decentralized Identifier — a globally unique agent identity (e.g., `did:web:myorg:my-agent`). May be configured explicitly or assigned by the cloud-node on connect. |
| **Handler** | An async function registered for a specific DIDComm message type. Receives a `Message`, returns a response `Message`, `None`, or `PASS`. |
| **PASS** | A sentinel value returned by a handler to decline a message — signals to the cloud-node that this agent does not handle this message type. |
| **Scenario** | A cross-language compatibility test case. Each scenario is a pair of async functions (`run_receiver`, `run_sender`) that exercise a specific SDK behavior against a cloud-node. |
| **Compat image** | A Docker image (`ghcr.io/layr8/python-sdk/compat:{version}`) that packages the scenario code and CLI adapter. Consumed by the compatibility orchestrator. |
| **Ready signal** | A JSON line (`{"status":"ready","did":"..."}`) printed to stdout by a receiver process after connecting and registering handlers. The compatibility orchestrator waits for this before launching the sender. |
| **Layer 1** | Pytest + testcontainers adapter — runs scenarios against real cloud-node Docker containers. |
| **Layer 2** | CLI adapter — implements the compatibility orchestrator's interface (`--mode`, `--scenario`, `--node`, `--did`). |
| **Compatibility orchestrator** | A separate repository that pairs SDK compat images across languages and cloud-node versions, runs test matrices, and produces compatibility reports. |
