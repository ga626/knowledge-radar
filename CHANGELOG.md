# Changelog

All notable user-facing changes are recorded here. This project follows an Alpha release channel; a version is delivered only when its immutable candidate artifact has passed its release checks.

## Unreleased

- Make the local configuration console fast on large data roots: first open now reads only sanitized installation/capability state, and category storage scanning is user-initiated.
- Keep the redistributable ZIP focused on product operation: retain the product installer, plugin, local console, runtime, and user help; exclude source-only installers, launchers, test/verification utilities, and development setup scripts.
- Clarify the Windows/Codex support boundary, optional-provider cost/login behavior, and safe data-root migration in Chinese-first user guidance.
- Add safe product data-root relocation: plan, explicit confirmation, browser-lock refusal, copy-and-SHA-256 verification, active MCP switch, and rollback while preserving the old data root.
- Keep browser state, Profile registry, media cache, and product runtime under the active product data root; add a public storage-ownership policy and reversible quarantine for manifest-known expired media cache.
- Repair Windows stdio verifier cleanup so it waits for the child process and closes all stdio handles before removing its temporary state.
- Repair the Release first-use contract: one stable `configure.cmd`, public stdio verification, data-root diagnostics, and separated user/developer documentation.
- Improve the repository entry points, support guidance, contributor conduct, and bilingual README accuracy.

## 0.1.0a4

- First public Alpha Release with a local product installer, versioned app/runtime, separate user data root, rollback, and Codex MCP registration.
