import { useCallback, useEffect, useState } from "react";
import Header, { type TabKey } from "./components/Header";
import DashboardPage from "./pages/DashboardPage";
import ManagementPage from "./pages/ManagementPage";
import PromptsPage from "./pages/PromptsPage";
import LoginPage from "./pages/LoginPage";
import SignupPage from "./pages/SignupPage";
import VerifyEmailPage from "./pages/VerifyEmailPage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import ResendVerificationPage from "./pages/ResendVerificationPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import PendingApprovalPage from "./pages/PendingApprovalPage";
import { getMe, UnauthorizedError } from "./api/client";
import { logout } from "./api/auth";
import { useApiErrorHandler } from "./hooks/useApiErrorHandler";
import type { MeResponse } from "./api/types";

/** Which unauthenticated screen to show when there is no valid session and
 * the current URL doesn't match a dedicated auth route. */
type AuthView = "login" | "signup";

/**
 * Root component: gates the whole app on the caller's login session.
 *
 * On mount, loads the caller's account via `getMe()` (cookie-based). A 401
 * means there is no valid session, so an unauthenticated view is shown -
 * either a route-specific page (verify email / forgot password / reset
 * password / resend verification, chosen from `window.location.pathname`)
 * or the login/signup toggle. Once a session resolves, a `status === "pending"` account sees
 * `PendingApprovalPage`, a `status === "approved"` account sees the regular
 * dashboard (`Header` plus the active tab's page), and any other status
 * (e.g. "rejected" or "disabled") is treated as logged-out, the same as a
 * 401 from `getMe()`.
 */
export default function App() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [authView, setAuthView] = useState<AuthView>("login");
  const [tab, setTab] = useState<TabKey>("analytics");
  const [loading, setLoading] = useState(true);

  /** Drops the app back to the login page. Called on a 401 from any
   * dashboard API call, and after an explicit log out. */
  const handleUnauthorized = useCallback(() => {
    setMe(null);
    setAuthView("login");
  }, []);

  const { error: meError, setError: setMeError, handleError } = useApiErrorHandler(handleUnauthorized);

  /** Loads (or reloads) the caller's account context via `getMe()`. On a
   * 401, drops the app back to the login page instead of setting an error. */
  const loadMe = useCallback(() => {
    setMeError(null);
    getMe()
      .then(setMe)
      .catch((err) => handleError(err, "Failed to load account"));
  }, [setMeError, handleError]);

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch((err) => {
        if (!(err instanceof UnauthorizedError)) {
          handleError(err, "Failed to load account");
        }
      })
      .finally(() => setLoading(false));
    // Intentionally run once on mount only; `loadMe` is used for later
    // reloads (e.g. after a page changes `me`).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Logs the current session out and returns to the login page.
   */
  const handleLogout = useCallback(() => {
    logout().finally(handleUnauthorized);
  }, [handleUnauthorized]);

  if (loading) {
    return null;
  }

  if (!me || (me.status !== "pending" && me.status !== "approved")) {
    const path = window.location.pathname;
    if (path.endsWith("/verify-email")) {
      return <VerifyEmailPage onGoToLogin={() => (window.location.href = "/")} />;
    }
    if (path.endsWith("/forgot-password")) {
      return <ForgotPasswordPage onBackToLogin={() => (window.location.href = "/")} />;
    }
    if (path.endsWith("/resend-verification")) {
      return <ResendVerificationPage onBackToLogin={() => (window.location.href = "/")} />;
    }
    if (path.endsWith("/reset-password")) {
      return <ResetPasswordPage onGoToLogin={() => (window.location.href = "/")} />;
    }

    if (authView === "signup") {
      return <SignupPage onBackToLogin={() => setAuthView("login")} />;
    }
    return (
      <LoginPage
        onLoggedIn={() => loadMe()}
        onGoToSignup={() => setAuthView("signup")}
        onGoToForgotPassword={() => (window.location.href = "/forgot-password")}
        onGoToResendVerification={() => (window.location.href = "/resend-verification")}
      />
    );
  }

  if (me.status === "pending") {
    return <PendingApprovalPage />;
  }

  return (
    <div className="min-h-screen bg-slate-950">
      <Header
        activeTab={tab}
        onTabChange={setTab}
        accountName={me.name}
        isOperator={me.is_operator}
        onLogout={handleLogout}
      />
      {tab === "analytics" ? (
        <DashboardPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
        />
      ) : tab === "prompts" ? (
        <PromptsPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
        />
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
