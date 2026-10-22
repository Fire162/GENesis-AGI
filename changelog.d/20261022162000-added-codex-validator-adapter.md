- Added an inactive Codex validator adapter that runs existing safety guards,
  converts approval requests to explicit denials, retains advisories and refuses
  observable guard failures. Shell calls require an absolute directory anchor;
  patches are checked against their actual targets in validator scratch roots.
  Ordinary Codex configuration and MCP access are unchanged.
