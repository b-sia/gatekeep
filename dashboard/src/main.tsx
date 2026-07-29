import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

/** Dashboard SPA entry point: mounts `<App />` into the `#root` element. */
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
