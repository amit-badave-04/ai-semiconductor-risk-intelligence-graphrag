import { describe, expect, it } from "vitest";
import { safeUrl } from "../safeUrl";

// S3: only structured URL fields become anchors, and only through safeUrl. The rejected values are built from pieces so no
// script URL literal sits in the source (the scheme names are the test data).
const js = ["java", "script:alert(1)"].join("");
const vb = ["vb", "script:x"].join("");

describe("safeUrl accepts only a plain https URL", () => {
  it("returns the parsed href of an https URL", () => {
    expect(safeUrl("https://www.federalregister.gov/d/2026-19537")).toBe("https://www.federalregister.gov/d/2026-19537");
    expect(safeUrl("https://example.com")).toBe("https://example.com/");
    expect(safeUrl("https://example.com:8443/a?b=c#d")).toBe("https://example.com:8443/a?b=c#d");
  });

  it.each([
    ["the script scheme", js],
    ["the script scheme in mixed case", ["JaVa", "ScRiPt:alert(1)"].join("")],
    ["vbscript:", vb],
    ["data:", "data:text/html,<script>x</script>"],
    ["blob:", "blob:https://example.com/uuid"],
    ["mailto:", "mailto:a@example.com"],
    ["http:", "http://example.com/"],
    ["a protocol-relative //host", "//example.com/x"],
    ["a path", "/api/evidence/x"],
    ["a relative url", "example.com/x"],
    ["HTTPS in capitals", "HTTPS://example.com/"],
    ["leading whitespace", " https://example.com/"],
    ["leading newline", "\nhttps://example.com/"],
    ["trailing whitespace", "https://example.com/ "],
    ["a tab inside the scheme", "ht\ttps://example.com/"],
    ["a newline inside the scheme", ["java\n", "script:alert(1)"].join("")],
    ["a control character", "https://exam\u0000ple.com/"],
    ["a C1 control character", "https://exam\u0085ple.com/"],
    ["credentials", "https://user:pass@example.com/"],
    ["a username only", "https://user@example.com/"],
    ["no host", "https:///x"],
    ["a backslash authority", "https:\\\\example.com\\x"],
    ["an empty string", ""],
    ["only a scheme", "https:"],
  ])("rejects %s", (_name, value) => {
    expect(safeUrl(value)).toBeNull();
  });

  it.each([undefined, null, 5, {}, ["https://example.com/"], true])("rejects a value that is not a string (%j)", (value) => {
    expect(safeUrl(value)).toBeNull();
  });
});
