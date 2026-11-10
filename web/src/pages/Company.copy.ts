// R25 (owner 2026-11-08: yes) and docs/specs/m5b-redesign.md section 4: the four labels of the kinds of risk change, and nothing
// else (tests/test_web_copy_deck.py admits this file as a copy home only while these are all it exports). They are the
// section labels of the server's context layout (retrieval/context_layout.py _ITEM_SECTIONS), pinned by
// tests/test_answerer_blocks.py and tests/test_answerer_context.py; the Method page's reliability note words the same labels. No
// kind is ever called "removed" or "dropped": that would overclaim. The block's other words are in copy/deck.ts company.changes.
export const KIND_NEW = "No matching risk factor found in the earlier filing";
export const KIND_DROPPED = "No longer appears as a separate risk factor";
export const KIND_UNSETTLED = "Not matched";
export const KIND_CHANGED = "Reworded";
