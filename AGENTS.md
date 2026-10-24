# Release versioning

- Bump the release version when adding features; do not accumulate feature batches under an unchanged version.
- Use a minor bump for a feature batch and a patch bump for fixes. Keep `board/version.py` and `pyproject.toml` in sync.
- Bump `SCHEMA_VERSION` only when the persisted schema changes; it does not replace the release version bump.
- Verify the running hub version after activation. Existing MCP bridges retain their loaded version until reconnected.
