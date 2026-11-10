import { deck } from "@/copy/deck";
import { stampView } from "@/lib/stamp";

/** The label a chip shows for `id` (RD11), as the tests of the pages that hold chips expect it. */
export const stampLabel = (id: string, evidence?: unknown): string => deck.chips.label(stampView(id, evidence));
