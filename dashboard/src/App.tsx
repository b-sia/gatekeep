import { useCallback, useEffect, useState } from "react";
import IdentityPicker from "./components/IdentityPicker";
import Header, { type TabKey } from "./components/Header";
import DashboardPage from "./pages/DashboardPage";
import ManagementPage from "./pages/ManagementPage";
import { getMe } from "./api/client";
import {
  clearActiveIdentity,
  getActiveIdentity,
  subscribeToRosterChanges,
  type Identity,
} from "./api/identityStore";
import { useApiErrorHandler } from "./hooks/useApiErrorHandler";
import type { MeResponse } from "./api/types";

/**
 * Root component: gates the dashboard behind this tab's active identity, owns
 * the active tab and the caller's own account context (GET /me), and renders
 * the shared header plus the active tab's page.
 */
export default function App() {
  const [activeIdentity, setActiveIdentity] = useState<Identity | null>(() =>
    getActiveIdentity(),
  );
  const [tab, setTab] = useState<TabKey>("analytics");
  const [me, setMe] = useState<MeResponse | null>(null);

  /** Drops this tab back to the picker. `client.ts` has already marked the
   * rejected identity invalid on a 401; here we just clear this tab's
   * pointer and forget the loaded account. */
  const handleUnauthorized = useCallback(() => {
    clearActiveIdentity();
    setMe(null);
    setActiveIdentity(null);
  }, []);

  /** Re-reads the active identity after the picker sets one for this tab. */
  const handleIdentityActivated = useCallback(() => {
    setActiveIdentity(getActiveIdentity());
  }, []);

  // Reconcile this tab's active identity whenever another tab writes to the
  // shared roster (forgets it, invalidates it, or refreshes its account
  // label via re-auth), so the header and dashboard don't keep showing a
  // stale snapshot. If the active identity no longer resolves, this drops
  // the tab back to the picker the same way a 401 does.
  useEffect(() => {
    return subscribeToRosterChanges(() => {
      const current = getActiveIdentity();
      if (!current) {
        clearActiveIdentity();
        setMe(null);
      }
      setActiveIdentity(current);
    });
  }, []);

  const { error: meError, setError: setMeError, handleError } = useApiErrorHandler(handleUnauthorized);

  const loadMe = useCallback(() => {
    setMeError(null);
    getMe()
      .then(setMe)
      .catch((err) => handleError(err, "Failed to load account"));
  }, [setMeError, handleError]);

  useEffect(() => {
    if (!activeIdentity) return;
    loadMe();
  }, [activeIdentity, loadMe]);

  if (!activeIdentity) {
    return <IdentityPicker onIdentityActivated={handleIdentityActivated} />;
  }

  return (
    <div className="min-h-screen bg-slate-950">
      <Header
        activeTab={tab}
        onTabChange={setTab}
        identity={activeIdentity}
        onLogout={handleUnauthorized}
      />
      {tab === "analytics" ? (
        <DashboardPage me={me} onUnauthorized={handleUnauthorized} />
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
