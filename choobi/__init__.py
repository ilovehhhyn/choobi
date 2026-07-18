"""choobi — local-first documentation agent.

The package is organized around one engine verb, `update`. Every entry point
(post-commit hook, coding-agent chat, UI button, shell) composes the same
`engine.run_update` contract; nothing duplicates its reasoning.
"""

__version__ = "0.1.0"
