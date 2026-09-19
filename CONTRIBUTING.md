# Contributing

Contributions are welcome.

1. Fork the repository and create a focused branch.
2. Install development dependencies with `python -m pip install -r requirements-dev.txt`.
3. Add behavioral tests for changes to signals, execution, market-data conversion, or Jev gating.
4. Run `python -m pytest -q`.
5. Open a pull request explaining behavior and risk implications.

Never include API keys, Moomoo credentials, account identifiers, `.env`, trading logs, or private market-data exports. Changes that affect live order execution should preserve safe defaults and document their failure behavior.
