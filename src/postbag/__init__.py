"""postbag — a newsletter engine you host yourself, shaped as a library.

Named for the carrier's postbag — the one sack that holds a whole round's
letters. That is what this engine is: the single place a list's outgoing mail
and the record of what already went out both live.

The shape, in one paragraph: you own the Postgres, an email service provider
owns the SMTP, and this package owns the parts in between — lists, subscribers,
suppression, an idempotent send log, markdown drip sequences gated on an
`approved: true` a human commits, compliant headers and footers, and a static
archive. There is no open or click tracking, and there is no plan to add any.

It brings no database driver. `store` builds parameterized SQL and `ddl` hands
you CREATE TABLE text; your application decides when and in which transaction
any of it runs.
"""

from postbag.tokens import (
    PURPOSE_UNSUBSCRIBE,
    InvalidTokenError,
    mint_token,
    verify_token,
)

__all__ = [
    "PURPOSE_UNSUBSCRIBE",
    "InvalidTokenError",
    "__version__",
    "mint_token",
    "verify_token",
]

__version__ = "0.1.0"
