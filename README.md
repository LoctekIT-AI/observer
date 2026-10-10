# Observer
Metadata-only Skill/MCP lifecycle observer for desktop AI tools.
This repository publishes customer-ready Observer bundles and release notes.

## Current release
- Version: v0.2.1
- Supported and verified: macOS arm64/x64
- Windows x64: customer install and native desktop event delivery testing in progress
- Download: [Observer v0.2.1 customer test bundle](https://github.com/reyfeng9-glitch/observer/releases/tag/v0.2.1)

## Scope
- Native desktop host adapters and observer workers
- SQLite cursor and Langfuse outbox
- Keychain-backed configuration on macOS
- Install, status, doctor, upgrade rollback, and uninstall outbox checks
- Metadata only: prompts, chain-of-thought, credentials, and complete tool results are not collected

Download the package from the release link above and follow the included installer and configuration instructions. Windows feedback should include the installer result and native event delivery result so fixes can be published as a new version.
