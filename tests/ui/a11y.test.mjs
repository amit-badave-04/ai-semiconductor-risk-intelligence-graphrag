// Keyboard and assistive-technology behaviour of the two widgets that had none (M5 decisions 2.3: "the three known
// accessibility gaps (fixed in place)"): the evidence drawer and the upload drop zone.
//
// The page's REAL inline script runs in a vm against the focus-aware fake DOM in harness.mjs, and the fake elements for
// #drawer, #drawerClose, #wsDrop and #wsFile are built from the page's REAL markup (markupElement), so a markup change
// changes what these tests exercise. The fakes model what matters here: `inert` and `hidden` remove an element from the
// focus order; `aria-hidden` does not (the drawer's old bug); a browser moves focus to <body> when the focused element
// turns inert.
import assert from "node:assert/strict";
import test from "node:test";
import { FakeElement, loadPage, markupElement } from "./harness.mjs";

const CHUNK = "0001045810-26-000021:I.1A:0361";

// ------------------------------------------------------------------------------------------------ the evidence drawer

// A citation chip. Today's real chips are plain spans (not in the tab order); `focusable` models a chip that can hold focus,
// which is what makes "focus returns to the opener" observable.
function chip({ focusable = true, id = CHUNK } = {}) {
  const el = new FakeElement("", { tag: "span", attrs: { class: "cite", ...(focusable ? { tabindex: "0" } : {}) } });
  el.dataset.id = id;
  return el;
}

function drawerPage({ fetch } = {}) {
  const drawer = markupElement("drawer");
  const drawerClose = markupElement("drawerClose", { parent: drawer });
  const answer = new FakeElement("answer"), wsChanges = new FakeElement("wsChanges");
  const q = new FakeElement("q", { tag: "textarea" });
  const page = loadPage({ elements: { drawer, drawerClose, answer, wsChanges, q }, fetch });
  const attach = (...els) => { for (const el of els) el.ownerDocument = page.document; return els[0]; };
  attach(drawer, drawerClose, answer, wsChanges, q);
  const tabbables = (...extra) => [drawerClose, q, ...extra].filter((el) => el.isTabbable());
  return { ...page, drawer, drawerClose, answer, wsChanges, q, attach, tabbables };
}

const press = (page, key, extra = {}) => page.document.dispatch("keydown", { key, ...extra });

test("first load: the drawer is closed in the markup and nothing inside it can be reached by Tab or by assistive technology", () => {
  const p = drawerPage();
  assert.equal(p.drawer.hasAttribute("inert"), true, "the markup carries `inert`, so the state is right before any script runs");
  assert.equal(p.drawer.getAttribute("aria-hidden"), "true");
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.drawerClose.isFocusable(), false, "the close button inside a closed drawer cannot take focus");
  assert.equal(p.drawerClose.isTabbable(), false);
  assert.equal(p.drawerClose.hiddenFromAT(), true);
  assert.deepEqual(p.tabbables().filter((el) => p.drawer.contains(el)), []);
  p.drawerClose.focus();
  assert.notEqual(p.document.activeElement, p.drawerClose, "focus() on it does nothing while the drawer is closed");
});

test("opening from a citation moves focus into the drawer and exposes it to assistive technology", () => {
  const p = drawerPage();
  const opener = p.attach(chip());
  opener.focus();
  p.answer.dispatch("click", { target: opener });
  assert.equal(p.drawer.classList.contains("open"), true);
  assert.equal(p.drawer.hasAttribute("inert"), false);
  assert.notEqual(p.drawer.getAttribute("aria-hidden"), "true", "an open drawer must not stay aria-hidden");
  assert.equal(p.drawer.hiddenFromAT(), false);
  assert.equal(p.drawerClose.isTabbable(), true);
  assert.equal(p.document.activeElement, p.drawerClose, "focus moved into the drawer, to its close button");
});

test("a citation in the what-changed view opens the drawer the same way", () => {
  const p = drawerPage();
  const opener = p.attach(chip({ id: "doc:0123456789ab:v1:0001" }));
  opener.focus();
  p.wsChanges.dispatch("click", { target: opener });
  assert.equal(p.drawer.classList.contains("open"), true);
  assert.equal(p.document.activeElement, p.drawerClose);
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, opener);
});

test("closing with the close button returns focus to the opener and takes the drawer out of the tab order again", () => {
  const p = drawerPage();
  const opener = p.attach(chip());
  opener.focus();
  p.answer.dispatch("click", { target: opener });
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, opener, "focus is back on the chip that opened the drawer");
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.drawer.hasAttribute("inert"), true);
  assert.equal(p.drawer.getAttribute("aria-hidden"), "true");
  assert.equal(p.drawerClose.isTabbable(), false);
  assert.deepEqual(p.tabbables(opener).filter((el) => p.drawer.contains(el)), []);
});

test("Escape closes the drawer, returns focus to the opener and re-hides the drawer", () => {
  const p = drawerPage();
  const opener = p.attach(chip());
  opener.focus();
  p.answer.dispatch("click", { target: opener });
  press(p, "Escape");
  assert.equal(p.document.activeElement, opener);
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.drawer.hasAttribute("inert"), true);
  assert.equal(p.drawer.getAttribute("aria-hidden"), "true");
  assert.equal(p.drawerClose.isTabbable(), false);
});

test("Escape with the drawer closed does nothing, and a stale opener is never refocused", () => {
  const p = drawerPage();
  const opener = p.attach(chip()), elsewhere = p.attach(chip({ id: "xbrl:1045810:revenue:2026-01-25" }));
  elsewhere.focus();
  assert.doesNotThrow(() => press(p, "Escape"));
  assert.equal(p.document.activeElement, elsewhere, "Escape on a drawer that was never opened must not move focus");
  opener.focus();
  p.answer.dispatch("click", { target: opener });
  p.drawerClose.dispatch("click");                 // closed: focus back on the opener
  elsewhere.focus();
  press(p, "Escape");                              // a second Escape, nothing is open
  assert.equal(p.document.activeElement, elsewhere, "the opener of the previous open is forgotten once the drawer closes");
});

test("Escape while focus is on another control closes the drawer without pulling focus away from it", () => {
  const p = drawerPage();
  const opener = p.attach(chip());
  opener.focus();
  p.answer.dispatch("click", { target: opener });
  p.q.focus();                                     // the user went back to the question box while the drawer stayed open
  press(p, "Escape");
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.drawer.hasAttribute("inert"), true);
  assert.equal(p.document.activeElement, p.q);
});

test("a second citation clicked while the drawer is open becomes the element focus returns to", () => {
  const p = drawerPage();
  const first = p.attach(chip()), second = p.attach(chip({ id: "fr:2026-19537" }));
  first.focus();
  p.answer.dispatch("click", { target: first });
  second.focus();
  p.answer.dispatch("click", { target: second });
  assert.equal(p.document.activeElement, p.drawerClose);
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, second);
});

test("with today's non-focusable chips the drawer still opens and closes cleanly and leaves nothing hidden in the tab order", () => {
  const p = drawerPage();
  const opener = chip({ focusable: false });
  p.answer.dispatch("click", { target: opener });
  assert.equal(p.document.activeElement, p.drawerClose);
  assert.doesNotThrow(() => p.drawerClose.dispatch("click"));
  assert.equal(p.drawer.hasAttribute("inert"), true);
  assert.equal(p.drawerClose.isTabbable(), false);
});

test("a click on something that is not a citation does not open the drawer", () => {
  const p = drawerPage();
  const other = new FakeElement("", { tag: "span" });
  p.answer.dispatch("click", { target: other });
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.drawer.hasAttribute("inert"), true);
});

test("Ctrl+Enter still submits the question; a bare Enter or Escape does not", () => {
  const calls = [];
  const p = drawerPage({ fetch: (url) => { calls.push(url); return new Promise(() => {}); } });
  p.q.value = "Which rules affect Nvidia?";
  press(p, "Enter");
  press(p, "Escape");
  assert.equal(calls.includes("/api/ask"), false);
  press(p, "Enter", { ctrlKey: true });
  assert.equal(calls.filter((u) => u === "/api/ask").length, 1);
});

test("the close button has an accessible name beyond the multiplication sign", () => {
  assert.match(markupElement("drawerClose").getAttribute("aria-label") || "", /close/i);
});

// ------------------------------------------------------------------------------------------------ the upload drop zone

function dropPage() {
  const wsDrop = markupElement("wsDrop"), wsFile = markupElement("wsFile");
  const page = loadPage({ elements: { wsDrop, wsFile } });
  wsDrop.ownerDocument = wsFile.ownerDocument = page.document;
  const handled = [];
  page.context.handleFiles = (files) => { handled.push(files); };   // the page's listeners call the global at event time
  return { ...page, wsDrop, wsFile, handled };
}

test("the drop zone is a keyboard tab stop with a button role; the hidden file input is not a second one", () => {
  const p = dropPage();
  assert.equal(p.wsDrop.getAttribute("role"), "button");
  assert.equal(p.wsDrop.isTabbable(), true, "tabindex=0 puts the drop zone in the Tab sequence");
  assert.equal(p.wsFile.isTabbable(), false);
});

test("Enter on the drop zone opens the file chooser once, and the key is consumed", () => {
  const p = dropPage();
  const e = p.wsDrop.dispatch("keydown", { key: "Enter" });
  assert.equal(p.wsFile.clicks, 1);
  assert.equal(e.defaultPrevented, true);
});

test("Space on the drop zone opens the file chooser once and does not scroll the page", () => {
  const p = dropPage();
  const e = p.wsDrop.dispatch("keydown", { key: " " });
  assert.equal(p.wsFile.clicks, 1);
  assert.equal(e.defaultPrevented, true, "the default action of Space (scrolling the page) is suppressed");
});

for (const [name, event] of [
  ["Tab (no keyboard trap)", { key: "Tab" }], ["Shift+Tab", { key: "Tab", shiftKey: true }], ["Escape", { key: "Escape" }],
  ["a letter", { key: "a" }], ["an arrow", { key: "ArrowDown" }],
  ["Ctrl+Enter (the ask shortcut)", { key: "Enter", ctrlKey: true }], ["Cmd+Enter", { key: "Enter", metaKey: true }],
  ["Alt+Enter", { key: "Enter", altKey: true }], ["a held-down Enter", { key: "Enter", repeat: true }],
]) {
  test(`${name} on the drop zone does not open the file chooser and is not consumed`, () => {
    const p = dropPage();
    const e = p.wsDrop.dispatch("keydown", event);
    assert.equal(p.wsFile.clicks, 0);
    assert.equal(e.defaultPrevented, false);
  });
}

test("a mouse click on the drop zone still opens the file chooser exactly once", () => {
  const p = dropPage();
  p.wsDrop.dispatch("click");
  assert.equal(p.wsFile.clicks, 1);
});

test("drag and drop on the zone behave as before", () => {
  const p = dropPage();
  for (const type of ["dragover", "dragenter"]) {
    const e = p.wsDrop.dispatch(type);
    assert.equal(e.defaultPrevented, true, `${type} is cancelled so the browser lets the file drop`);
    assert.equal(p.wsDrop.classList.contains("drag"), true);
    p.wsDrop.dispatch("dragleave");
    assert.equal(p.wsDrop.classList.contains("drag"), false);
  }
  p.wsDrop.dispatch("dragover");
  const files = [{ name: "a.pdf" }];
  const drop = p.wsDrop.dispatch("drop", { dataTransfer: { files } });
  assert.equal(drop.defaultPrevented, true);
  assert.equal(p.wsDrop.classList.contains("drag"), false);
  assert.deepEqual(p.handled, [files], "the dropped files go to handleFiles");
  assert.equal(p.wsFile.clicks, 0, "a drop never opens the chooser");
});

test("choosing files in the chooser still uploads them", () => {
  const p = dropPage();
  const files = [{ name: "b.pdf" }];
  p.wsFile.dispatch("change", { target: { files } });
  assert.deepEqual(p.handled, [files]);
});
