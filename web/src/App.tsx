import { useState } from "react";
import { BrowserRouter } from "react-router";
import { basenameFor } from "@/lib/basename";
import { AppRoutes } from "./routes";

export function App() {
  // Read once: the same build answers under /v2/ and, after the server flips the root, at / (R12).
  const [basename] = useState(() => basenameFor(window.location.pathname));
  return (
    <BrowserRouter basename={basename}>
      <AppRoutes />
    </BrowserRouter>
  );
}
