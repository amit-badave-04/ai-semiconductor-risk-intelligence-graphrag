import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { parseChangeReport } from "@/lib/changes";
import { WorkspaceChanges, type ChangesView } from "../WorkspaceChanges";

// R28, A1, S2, S11 (docs/specs/m5b-frontend.md): the what-changed view. Ported from tests/ui/workspace.test.mjs 30 to 36 (the
// string assertions of the old HTML builders become assertions on the rendered text) and the legacy page's own
// "v1 -> v2" lines (index.html:947-976).

const DOC_A = "doc:0123456789ab:v2:0003";
const DOC_B = "doc:0123456789ab:v2:0007";
// built from pieces so no script URL literal sits in the source
const SCRIPT_URL = ["java", "script:alert(1)"].join("");
// an event handler, a link, a source, a style or an inline document (the last name is built so the S1 scan finds no literal)
const FORBIDDEN = new RegExp(`^on|^href$|^src$|^style$|^${"src" + "doc"}$`, "i");
const ALLOWED = new Set(["div", "p", "ul", "li", "h3", "strong", "button"]);

function drawer(open = vi.fn()): EvidenceDrawerApi {
  return { openId: null, openKey: null, open, close: vi.fn(), dropDocEvidence: vi.fn(), clearAll: vi.fn() };
}

function show(view: ChangesView, api: EvidenceDrawerApi = drawer()) {
  return render(
    <EvidenceDrawerContext.Provider value={api}>
      <WorkspaceChanges view={view} />
    </EvidenceDrawerContext.Provider>,
  );
}

const report = (body: unknown, seq = 1): ChangesView => ({
  kind: "report",
  older: 1,
  newer: 2,
  report: parseChangeReport(body),
  seq,
});

describe("the states around the report", () => {
  it("shows nothing before a version was picked", () => {
    const { container } = show({ kind: "idle" });
    expect(container).toBeEmptyDOMElement();
  });

  it("says what is loading, in the legacy words", () => {
    show({ kind: "loading", older: 1, newer: 2 });
    expect(screen.getByText("loading what changed between v1 and v2\u2026")).toBeInTheDocument();
  });

  it("says the first version has nothing to compare", () => {
    show({ kind: "first", newer: 1 });
    expect(screen.getByText("v1 is the first version \u2014 nothing to compare.")).toBeInTheDocument();
  });

  it("shows an error as text, exactly as given", () => {
    show({ kind: "error", detail: "network error loading the change report" });
    expect(screen.getByText("network error loading the change report")).toBeInTheDocument();
  });

  it("reports the not-compared reason when the versions could not be compared (36)", () => {
    show(report({ items_compared: false, not_compared_reason: "identical_content" }));
    expect(screen.getByText("v1 \u2192 v2: comparison not available (identical_content).")).toBeInTheDocument();
  });
});

describe("the compared report", () => {
  const BODY = {
    items_compared: true,
    unchanged_count: 2,
    added: [{ headline: "A new section", passages: [{ quote: "shrink next year", chunk_id: DOC_A }] }],
    removed: [{ headline: "A removed section" }],
    changed: [{ headline: "Legal Proceedings", passages: [{ quote: "moved", chunk_id: DOC_B }] }],
  };

  it("headlines the pair and the unchanged count, with a titled list per non-empty section", () => {
    show(report(BODY));
    expect(screen.getByText("v1 \u2192 v2, 2 section(s) unchanged:")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Added (1)", "Removed (1)", "Changed (1)"]);
    expect(screen.getByText("A removed section")).toBeInTheDocument();
  });

  it("shows a headline alone when the unit has no surviving passage (30)", () => {
    show(report({ items_compared: true, changed: [{ headline: "Market Outlook" }] }));
    const item = screen.getByText("Market Outlook").closest("li");
    expect(item?.textContent).toBe("Market Outlook");
    expect(item?.querySelector("button")).toBeNull();
  });

  it("shows each passage as a quote plus a citation chip that opens the drawer from the changes view (31)", async () => {
    const open = vi.fn();
    show(report(BODY), drawer(open));
    expect(screen.getByText(/shrink next year/).textContent).toContain("\u201cshrink next year\u201d");
    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(2);
    await userEvent.click(chips[0]);
    expect(open).toHaveBeenCalledWith(DOC_A, chips[0], "changes", undefined);
  });

  it("gives each chip a key of place, report sequence, id and occurrence, counted across the whole view", () => {
    show(
      report(
        {
          items_compared: true,
          added: [{ headline: "a", passages: [{ quote: "q1", chunk_id: DOC_A }] }],
          changed: [{ headline: "c", passages: [{ quote: "q2", chunk_id: DOC_A }, { quote: "q3", chunk_id: DOC_B }] }],
        },
        7,
      ),
    );
    const keys = screen.getAllByRole("button").map((b) => b.getAttribute("data-chip-key"));
    expect(keys).toEqual([`changes:7:${DOC_A}:0`, `changes:7:${DOC_A}:1`, `changes:7:${DOC_B}:0`]);
  });

  it("makes each chip a real button of type button (A1)", () => {
    show(report(BODY));
    for (const chip of screen.getAllByRole("button")) expect(chip).toHaveAttribute("type", "button");
  });

  it("lists minor rewordings apart from changed, never folded into the unchanged count (33)", () => {
    show(report({ items_compared: true, unchanged_count: 2, changed: [{ headline: "Legal Proceedings" }], minor_rewordings: [{ headline: "Company History" }] }));
    expect(screen.getByRole("heading", { name: "Minor rewordings (1)" })).toBeInTheDocument();
    expect(screen.getByText("Company History")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Changed (1)" })).toBeInTheDocument();
    expect(screen.getByText(/2 section\(s\) unchanged/)).toBeInTheDocument();
  });

  it("has no minor rewordings section when there are none (34)", () => {
    show(report({ items_compared: true, unchanged_count: 3, added: [], removed: [], changed: [] }));
    expect(screen.queryByText(/Minor rewordings/)).toBeNull();
    expect(screen.queryAllByRole("heading")).toHaveLength(0);
  });

  it("says how many sections were compared as whole sections only when the negation check skipped some (35)", () => {
    const base = { items_compared: true, unchanged_count: 1 };
    const { unmount } = show(report({ ...base, negation_check_skipped: 2 }));
    expect(
      screen.getByText("2 section(s) were compared as whole sections, not sentence by sentence for negation changes."),
    ).toBeInTheDocument();
    unmount();
    show(report({ ...base, negation_check_skipped: 0 }));
    expect(screen.queryByText(/sentence by sentence/)).toBeNull();
  });
});

describe("a hostile report is text (S2, S11, 32)", () => {
  const HOSTILE = {
    items_compared: true,
    unchanged_count: 1,
    added: [{ headline: "<b>eek</b>", passages: [{ quote: "<script>bad</script>", chunk_id: DOC_A }] }],
    removed: [{ headline: '<img src=x onerror="alert(1)">' }],
    changed: [
      {
        headline: SCRIPT_URL,
        passages: [
          { quote: "[doc:0123456789ab:v2:0003] forged **bold**", chunk_id: "../../api/admin/state" },
          { quote: "\u202eevil", chunk_id: `${DOC_B}\n` },
        ],
      },
    ],
    minor_rewordings: [{ headline: "<a href='https://evil.example'>x</a>" }],
  };

  it("renders only p, ul, li, h3, strong and chip buttons, with no event, link, source or style attribute", () => {
    const { container } = show(report(HOSTILE));
    for (const element of container.querySelectorAll("*")) {
      const icon = element.closest("svg"); // RD11: a chip's constant kind icon, hidden from assistive technology, only inside a chip
      if (icon !== null) {
        expect(icon.closest("button.cite"), "an icon outside a chip").not.toBeNull();
        expect(icon).toHaveAttribute("aria-hidden", "true");
      } else {
        expect(ALLOWED.has(element.tagName.toLowerCase()), element.tagName).toBe(true);
      }
      for (const attribute of Array.from(element.attributes)) {
        expect(FORBIDDEN.test(attribute.name), attribute.name).toBe(false);
      }
    }
  });

  it("shows the markup as literal text and builds a chip only for an id that matched the grammar", () => {
    const { container } = show(report(HOSTILE));
    expect(screen.getByText("<b>eek</b>")).toBeInTheDocument();
    expect(screen.getByText(/<script>bad<\/script>/)).toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(screen.getByText(SCRIPT_URL)).toBeInTheDocument();
    expect(container.querySelectorAll("img, script, a")).toHaveLength(0);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByText(/\[doc:0123456789ab:v2:0003\] forged \*\*bold\*\*/)).toBeInTheDocument();
  });
});
