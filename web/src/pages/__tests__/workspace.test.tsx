import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { getStats, refreshStats, resetStatsStore } from "@/lib/statsStore";
import { WS_SESSION_KEY } from "@/lib/workspace";
import { INERT_TURNSTILE, TurnstileContext } from "@/state/useTurnstile";
import { WorkspaceProvider } from "@/state/WorkspaceProvider";
import { deck } from "@/copy/deck";
import { FILE_ACCEPT } from "@/lib/workspace";
import { Workspace } from "../Workspace";

// the Workspace page's words (R4: moved from pages/Workspace.copy.ts to the deck under the same names)
const copy = deck.workspace;

// R28, A3, A4, A6, S8, S12 (docs/specs/m5b-frontend.md): the workspace page, over the real provider and a fake server. The copy
// is pinned against the legacy page (index.html), character for character.

const ID = "0123456789abcdef0123456789abcdef";
const TOKEN = "tok-SECRET_0123456789"; // a fake test token, not a credential - gitleaks:allow
const EXPIRES = "2026-11-09T10:00:00+00:00";
const DOC = "abc123456789";
const V1 = "2026-09-01T00:00:00Z";
const V2 = "2026-09-15T00:00:00Z";
const CHUNK = `doc:${DOC}:v2:0003`;
// a document's title as its heading shows it (the as-of label repeats the title in its own <bdi>)
const HEADING = { selector: "strong bdi" };

const docs = (extra = false, extraInstant = "2026-09-20T00:00:00Z", extraTitle = "<b>other</b>") => ({
  workspace_id: ID,
  expires_at: EXPIRES,
  documents: [
    {
      document_id: DOC,
      title: "memo.txt",
      latest_version: 2,
      versions: [
        { version: 1, created_at: V1, is_current: false, status: "superseded" },
        { version: 2, created_at: V2, is_current: true, status: "ready" },
      ],
    },
    ...(extra
      ? [{ document_id: "def123456789", title: extraTitle, latest_version: 1, versions: [{ version: 1, created_at: extraInstant, is_current: true, status: "ready" }] }]
      : []),
  ],
});

interface Fake {
  calls: { method: string; url: string; headers: Record<string, string>; body: unknown }[];
  uploads: { value: boolean };
  extraDocument: { value: boolean };
  extraInstant: { value: string };
  extraTitle: { value: string };
  createDelayMs: { value: number };
  createResponse: { value: Response | null };
  restoreOk: { value: boolean };
  post: { value: (n: number) => Response };
  changes: { value: Response };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function installFake(): Fake {
  const fake: Fake = {
    calls: [],
    uploads: { value: true },
    extraDocument: { value: false },
    extraInstant: { value: "2026-09-20T00:00:00Z" },
    extraTitle: { value: "<b>other</b>" },
    createDelayMs: { value: 0 },
    createResponse: { value: null },
    restoreOk: { value: true },
    post: { value: () => json({ unchanged: true, document_id: DOC, version: 2 }) },
    changes: {
      value: json({
        items_compared: true,
        unchanged_count: 3,
        added: [{ headline: "A new section", passages: [{ quote: "shrink next year", chunk_id: CHUNK }] }],
        removed: [],
        changed: [],
      }),
    },
  };
  let posts = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      fake.calls.push({ method, url, headers: (init?.headers ?? {}) as Record<string, string>, body: init?.body });
      if (url === "/api/stats") return json({ uploads_enabled: fake.uploads.value });
      if (method === "POST" && url === "/api/workspace") {
        if (fake.createDelayMs.value > 0) await new Promise((r) => setTimeout(r, fake.createDelayMs.value));
        return fake.createResponse.value ?? json({ workspace_id: ID, token: TOKEN, expires_at: EXPIRES }, 201);
      }
      if (method === "POST" && url.endsWith("/documents")) {
        posts += 1;
        return fake.post.value(posts);
      }
      if (method === "GET" && url.includes("/changes?")) return fake.changes.value.clone();
      if (method === "GET" && /\/api\/workspace\/[0-9a-f]{32}$/.test(url)) {
        return fake.restoreOk.value ? json(docs(fake.extraDocument.value, fake.extraInstant.value, fake.extraTitle.value)) : json({ detail: "x" }, 404);
      }
      if (method === "DELETE") return new Response(null, { status: 204 });
      return new Response("{}", { status: 404 });
    }),
  );
  return fake;
}

function drawer(open = vi.fn()): EvidenceDrawerApi {
  return { openId: null, openKey: null, open, close: vi.fn(), dropDocEvidence: vi.fn(), clearAll: vi.fn() };
}

function mount(api: EvidenceDrawerApi = drawer()) {
  return render(
    <StrictMode>
      <TurnstileContext.Provider value={{ ...INERT_TURNSTILE, siteKeyState: { kind: "none" } }}>
        <WorkspaceProvider>
          <EvidenceDrawerContext.Provider value={api}>
            <MemoryRouter>
              <Workspace />
            </MemoryRouter>
          </EvidenceDrawerContext.Provider>
        </WorkspaceProvider>
      </TurnstileContext.Provider>
    </StrictMode>,
  );
}

// the page's markup as a string (a serializer, not an HTML sink: the S1 scan lists the property names)
const pageMarkup = () => new XMLSerializer().serializeToString(document.body);
const store = () => window.sessionStorage.setItem(WS_SESSION_KEY, JSON.stringify({ id: ID, token: TOKEN, expires_at: EXPIRES }));
const open = async () => {
  store();
  const fake = installFake();
  mount();
  await screen.findByText("memo.txt", HEADING);
  return fake;
};

beforeEach(() => {
  window.sessionStorage.clear();
  resetStatsStore();
});
afterEach(() => vi.unstubAllGlobals());

describe("hidden until uploads are enabled (R28)", () => {
  it("shows the heading alone, and makes no other request, while uploads are off", async () => {
    const fake = installFake();
    fake.uploads.value = false;
    mount();
    expect(screen.getByRole("heading", { level: 1, name: "Workspace" })).toBeInTheDocument();
    await waitFor(() => expect(fake.calls.some((c) => c.url === "/api/stats")).toBe(true));
    expect(screen.queryByText(copy.PANEL_TITLE)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(fake.calls.map((c) => c.url).every((u) => u === "/api/stats")).toBe(true);
  });

  it("makes no second /api/stats request when the stats are already loaded (the Ask page's store)", async () => {
    const fake = installFake();
    await refreshStats();
    expect(getStats()).not.toBeNull();
    const before = fake.calls.filter((c) => c.url === "/api/stats").length;
    mount();
    expect(await screen.findByRole("heading", { level: 2, name: copy.PANEL_TITLE })).toBeInTheDocument();
    expect(fake.calls.filter((c) => c.url === "/api/stats")).toHaveLength(before);
  });

  it("stays hidden when /api/stats fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    mount();
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(screen.queryByText(copy.PANEL_TITLE)).toBeNull();
  });

  it("shows the panel, the intro and the create controls when uploads are on, and sends no POST by itself (S12)", async () => {
    const fake = installFake();
    mount();
    expect(await screen.findByRole("heading", { level: 2, name: copy.PANEL_TITLE })).toBeInTheDocument();
    expect(screen.getByText(copy.INTRO)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: copy.CREATE_BUTTON })).toBeInTheDocument();
    expect(fake.calls.filter((c) => c.method !== "GET")).toEqual([]);
  });
});

describe("every control has a label (A4) and the token stays out of the page (S8)", () => {
  it("names the restore id and token fields, and makes the token a password field with autocomplete off", async () => {
    installFake();
    mount();
    const id = await screen.findByLabelText(copy.ID_LABEL);
    const token = screen.getByLabelText(copy.TOKEN_LABEL);
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("autocomplete", "off");
    expect(id).toHaveAttribute("autocomplete", "off");
  });

  it("names the as-of date, the upload target, the file input and each document's selector", async () => {
    await open();
    expect(screen.getByLabelText(copy.AS_OF_TITLE)).toHaveAttribute("type", "date");
    expect(screen.getByLabelText(copy.UPLOAD_AS)).toBeInstanceOf(HTMLSelectElement);
    const zone = screen.getByRole("button", { name: copy.DROP_TEXT });
    expect(document.querySelector('input[type="file"]')).toHaveAttribute("aria-labelledby", zone.id); // named by the drop zone's text
    expect(screen.getByLabelText("memo.txt:")).toBeInstanceOf(HTMLSelectElement);
  });

  it("restores a workspace from the typed id and token, clears both fields, and never writes the token into the page", async () => {
    const fake = installFake();
    mount();
    await userEvent.type(await screen.findByLabelText(copy.ID_LABEL), ID);
    await userEvent.type(screen.getByLabelText(copy.TOKEN_LABEL), TOKEN);
    expect(pageMarkup()).not.toContain(TOKEN); // typing into a password field leaves no value attribute
    await userEvent.click(screen.getByRole("button", { name: copy.RESTORE_BUTTON }));
    await screen.findByText("memo.txt", HEADING);
    expect(fake.calls.find((c) => c.url === `/api/workspace/${ID}`)?.headers).toEqual({ "X-Workspace-Token": TOKEN });
    expect(pageMarkup()).not.toContain(TOKEN);
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(document.body.textContent).toContain(ID);
  });

  it("says 'workspace not found' and keeps what was typed out of the page when the server does not confirm", async () => {
    const fake = installFake();
    fake.restoreOk.value = false;
    mount();
    await userEvent.type(await screen.findByLabelText(copy.ID_LABEL), ID);
    await userEvent.type(screen.getByLabelText(copy.TOKEN_LABEL), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: copy.RESTORE_BUTTON }));
    expect(await screen.findByText("workspace not found")).toBeInTheDocument();
    expect(pageMarkup()).not.toContain(TOKEN);
  });

  it("does nothing, with no request, when Restore is pressed with an empty field", async () => {
    const fake = installFake();
    mount();
    await userEvent.type(await screen.findByLabelText(copy.ID_LABEL), ID);
    await userEvent.click(screen.getByRole("button", { name: copy.RESTORE_BUTTON }));
    expect(fake.calls.filter((c) => c.url.startsWith("/api/workspace"))).toEqual([]);
  });
});

describe("create and delete", () => {
  it("creates at once, shows the new workspace with its expiry and no token, and Delete closes it again", async () => {
    const fake = installFake();
    mount();
    await userEvent.click(await screen.findByRole("button", { name: copy.CREATE_BUTTON }));
    expect(await screen.findByText(copy.DELETE_BUTTON)).toBeInTheDocument();
    expect(screen.getByText(ID)).toBeInTheDocument();
    expect(document.body.textContent).toContain(`\u00b7 expires ${EXPIRES}`);
    expect(pageMarkup()).not.toContain(TOKEN);
    expect(fake.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(screen.getByText(copy.NO_DOCUMENTS)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: copy.DELETE_BUTTON }));
    expect(await screen.findByRole("button", { name: copy.CREATE_BUTTON })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(WS_SESSION_KEY)).toBeNull();
  });

  it("shows the server's text for a refusal in the status region", async () => {
    const fake = installFake();
    fake.createResponse.value = json({ detail: "Bot check failed \u2014 reload the page and try again." }, 403);
    mount();
    await userEvent.click(await screen.findByRole("button", { name: copy.CREATE_BUTTON }));
    expect(await screen.findByText("Bot check failed \u2014 reload the page and try again.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Bot check failed");
  });

  it("sends exactly one create for a double click", async () => {
    const fake = installFake();
    fake.createDelayMs.value = 60;
    mount();
    const button = await screen.findByRole("button", { name: copy.CREATE_BUTTON });
    await userEvent.dblClick(button);
    await screen.findByText(copy.DELETE_BUTTON);
    expect(fake.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});

describe("the upload area (A6, S12)", () => {
  it("offers a new document and a new version of each existing one, with each document's own title as text", async () => {
    const fake = installFake();
    fake.extraDocument.value = true;
    store();
    mount();
    await screen.findByText("memo.txt", HEADING);
    const options = within(screen.getByLabelText(copy.UPLOAD_AS)).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Upload a new document", "New version of memo.txt", "New version of <b>other</b>"]);
    expect(document.querySelector("b")).toBeNull(); // the title is text, never markup
  });

  it("makes the drop zone a real button that opens the file chooser from the click and from Enter and Space", async () => {
    await open();
    const zone = screen.getByRole("button", { name: copy.DROP_TEXT });
    expect(zone.tagName).toBe("BUTTON");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const click = vi.spyOn(input, "click");
    await userEvent.click(zone);
    zone.focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard(" ");
    expect(click).toHaveBeenCalledTimes(3);
    expect(input).toHaveAttribute("accept", FILE_ACCEPT);
  });

  it("uploads a chosen file into the selected document, once, and shows its row", async () => {
    const fake = await open();
    await userEvent.selectOptions(screen.getByLabelText(copy.UPLOAD_AS), DOC);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(["hello"], "v2.txt", { type: "text/plain" }));
    expect(await screen.findByText("unchanged (still v2)")).toBeInTheDocument();
    const posts = fake.calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe(`/api/workspace/${ID}/documents`);
    expect((posts[0].body as FormData).get("document_id")).toBe(DOC);
    expect(screen.getByText("v2.txt")).toBeInTheDocument();
    expect(input.value).toBe("");
  });

  it("uploads dropped files one at a time and lets drag over show the target", async () => {
    const fake = await open();
    const zone = screen.getByRole("button", { name: copy.DROP_TEXT });
    const files = [new File(["1"], "a.txt"), new File(["2"], "b.txt")];
    const dataTransfer = { files } as unknown as DataTransfer;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.dragOver(zone, { dataTransfer });
    expect(zone).toHaveAttribute("data-dragging");
    fireEvent.drop(zone, { dataTransfer });
    expect(zone).not.toHaveAttribute("data-dragging");
    await waitFor(() => expect(screen.getAllByText("unchanged (still v2)")).toHaveLength(2));
    expect(fake.calls.filter((c) => c.method === "POST")).toHaveLength(2);
  });

  it("announces a terminal upload state in the polite status region (A3)", async () => {
    await open();
    await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["x"], "n.txt"));
    await waitFor(() => expect(screen.getAllByRole("status").some((s) => s.textContent === "n.txt: unchanged (still v2)")).toBe(true));
  });
});

// docs/specs/m5b-redesign.md R4 (the open workspace, slice 5): names come from the uploader. A right-to-left override (U+202E) in one must not reorder
// the words around it, so each name sits in its own <bdi>; long names and ids wrap inside the page, the selects shrink with it,
// and the drag cue changes the border's style, not only its colour.
// (a .txt file the picker accepts, which an override would show as "reporttxt.fdp")
const RLO_NAME = "report‮pdf.txt";

describe("uploader names are isolated and wrap (R4)", () => {
  it("puts a document's title in a <bdi>, in its heading and in its as-of label", async () => {
    const fake = installFake();
    fake.extraDocument.value = true;
    fake.extraTitle.value = RLO_NAME;
    store();
    mount();
    const strong = await screen.findByText(RLO_NAME, HEADING);
    expect(strong.parentElement?.tagName).toBe("STRONG");
    expect(strong.parentElement?.className).toContain("break-all");
    const label = (screen.getByLabelText(`${RLO_NAME}:`) as HTMLSelectElement).closest("label");
    expect(label?.querySelector("bdi")?.textContent).toBe(RLO_NAME);
  });

  it("puts an upload's file name in a <bdi> in its row", async () => {
    await open();
    await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["x"], RLO_NAME));
    const name = await screen.findByText(RLO_NAME, { selector: "[data-job-row] bdi" });
    expect(name.parentElement?.className).toContain("break-all");
  });

  it("lets the selects shrink and the workspace id wrap", async () => {
    await open();
    for (const select of document.querySelectorAll("select")) {
      expect(select.className).toContain("max-w-full");
      expect(select.className).toContain("min-w-0");
    }
    expect(screen.getByText(ID, { selector: "code" }).className).toContain("break-all");
  });

  it("shows dragging with a solid border, not only a colour", async () => {
    await open();
    expect(screen.getByRole("button", { name: copy.DROP_TEXT }).className).toContain("data-[dragging]:border-solid");
  });
});

describe("the documents and the as-of choice (upload_flow 1 to 3)", () => {
  it("lists each document's latest version and timeline", async () => {
    await open();
    expect(screen.getByText("memo.txt", HEADING).closest("p")?.textContent).toBe("memo.txt (latest v2)");
    expect(screen.getByText("v1 (superseded) \u2192 v2 (current)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "memo.txt: v1" })).toBeInTheDocument();
  });

  it("shows the instant the visitor picked on the document that offers it, and 'current' on the rest", async () => {
    const fake = installFake();
    fake.extraDocument.value = true;
    store();
    mount();
    await screen.findByText("memo.txt", HEADING);
    const first = screen.getByLabelText("memo.txt:") as HTMLSelectElement;
    const second = screen.getByLabelText("<b>other</b>:") as HTMLSelectElement;
    await userEvent.selectOptions(first, V1);
    expect(first.value).toBe(V1);
    expect(second.value).toBe("");
    await userEvent.selectOptions(second, "2026-09-20T00:00:00Z"); // picking on one clears the other (3)
    expect(second.value).toBe("2026-09-20T00:00:00Z");
    expect(first.value).toBe("");
  });

  it("shows a shared instant on the first document that offers it only (at most one choice is active)", async () => {
    const fake = installFake();
    fake.extraDocument.value = true;
    fake.extraInstant.value = V1;
    store();
    mount();
    await screen.findByText("memo.txt", HEADING);
    await userEvent.selectOptions(screen.getByLabelText("memo.txt:"), V1);
    expect((screen.getByLabelText("memo.txt:") as HTMLSelectElement).value).toBe(V1);
    expect((screen.getByLabelText("<b>other</b>:") as HTMLSelectElement).value).toBe("");
  });

  it("lets 'current' end an instant but leaves a date the visitor typed", async () => {
    await open();
    const select = screen.getByLabelText("memo.txt:") as HTMLSelectElement;
    await userEvent.selectOptions(select, V2);
    await userEvent.selectOptions(select, "");
    expect(select.value).toBe("");
    const date = screen.getByLabelText(copy.AS_OF_TITLE) as HTMLInputElement;
    await userEvent.type(date, "2026-09-10");
    expect(date.value).toBe("2026-09-10");
    await userEvent.selectOptions(select, "");
    expect(date.value).toBe("2026-09-10");
  });

  it("clears the date field when an instant is picked, so one choice is active", async () => {
    await open();
    const date = screen.getByLabelText(copy.AS_OF_TITLE) as HTMLInputElement;
    await userEvent.type(date, "2026-09-10");
    await userEvent.selectOptions(screen.getByLabelText("memo.txt:"), V1);
    expect(date.value).toBe("");
  });
});

describe("what changed", () => {
  it("says the first version has nothing to compare, with no request", async () => {
    const fake = await open();
    await userEvent.click(screen.getByRole("button", { name: "memo.txt: v1" }));
    expect(screen.getByText(copy.firstVersion(1))).toBeInTheDocument();
    expect(fake.calls.some((c) => c.url.includes("/changes?"))).toBe(false);
  });

  it("loads the report for v2 against v1 with a GET, shows it, and its chips open the drawer from the changes view", async () => {
    const opened = vi.fn();
    store();
    const fake = installFake();
    mount(drawer(opened));
    await screen.findByText("memo.txt", HEADING);
    await userEvent.click(screen.getByRole("button", { name: "memo.txt: v2" }));
    expect(await screen.findByText(copy.comparedHeader(1, 2, 3))).toBeInTheDocument();
    const call = fake.calls.find((c) => c.url.includes("/changes?"));
    expect(call?.url).toBe(`/api/workspace/${ID}/changes?document_id=${DOC}&from=1&to=2`);
    expect(call?.headers).toEqual({ "X-Workspace-Token": TOKEN });
    const chip = document.querySelector(`[data-chip-key^="changes:"]`) as HTMLElement;
    await userEvent.click(chip);
    expect(opened).toHaveBeenCalledWith(CHUNK, chip, "changes", undefined);
  });

  it("shows a failed report as the server's text", async () => {
    const fake = await open();
    fake.changes.value = json({ detail: "no change report for that version pair" }, 404);
    await userEvent.click(screen.getByRole("button", { name: "memo.txt: v2" }));
    expect(await screen.findByText("no change report for that version pair")).toBeInTheDocument();
  });
});

describe("the copy is the legacy page's, character for character (R30)", () => {
  const html = readFileSync(path.resolve(import.meta.dirname, "../../../../src/semigraph/serve/static/index.html"), "utf8");
  const squash = (s: string) => s.replace(/\s+/g, " ");
  const legacy = squash(html);
  const unescape = (s: string) => s.replace(/&ldquo;/g, "\u201c").replace(/&rdquo;/g, "\u201d").replace(/&quot;/g, '"');

  it.each([
    ["PANEL_TITLE", copy.PANEL_TITLE],
    ["INTRO", copy.INTRO],
    ["CREATE_BUTTON", copy.CREATE_BUTTON],
    ["HAVE_ONE", copy.HAVE_ONE],
    ["RESTORE_BUTTON", copy.RESTORE_BUTTON],
    ["DELETE_BUTTON", copy.DELETE_BUTTON],
    ["AS_OF_TITLE", copy.AS_OF_TITLE],
    ["UPLOAD_AS", copy.UPLOAD_AS],
    ["DROP_TEXT", copy.DROP_TEXT],
    ["FILE_ACCEPT", FILE_ACCEPT],
    ["NO_DOCUMENTS", copy.NO_DOCUMENTS],
  ])("%s occurs in index.html", (_name, text) => {
    expect(unescape(legacy)).toContain(squash(text));
  });

  it("keeps the placeholders and the hint of the legacy fields", () => {
    expect(legacy).toContain(`placeholder="${copy.ID_LABEL}"`);
    expect(legacy).toContain(`placeholder="${copy.TOKEN_LABEL}"`);
    expect(legacy).toContain(copy.AS_OF_HINT);
    expect(legacy).toContain(`${copy.WORKSPACE_WORD} <code id="wsIdLabel">`);
    expect(legacy).toContain(copy.EXPIRES_WORD);
    expect(legacy).toContain(`"${copy.EXPIRES_UNKNOWN}"`);
  });

  it("keeps the words of the status, loading and comparison lines", () => {
    expect(legacy).toContain("restoring\u2026");
    expect(legacy).toContain("creating\u2026");
    expect(legacy).toContain("loading what changed between v${older} and v${newer}\u2026");
    expect(legacy).toContain("v${newer} is the first version \u2014 nothing to compare.");
    expect(legacy).toContain("comparison not available (${esc(report.not_compared_reason || \"unknown reason\")})");
    expect(legacy).toContain("section(s) were compared as whole sections, not sentence by sentence for negation changes.");
    expect(legacy).toContain("v${older} \u2192 v${newer}, ${report.unchanged_count} section(s) unchanged:");
    for (const word of [copy.SECTION_ADDED, copy.SECTION_REMOVED, copy.SECTION_CHANGED]) expect(legacy).toContain(`section("${word}"`);
    expect(legacy).toContain(`${copy.SECTION_MINOR} (`);
  });

  it("matches the helpers that build the same lines", () => {
    expect(copy.loadingChanges(1, 2)).toBe("loading what changed between v1 and v2\u2026");
    expect(copy.firstVersion(3)).toBe("v3 is the first version \u2014 nothing to compare.");
    expect(copy.notCompared(1, 2, "x")).toBe("v1 \u2192 v2: comparison not available (x).");
    expect(copy.comparedHeader(1, 2, 5)).toBe("v1 \u2192 v2, 5 section(s) unchanged:");
    expect(copy.skippedNote(4)).toBe("4 section(s) were compared as whole sections, not sentence by sentence for negation changes.");
  });
});
