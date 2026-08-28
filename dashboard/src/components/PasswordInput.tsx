import { useState } from "react";

interface PasswordInputProps {
  /** `id`/`htmlFor` for the input and its label. */
  id: string;
  /** Label text shown above the input. */
  label: string;
  /** Current field value. */
  value: string;
  /** Called with the new value on every keystroke. */
  onChange: (value: string) => void;
  /** Whether the input should autofocus on mount. */
  autoFocus?: boolean;
}

/** Open-eye icon shown when the password is hidden (click to reveal it). */
function EyeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

/** Crossed-out eye icon shown when the password is visible (click to hide it). */
function EyeOffIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a20.3 20.3 0 0 1 5.06-5.94M9.9 4.24A10.4 10.4 0 0 1 12 4c7 0 11 7 11 7a20.3 20.3 0 0 1-2.68 3.68M14.12 14.12a3 3 0 1 1-4.24-4.24" />
      <path d="M1 1l22 22" />
    </svg>
  );
}

/**
 * A password `<input>` with an eye-icon toggle that switches its type
 * between `password` and `text`.
 *
 * @param props - See {@link PasswordInputProps}.
 */
export default function PasswordInput({ id, label, value, onChange, autoFocus }: PasswordInputProps) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="mb-3">
      <label htmlFor={id} className="mb-1 block text-sm text-slate-300">
        {label}
      </label>
      <div className="relative">
        <input
          id={id}
          type={visible ? "text" : "password"}
          autoFocus={autoFocus}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 pr-9 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="button"
          onClick={() => setVisible((v) => !v)}
          aria-label={visible ? "Hide password" : "Show password"}
          className="absolute inset-y-0 right-0 flex items-center px-2.5 text-slate-400 hover:text-slate-200"
        >
          {visible ? <EyeOffIcon /> : <EyeIcon />}
        </button>
      </div>
    </div>
  );
}
