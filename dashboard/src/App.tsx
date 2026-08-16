import { useCallback, useEffect, useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import Header, { type TabKey } from "./components/Header";
import DashboardPage from "./pages/DashboardPage";
import ManagementPage from "./pages/ManagementPage";
import { UnauthorizedError, clearStoredApiKey, getMe, getStoredApiKey } from "./api/client";
import type { MeResponse } from "./api/types";

/**
 * Root component: gates the dashboard behind an API key, owns the active tab
 * and the caller's own account context (GET /me), and renders the shared
 * header plus the active tab's page.
 */
export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);
  const [tab, setTab] = useState<TabKey>("analytics");
  const [me, setMe] = useState<MeResponse | null>(null);
  const [meError, setMeError] = useState<string | null>(null);

  /** Clears the stored API key and returns to the key entry screen. */
  function handleUnauthorized() {
    clearStoredApiKey();
    setMe(null);
    setHasKey(false);
  }

  const loadMe = useCallback(() => {
    setMeError(null);
    getMe()
      .then(setMe)
      .catch((err) => {
        if (err instanceof UnauthorizedError) return handleUnauthorized();
        setMeError(err instanceof Error ? err.message : "Failed to load account");
      });
  }, []);

  useEffect(() => {
    if (!hasKey) return;
    loadMe();
  }, [hasKey, loadMe]);

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return (
    <div className="min-h-screen bg-slate-950">
      <Header activeTab={tab} onTabChange={setTab} onClearKey={handleUnauthorized} />
      {tab === "analytics" ? (
        <DashboardPage onUnauthorized={handleUnauthorized} />
      ) : (
        <ManagementPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
          onMeChanged={setMe}
        />
      )}
    </div>
  );
}
