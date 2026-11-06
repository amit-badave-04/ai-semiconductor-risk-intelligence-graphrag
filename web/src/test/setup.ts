import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs without globals (explicit imports), so Testing Library's automatic cleanup is wired here.
afterEach(() => {
  cleanup();
  document.title = "";
  document.body.replaceChildren();
});
