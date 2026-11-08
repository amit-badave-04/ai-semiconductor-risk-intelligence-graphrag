import { useState } from "react";
import { BrowserRouter } from "react-router";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { basenameFor } from "@/lib/basename";
import { AskProvider } from "@/state/AskProvider";
import { TurnstileProvider } from "@/state/TurnstileProvider";
import { WorkspaceProvider } from "@/state/WorkspaceProvider";
import { AppRoutes } from "./routes";

export function App() {
  // Read once: the same build answers under /v2/ and, after the server flips the root, at / (R12).
  const [basename] = useState(() => basenameFor(window.location.pathname));
  // The state that must outlive a route change sits above the router (R31, R33, R34): the bot-check widget and token, the
  // workspace and its uploads, the ask and its stream, and the one evidence drawer. The order is part of the contract: the
  // ask reads the workspace and the token, the drawer reads the workspace.
  return (
    <TurnstileProvider>
      <WorkspaceProvider>
        <AskProvider>
          <EvidenceDrawerProvider>
            <BrowserRouter basename={basename}>
              <AppRoutes />
            </BrowserRouter>
          </EvidenceDrawerProvider>
        </AskProvider>
      </WorkspaceProvider>
    </TurnstileProvider>
  );
}
