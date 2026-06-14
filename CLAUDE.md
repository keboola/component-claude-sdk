## Secrets — secrets.json (NEVER read values)
- `secrets.json` (repo root) holds REAL secret VALUES, e.g. `parameters.#anthropic_key` (later possibly a GitHub token). It is gitignored.
- Any agent MAY reference the KEY NAMES to wire config, write tests, and document usage — but MUST NEVER read, open, cat, head, grep, print, echo, or otherwise surface the VALUES.
- Do NOT use the Read tool on secrets.json and do NOT put `secrets.json` on a shell command line. A PreToolUse hook enforces this and will block such calls.
- The component code and the datadir/VCR test runners load secrets.json themselves at runtime — let them. Your job is to ensure the value is wired through (config schema `#anthropic_key`, test fixtures), never to inspect it.
