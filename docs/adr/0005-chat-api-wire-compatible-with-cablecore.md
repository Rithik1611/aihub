# 0005. aihub's chat API is wire-compatible with cableCORE's

Status: Accepted
Date: 2026-11-12

## Context

aihub serves normal chat to cableCORE's existing frontend. That frontend reads
`res.data.data` from a `{statusCode, message, data}` envelope and expects camelCase
fields (`isPinned`, `messageCount`, `sessionId`, `questionLead`).

aihub started out returning bare snake_case JSON. Pointing the frontend at it would
have meant rewriting every chat hook and component - churn with no benefit, on code
that is being replaced anyway. Chat history may also need to be migrated out of
cableCORE later, which is only a straight copy if the stored JSON shape matches.

## Decision

aihub's chat API reproduces cableCORE's chat API byte for byte: the same envelope,
the same camelCase field names, the same status codes and the same error messages.
Python keeps snake_case internally; Pydantic's `to_camel` alias generator handles the
wire, and both spellings are accepted as input. Message metadata is **stored** with
camelCase keys too, so rows can be copied from cableCORE without transformation.

## Consequences

Cutover is a base-URL change, which is what made it testable in a single session:
one frontend file routes normal chat to aihub and everything else stays put.
Rolling back is the same switch in reverse.

The cost is that cableCORE's conventions are now aihub's conventions, including ones
aihub would not have chosen - an envelope that duplicates the HTTP status, and
camelCase in a Python codebase. A future consumer that is not cableCORE inherits
them.

This is a wire contract, so changing it later breaks clients. When aihub serves an
application that is not cableCORE, the way out is a versioned path (`/api/v2/chat`)
rather than editing these responses, and this ADR gets superseded then.
