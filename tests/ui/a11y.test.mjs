// Keyboard and assistive-technology behaviour of the widgets that had none (M5 decisions 2.3: "the three known
// accessibility gaps (fixed in place)"; M5b I0b: the rest of what 208364a left open): the citation chips, the evidence
// drawer, the status line, the question label and the upload drop zone.
//
// The page's REAL inline script runs in a vm against the focus-aware fake DOM in harness.mjs, and the fake elements for
// #drawer, #drawerClose, #wsDrop and #wsFile are built from the page's REAL markup (markupElement), and a chip from what
// the page's REAL renderer emits, so a markup change changes what these tests exercise. The fakes model what matters
// here: `inert` and `hidden` remove an element from the focus order; `aria-hidden` does not (the drawer's old bug); a
// browser moves focus to <body> when the focused element turns inert.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { FakeElement, INDEX_PATH, loadPage, markupElement, markupOf } from "./harness.mjs";

const CHUNK = "0001045810-26-000021:I.1A:0361";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2026-19537";
const DOC = "doc:0123456789ab:v1:0001";

// ------------------------------------------------------------------------------------------------ the citation chips

// A citation chip is built from the markup the page's own renderers emit (the answer's renderMarkdown and the what-changed
// view's changeItemHtml), never from a hand-written attribute list.
const { api: pageApi } = loadPage();
const SOURCES = {
  answer: (id) => pageApi.renderMarkdown(`See [${id}].`),
  changes: (id) => pageApi.changeItemHtml({ headline: "h", passages: [{ quote: "q", chunk_id: id }] }),
};
const chipHtml = (id, source) => SOURCES[source](id).match(/<span [^>]*\bclass="cite"[^>]*>[^<]*<\/span>/)[0];
const attrsOf = (tagHtml) => Object.fromEntries([...tagHtml.match(/<span ([^>]*)>/)[1].matchAll(/([a-z-]+)="([^"]*)"/g)].map((m) => [m[1], m[2]]));

// The what-changed view takes chunk_id raw from the API and the chip goes into innerHTML (I0b moved its escaping into the
// shared chipHtml; the Opus verifier found no test that a hostile id stays text).
test("a hostile chunk id in the what-changed view stays text in the chip's attributes and label", () => {
  const hostile = 'x"><img src=x onerror=alert(1)>';
  const html = pageApi.changeItemHtml({ headline: "h", passages: [{ quote: "q", chunk_id: hostile }] });
  assert.ok(!html.includes("<img"), html);
  assert.ok(!html.includes('x">'), html);
  assert.ok(html.includes("&lt;img"), html);
});

// A FakeElement that also models what the drawer's focus return needs: `isConnected` (false once a re-render has replaced
// the element, as `innerHTML = ...` does) and script focus on a tabindex="-1" element (the fake's base rule only admits
// tabindex >= 0; a browser lets script focus a -1 element and keeps it out of the Tab sequence).
class PageElement extends FakeElement {
  isConnected = true;
  isFocusable() { return this.getAttribute("tabindex") === "-1" ? !this.hidden && !this.isInert() : super.isFocusable(); }
}

// #answer or #wsChanges: render() replaces every chip element in it, like a re-render of its innerHTML.
class Container extends PageElement {
  chips = [];
  render(...chips) {
    for (const old of this.chips) { old.isConnected = false; old.parent = null; }
    this.chips = chips;
    for (const c of chips) { c.parent = this; c.isConnected = true; c.ownerDocument = this.ownerDocument; }
  }
  querySelectorAll(selector) { return selector === ".cite" ? [...this.chips] : []; }
}

// `focusable: false` models an opener that cannot hold focus (none of the page's own chips, now).
function chip({ focusable = true, id = CHUNK, source = "answer" } = {}) {
  const attrs = attrsOf(chipHtml(id, source));
  if (!focusable) delete attrs.tabindex;
  const el = new PageElement("", { tag: "span", attrs });
  el.dataset.id = id;
  return el;
}

test("every citation chip the page renders is a tab stop with a button role and a visible name, in the answer and in the what-changed view", () => {
  for (const source of Object.keys(SOURCES)) {
    for (const id of [CHUNK, XBRL, FR, DOC]) {
      const html = chipHtml(id, source), attrs = attrsOf(html), el = chip({ id, source });
      assert.equal(attrs.role, "button", `${source} ${id}: role`);
      assert.equal(attrs.tabindex, "0", `${source} ${id}: tabindex`);
      assert.equal(el.isTabbable(), true, `${source} ${id}: in the Tab sequence`);
      assert.match(html, />[^<]+<\/span>$/, `${source} ${id}: the chip's text is its accessible name`);
      assert.equal(attrs["data-id"], id);
    }
  }
});

// ------------------------------------------------------------------------------------------------ the evidence drawer

// `heading: true` adds the "Ask the graph" heading (a real-markup element), the focus target of a stale opener.
function drawerPage({ fetch, heading = false } = {}) {
  const drawer = markupElement("drawer");
  const drawerClose = markupElement("drawerClose", { parent: drawer });
  const answer = new Container("answer"), wsChanges = new Container("wsChanges");
  const q = new FakeElement("q", { tag: "textarea" });
  const askHeading = heading ? new PageElement("askHeading", markupOf("askHeading")) : null;
  const page = loadPage({ elements: { drawer, drawerClose, answer, wsChanges, q, ...(askHeading ? { askHeading } : {}) }, fetch });
  const attach = (...els) => { for (const el of els) el.ownerDocument = page.document; return els[0]; };
  attach(drawer, drawerClose, answer, wsChanges, q, ...(askHeading ? [askHeading] : []));
  const tabbables = (...extra) => [drawerClose, q, ...extra].filter((el) => el.isTabbable());
  return { ...page, drawer, drawerClose, answer, wsChanges, q, askHeading, attach, tabbables };
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
  const opener = p.attach(chip({ id: DOC, source: "changes" }));
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

test("with an opener that cannot hold focus the drawer still opens and closes cleanly and leaves nothing hidden in the tab order", () => {
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

// ---- a citation chip is operated from the keyboard: Enter on keydown, Space on keyup (like a native button). Space cannot
// open on keydown: the drawer takes focus at once, and in some browsers the keyup then lands on the drawer's close button
// and clicks it, so the drawer would open and close again in one keystroke.

const keydown = (container, target, key, extra = {}) => container.dispatch("keydown", { target, key, ...extra });
const keyup = (container, target, key, extra = {}) => container.dispatch("keyup", { target, key, ...extra });

for (const where of ["answer", "wsChanges"]) {
  const source = where === "answer" ? "answer" : "changes";
  const focusedChip = (p, options = {}) => { const opener = chip({ source, ...options }); p[where].render(opener); opener.focus(); return opener; };

  test(`Enter on a focused citation chip in #${where} opens the evidence drawer, moves focus into it, and is consumed`, () => {
    const p = drawerPage();
    const opener = focusedChip(p);
    assert.equal(p.document.activeElement, opener, "the chip can hold focus");
    const e = keydown(p[where], opener, "Enter");
    assert.equal(p.drawer.classList.contains("open"), true);
    assert.equal(p.drawer.hasAttribute("inert"), false);
    assert.equal(p.document.activeElement, p.drawerClose);
    assert.equal(e.defaultPrevented, true);
    press(p, "Escape");
    assert.equal(p.document.activeElement, opener, "and Escape gives focus back to the chip");
  });

  test(`Space on a focused citation chip in #${where}: the keydown is consumed (no page scroll), the keyup opens the drawer`, () => {
    const p = drawerPage();
    const opener = focusedChip(p);
    const down = keydown(p[where], opener, " ");
    assert.equal(down.defaultPrevented, true, "the default action of Space (scrolling the page) is suppressed");
    assert.equal(p.drawer.classList.contains("open"), false, "nothing opens until the key is released");
    assert.equal(p.document.activeElement, opener);
    keyup(p[where], opener, " ");
    assert.equal(p.drawer.classList.contains("open"), true);
    assert.equal(p.document.activeElement, p.drawerClose);
  });

  for (const [name, event] of [
    ["Tab (no keyboard trap)", { key: "Tab" }], ["Shift+Tab", { key: "Tab", shiftKey: true }], ["Escape", { key: "Escape" }],
    ["a letter", { key: "a" }], ["an arrow", { key: "ArrowDown" }],
    ["Ctrl+Enter (the ask shortcut)", { key: "Enter", ctrlKey: true }], ["Cmd+Enter", { key: "Enter", metaKey: true }],
    ["Alt+Enter", { key: "Enter", altKey: true }], ["a held-down Enter", { key: "Enter", repeat: true }],
    ["a held-down Space", { key: " ", repeat: true }], ["Ctrl+Space", { key: " ", ctrlKey: true }],
  ]) {
    test(`${name} on a citation chip in #${where} does not open the drawer and is not consumed`, () => {
      const p = drawerPage();
      const opener = focusedChip(p);
      const down = keydown(p[where], opener, event.key, event);
      const up = keyup(p[where], opener, event.key, event);
      assert.equal(p.drawer.classList.contains("open"), false);
      assert.equal(p.drawer.hasAttribute("inert"), true);
      assert.equal(down.defaultPrevented, false);
      assert.equal(up.defaultPrevented, false);
    });
  }

  test(`releasing Enter or Tab on a citation chip in #${where} does not open the drawer (only Space opens on keyup)`, () => {
    const p = drawerPage();
    const opener = focusedChip(p);
    keyup(p[where], opener, "Enter");
    keyup(p[where], opener, "Tab");
    assert.equal(p.drawer.classList.contains("open"), false);
  });

  test(`a key pressed on something in #${where} that is not a citation does not open the drawer or get consumed`, () => {
    const p = drawerPage();
    const other = new PageElement("", { tag: "span" });
    for (const key of ["Enter", " "]) {
      assert.equal(keydown(p[where], other, key).defaultPrevented, false);
      assert.equal(keyup(p[where], other, key).defaultPrevented, false);
    }
    assert.equal(p.drawer.classList.contains("open"), false);
  });
}

test("a bare click still opens the drawer from a chip, as before", () => {
  const p = drawerPage();
  const opener = chip(); p.answer.render(opener); opener.focus();
  p.answer.dispatch("click", { target: opener });
  assert.equal(p.drawer.classList.contains("open"), true);
  assert.equal(p.document.activeElement, p.drawerClose);
});

// ---- a stale opener: the chip that opened the drawer was replaced by a re-render while the drawer was open (every delta of
// a streaming answer re-renders all of #answer, so a chip opened mid-stream is always replaced)

for (const where of ["answer", "wsChanges"]) {
  const source = where === "answer" ? "answer" : "changes";
  const mk = (id = CHUNK) => chip({ id, source });

  test(`a chip in #${where} replaced by a re-render: focus returns to the same chip in the re-rendered text`, () => {
    const p = drawerPage({ heading: true });
    const opener = mk(); p[where].render(opener); opener.focus();
    keydown(p[where], opener, "Enter");
    const rerendered = mk(); p[where].render(rerendered);          // the next delta replaces every chip element
    assert.equal(opener.isConnected, false);
    p.drawerClose.dispatch("click");
    assert.equal(p.document.activeElement, rerendered, "focus went to the chip that is on the page now, not nowhere");
    assert.equal(p.drawer.hasAttribute("inert"), true);
  });

  test(`a re-render of #${where} that appended more text and chips keeps the opener's place (id and occurrence)`, () => {
    const p = drawerPage({ heading: true });
    const [first, second, other] = [mk(), mk(), mk(FR)];
    p[where].render(first, other, second); second.focus();
    keydown(p[where], second, "Enter");                               // the SECOND occurrence of CHUNK
    const [a, b, c, d] = [mk(), mk(FR), mk(), mk(XBRL)];
    p[where].render(a, b, c, d);
    press(p, "Escape");
    assert.equal(p.document.activeElement, c, "the second CHUNK chip, not the first");
  });
}

test("the opener is gone and the answer is a NEW one: focus goes to the Ask heading, not to a look-alike chip and not to <body>", () => {
  const p = drawerPage({ heading: true });
  const opener = chip(); p.answer.render(opener); opener.focus();
  keydown(p.answer, opener, "Enter");
  p.q.value = "A different question"; p.api.ask();                      // a new ask clears the answer
  const lookAlike = chip(); p.answer.render(lookAlike);                // and the new answer happens to cite the same id
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, p.askHeading);
  assert.notEqual(p.document.activeElement, lookAlike);
});

test("the opener is gone and the same answer no longer has that chip: focus goes to the Ask heading", () => {
  const p = drawerPage({ heading: true });
  const opener = chip(); p.answer.render(opener); opener.focus();
  keydown(p.answer, opener, "Enter");
  p.answer.render();                                                    // no chip left
  press(p, "Escape");
  assert.equal(p.document.activeElement, p.askHeading);
});

test("a new what-changed report replaced the opener: focus goes to the Ask heading, not to a look-alike chip", () => {
  const p = drawerPage({ heading: true });
  const opener = chip({ id: DOC, source: "changes" }); p.wsChanges.render(opener); opener.focus();
  keydown(p.wsChanges, opener, "Enter");
  p.context.showChanges("0123456789ab", 1, 2);                          // the next report replaces the old one
  const lookAlike = chip({ id: DOC, source: "changes" }); p.wsChanges.render(lookAlike);
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, p.askHeading);
});

// The drawer is non-modal, so the other container can be replaced while it is open. A replacement of #wsChanges (a compare
// click) must not make the remembered place of an #answer chip look stale, and a new ask must not do that to a #wsChanges chip.
test("a what-changed report loading while the drawer is open from an #answer chip does not lose that chip's place", () => {
  const p = drawerPage({ heading: true });
  const opener = chip(); p.answer.render(opener); opener.focus();
  keydown(p.answer, opener, "Enter");
  p.context.showChanges("0123456789ab", 1, 2);                          // a version compare, clicked in the non-modal drawer's page
  const rerendered = chip(); p.answer.render(rerendered);                // the next delta of the streaming answer replaces every chip
  assert.equal(opener.isConnected, false);
  p.drawerClose.dispatch("click");
  assert.equal(p.document.activeElement, rerendered, "the same chip, though #wsChanges changed meanwhile");
});

test("a new ask while the drawer is open from a #wsChanges chip does not lose that chip's place", () => {
  const p = drawerPage({ heading: true });
  const opener = chip({ id: DOC, source: "changes" }); p.wsChanges.render(opener); opener.focus();
  keydown(p.wsChanges, opener, "Enter");
  p.q.value = "A different question"; p.api.ask();                      // a new ask replaces #answer, not the report
  const rerendered = chip({ id: DOC, source: "changes" }); p.wsChanges.render(rerendered);
  assert.equal(opener.isConnected, false);
  press(p, "Escape");
  assert.equal(p.document.activeElement, rerendered);
});

test("a stale opener never pulls focus away from a control the user moved to", () => {
  const p = drawerPage({ heading: true });
  const opener = chip(); p.answer.render(opener); opener.focus();
  keydown(p.answer, opener, "Enter");
  p.answer.render(chip());
  p.q.focus();
  press(p, "Escape");
  assert.equal(p.drawer.classList.contains("open"), false);
  assert.equal(p.document.activeElement, p.q);
});

test("the Ask heading takes focus from script but is not a tab stop", () => {
  const { tag, attrs } = markupOf("askHeading");
  assert.equal(tag, "h2");
  assert.equal(attrs.tabindex, "-1");
  const p = drawerPage({ heading: true });
  assert.equal(p.askHeading.isTabbable(), false);
  p.askHeading.focus();
  assert.equal(p.document.activeElement, p.askHeading);
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

test("the evidence drawer is a named complementary landmark, still inert and hidden from assistive technology while closed", () => {
  const { tag, attrs } = markupOf("drawer");
  assert.equal(tag, "aside", "an <aside> is the complementary landmark");
  assert.equal(attrs["aria-label"], "Evidence", "the landmark is named");
  assert.equal("inert" in attrs, true);
  assert.equal(attrs["aria-hidden"], "true");
  assert.equal("role" in attrs, false, "no role override: it must stay the complementary landmark");
  const p = drawerPage();
  assert.equal(p.drawer.getAttribute("aria-label"), "Evidence");
  p.answer.dispatch("click", { target: p.attach(chip()) });
  assert.equal(p.drawer.getAttribute("aria-label"), "Evidence", "opening does not rename it");
  assert.equal(p.drawer.hiddenFromAT(), false, "open: the named landmark is in the accessibility tree");
});

// ------------------------------------------------------------------------------------------------ the status line and the question label

test("#status is a polite live region in the markup the browser parses, so a change to it is announced", () => {
  const { attrs } = markupOf("status");
  assert.equal(attrs["aria-live"], "polite");
  assert.equal(attrs.role, "status");
});

test("setStatus writes into #status, the live region (an error and a plain message)", () => {
  const status = new FakeElement("status");
  const p = loadPage({ elements: { status } });
  p.context.setStatus("retrieving from the graph…");
  assert.equal(status.innerHTML, "retrieving from the graph…");
  p.context.setStatus("network error", "err");
  assert.equal(status.innerHTML, '<span class="err">network error</span>');
});

test("the streamed answer text is not a live region (only the status line and the steps timeline announce)", () => {
  const { attrs } = markupOf("answer");
  for (const name of ["aria-live", "aria-atomic", "aria-relevant"]) assert.equal(name in attrs, false, name);
  assert.equal(["status", "alert", "log", "marquee", "timer"].includes(attrs.role), false);
  assert.equal(markupOf("agentSteps").attrs["aria-label"], "What the research agent did", "the steps timeline keeps its own name");
  const html = readFileSync(INDEX_PATH, "utf8");
  assert.match(html, /<div class="steps" aria-live="polite" aria-relevant="additions">/, "and its polite live region");
});

test("the question box has a programmatic label that stays in the accessibility tree", () => {
  const html = readFileSync(INDEX_PATH, "utf8").replace(/<script>[\s\S]*?<\/script>/, "");
  const labels = [...html.matchAll(/<label\b([^>]*)>([\s\S]*?)<\/label>/g)].filter((m) => /\sfor="q"/.test(" " + m[1]));
  assert.equal(labels.length, 1, "exactly one <label for=q>");
  const [, attrs, text] = labels[0];
  assert.match(text.replace(/<[^>]+>/g, "").trim(), /\w/, "the label has text");
  assert.doesNotMatch(attrs, /\b(hidden|aria-hidden|style)\b/, "visually hidden by a CSS class, never by hidden / display:none (they drop it from the tree)");
  assert.equal(markupOf("q").tag, "textarea");
  assert.equal(html.indexOf(labels[0][0]) < html.indexOf('id="q"'), true, "the label comes before the box");
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
