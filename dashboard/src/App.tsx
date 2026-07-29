import { useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
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

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      Authenticated. Dashboard page wired in Task 12.
      <button onClick={handleUnauthorized} className="ml-2 underline">
        Clear key
      </button>
    </div>
  );
}
