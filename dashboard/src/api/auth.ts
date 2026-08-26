/** Auth API calls for signup, login, logout, and password reset. All requests
 *  carry cookies so the server-side session is established/read. */
const BASE = "/dashboard/api/auth";

/**
 * Issues a POST against `${BASE}${path}` with a JSON body, always with
 * cookies attached so the server can establish/read the session.
 *
 * @param path - API path under `/dashboard/api/auth`, with a leading slash.
 * @param body - JSON-serializable request body.
 * @returns The parsed JSON response body.
 * @throws {Error} If the response is not OK; the message includes the
 *   server's error message when the body is OpenAI-shaped.
 */
async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json())?.error?.message ?? "Request failed");
  return res.json() as Promise<T>;
}

/** Response returned on successful login. */
export interface LoginResult {
  /** The logged-in account's id. */
  account_id: number;
  /** The account's status (e.g. "pending", "active"). */
  status: string;
  /** Whether the account is an operator account. */
  is_operator: boolean;
  /** CSRF token to send back as `X-CSRF-Token` on subsequent non-GET requests. */
  csrf_token: string;
}

/**
 * Registers a new account with an email and password.
 *
 * @param email - The account's email address.
 * @param password - The account's chosen password.
 * @returns The signup status.
 */
export const signup = (email: string, password: string) =>
  post<{ status: string }>("/signup", { email, password });

/**
 * Verifies an account's email using the token sent by email.
 *
 * @param token - The email verification token.
 * @returns The verification status.
 */
export const verifyEmail = (token: string) => post<{ status: string }>("/verify-email", { token });

/**
 * Logs in with an email and password, establishing a cookie-based session.
 *
 * @param email - The account's email address.
 * @param password - The account's password.
 * @returns The account id, status, operator flag, and CSRF token for the
 *   new session.
 */
export const login = (email: string, password: string) =>
  post<LoginResult>("/login", { email, password });

/**
 * Logs out, clearing the server-side session.
 *
 * @returns The logout status.
 */
export const logout = () => post<{ status: string }>("/logout", {});

/**
 * Requests a password reset email for the given address.
 *
 * @param email - The account's email address.
 * @returns The request status.
 */
export const requestReset = (email: string) =>
  post<{ status: string }>("/password/reset-request", { email });

/**
 * Resets a password using a reset token.
 *
 * @param token - The password reset token.
 * @param newPassword - The new password to set.
 * @returns The reset status.
 */
export const resetPassword = (token: string, newPassword: string) =>
  post<{ status: string }>("/password/reset", { token, new_password: newPassword });
