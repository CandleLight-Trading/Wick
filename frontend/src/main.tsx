import React from "react";
import ReactDOM from "react-dom/client";
import { ClerkProvider } from "@clerk/react";
import App from "./App";
import { CLERK_KEY } from "./api";
import "./index.css";

// With a publishable key Wick runs on Clerk accounts (accounts.candlelit.us in production).
// Without one, the shared-password or open dev mode applies and nothing else changes.

const app = <App />;
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {CLERK_KEY ? (
      <ClerkProvider publishableKey={CLERK_KEY} afterSignOutUrl={location.origin} appearance={{ variables: { colorPrimary: "#34d399" } }}>
        {app}
      </ClerkProvider>
    ) : app}
  </React.StrictMode>,
);
