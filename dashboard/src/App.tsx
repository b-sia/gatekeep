import { useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import DashboardPage from "./pages/DashboardPage";
import { clearStoredApiKey, getStoredApiKey } from "./api/client";

/**
 * Root component: gates the dashboard behind an API key. Renders the key
 * entry screen until a key is stored, then the dashboard itself; any 401
 * from the dashboard API clears the stored key and drops back to entry.
 */
export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);

  /** Clears the stored API key and returns to the key entry screen, e.g.
   * after a 401 or the user clicking "API key" to replace it. */
  function handleUnauthorized() {
    clearStoredApiKey();
    setHasKey(false);
  }

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return <DashboardPage onUnauthorized={handleUnauthorized} />;
}
