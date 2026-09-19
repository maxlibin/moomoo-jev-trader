# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose API keys, brokerage credentials, account data, or permit unintended order execution.

Use GitHub's private vulnerability reporting for this repository. Include reproduction steps, affected files, and the potential impact. Do not include real credentials or account identifiers.

## Trading and credential safety

- Keep `.env`, OpenD login data, trading logs, and API keys private.
- Begin with `AUTO_TRADE=false` and `MOOMOO_TRADE_ENV=SIMULATE`.
- Treat `MOOMOO_TRADE_ENV=REAL` as access to real funds.
- Restrict OpenD to localhost unless you have separately secured the network connection.
- Rotate any credential that may have been exposed.
