// GENERATED FILE - DO NOT EDIT.
// Written by scripts/export_ids.py from src/semigraph/retrieval/ids.py.
// Regenerate with: uv run python -m scripts.export_ids
// tests/test_export_ids.py fails while this file is stale.
//
// The citation-id grammar of the server. The *_ID_PATTERN strings embed in larger expressions; the *_ID_ANCHORED
// strings and the *_ID_RE objects validate a whole string. Python ends an anchored form with \Z (end of the string and
// nothing else); JavaScript has no \Z, so the generator writes $, which means the same without the m flag: build these
// without it. CHUNK_ID_ANCHORED is stricter than CHUNK_ID_PATTERN (10 to 30 characters before the first colon).
// CITE_PATTERN is a source string, not a RegExp: a shared global RegExp keeps lastIndex between uses, so a caller makes
// its own: new RegExp(CITE_PATTERN, "g"); group 1 of a match is the bare id.

export const CHUNK_ID_PATTERN = "[0-9\\-]+:[IVX]+\\.[0-9A-Z]+:[0-9]{4}";
export const XBRL_ID_PATTERN = "xbrl:[0-9]+:[a-z][a-z0-9_]*:[0-9]{4}-[0-9]{2}-[0-9]{2}";
export const FR_ID_PATTERN = "fr:(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}";
export const DOC_ID_PATTERN = "doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4}";
export const CITE_PATTERN = "\\[([0-9\\-]+:[IVX]+\\.[0-9A-Z]+:[0-9]{4}|xbrl:[0-9]+:[a-z][a-z0-9_]*:[0-9]{4}-[0-9]{2}-[0-9]{2}|fr:(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}|doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4})\\]";
export const CHUNK_ID_ANCHORED = "^[0-9\\-]{10,30}:[IVX]+\\.[0-9A-Z]+:[0-9]{4}$";
export const XBRL_ID_ANCHORED = "^xbrl:[0-9]+:[a-z][a-z0-9_]*:[0-9]{4}-[0-9]{2}-[0-9]{2}$";
export const FR_ID_ANCHORED = "^fr:(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}$";
export const DOC_ID_ANCHORED = "^doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4}$";
export const CHUNK_ID_RE = new RegExp(CHUNK_ID_ANCHORED);
export const XBRL_ID_RE = new RegExp(XBRL_ID_ANCHORED);
export const FR_ID_RE = new RegExp(FR_ID_ANCHORED);
export const DOC_ID_RE = new RegExp(DOC_ID_ANCHORED);
