"""Cross-cutting infrastructure: configuration, database, authentication, throttling.

Everything here is shared by every feature module and owns no business rules.
Feature modules import from `app.core`; `app.core` never imports from them.
"""
