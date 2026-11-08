// The text corpus shared by the S2 allow-list test and the transitional differential test of AnswerText (R21): every hand-written
// hostile string of S2, plus a seeded generator of markdown-shaped noise. Test code only.
export const CHUNK = "0001045810-26-000021:I.1A:0361";
export const XBRL = "xbrl:1045810:revenue:2026-01-25";
export const FR = "fr:2026-19537";
export const DOC = "doc:0123456789ab:v2:0007";

export const HOSTILE: readonly string[] = [
  "<img src=x onerror=alert(1)>",
  "<script>alert(1)</script>",
  `<script>alert(1)</script> <img src=x onerror="alert(2)"> [${CHUNK}]`,
  "[click me](javascript:alert(1))",
  "[x](data:text/html,<b>hi</b>)",
  '<a href="javascript:alert(1)">x</a>',
  `<iframe ${"src"}${"doc"}="<script>1</script>"></iframe>`,
  "<svg onload=alert(1)>",
  "]]> <![CDATA[ x ]]>",
  "bidi \u202e reversed \u202c and \u2066 isolates \u2069",
  "zero\u200bwidth\u200c\u200dchars\ufeff",
  "** unbalanced bold",
  "**one** and ** two",
  "****",
  "*****",
  "**a****b**",
  "**[" + CHUNK + "**]",
  "**see [" + CHUNK + "] twice [" + CHUNK + "]**",
  "[",
  "[[[[]]]]",
  "]",
  '[xbrl:1:a:2026-01-01"onclick="x]',
  `[${CHUNK}"]`,
  `[${CHUNK}\n]`,
  `[${CHUNK}, ${XBRL}]`,
  `[ ${CHUNK}]`,
  `[${CHUNK.toLowerCase()}]`,
  "[0001045810-26-0000:I.1A:0361]",
  `[${"0".repeat(40)}-26-000021:I.1A:0361]`,
  `[${CHUNK}][${CHUNK}][${XBRL}][${FR}][${DOC}]`,
  `Revenue was $215.9B [${XBRL}], see risk [${CHUNK}] and the rule [${FR}], your file [${DOC}].`,
  "# Heading with **bold** and [" + FR + "]",
  "#### four hashes\n##### five hashes\n#no space\n# ",
  "#",
  "- one\n* two\n  - three\n-no space\n- \n-",
  "- item [" + CHUNK + "]\n\n- item **b**\nplain after list",
  "line one\r\nline two\r\n\r\n",
  "tab\there   \t\n   leading spaces\n\t- tabbed bullet",
  "a\u2028b\u2029c",
  "\u2028- not a bullet\u2029",
  "&amp; &lt; &#39; \" ' < > &",
  "<b>real tags</b> **md bold**",
  "[" + "[".repeat(5000),
  `${"x".repeat(60_000)} [${CHUNK}] ${"y".repeat(60_000)}`,
  "**".repeat(3000),
  "- ".repeat(2000),
  `[${CHUNK}] `.repeat(500),
  "",
  "   ",
  "\n\n\n",
];

const PIECES = [
  "**", "*", "- ", "* ", "# ", "## ", "#### ", "##### ", "[", "]", "\n", "\n\n", " ", "  ", "\r", "\u2028", "<", ">", "&", '"', "'",
  "word", "Nvidia", "$1.5B", "x", "-", "#", ".", ":",
  `[${CHUNK}]`, `[${XBRL}]`, `[${FR}]`, `[${DOC}]`, CHUNK, `[${CHUNK}`, `${XBRL}]`, "[fr:C1-2026-16628]", "[doc:0123456789ab:v12:0001]",
  "<script>", "</b>", "<b>",
];

function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** `count` strings of 1..`maxPieces` markdown-significant pieces, the same on every run for one seed. */
export function generated(count: number, seed = 20261108, maxPieces = 18): string[] {
  const random = mulberry32(seed);
  const out: string[] = [];
  for (let i = 0; i < count; i += 1) {
    const length = 1 + Math.floor(random() * maxPieces);
    let text = "";
    for (let j = 0; j < length; j += 1) text += PIECES[Math.floor(random() * PIECES.length)];
    out.push(text);
  }
  return out;
}
