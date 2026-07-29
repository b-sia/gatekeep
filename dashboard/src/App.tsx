import { useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import DashboardPage from "./pages/DashboardPage";
import { clearStoredApiKey, getStoredApiKey } from "./api/client";

export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);

  function handleUnauthorized() {
    clearStoredApiKey();
    setHasKey(false);
  }

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return <DashboardPage onUnauthorized={handleUnauthorized} />;
}
