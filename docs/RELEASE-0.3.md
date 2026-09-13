# Eira 0.3

`Eira` now opens interactive chat directly. The lowercase `eira` command and existing subcommands remain supported.

- Styled terminal welcome panel with workspace, model, version, session, and permission context.
- Interactive provider/model setup with user-local preferences and process-only hidden key entry.
- Commands for help, model/provider selection, new sessions, session listing/resume, status, display clearing, and exit.
- Readline editing and bounded process-local input history; plain rendering for redirected output and `NO_COLOR`.
- Installer aliases and shell PATH setup for bash, zsh, and fish, with current-terminal activation guidance.

The provider transports, daily-price tools, reviewed writes, Docker-only shell policy, and durable sessions from 0.2 remain in place. This release improves the terminal workflow; it does not claim feature or benchmark parity with Claude Code.
