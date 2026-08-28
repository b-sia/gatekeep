import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PasswordInput from "./PasswordInput";

afterEach(cleanup);

it("defaults to masked and toggles to plain text on Show/Hide", () => {
  render(<PasswordInput id="pw" label="Password" value="secret" onChange={vi.fn()} />);
  const input = screen.getByLabelText(/password/i, { selector: "input" }) as HTMLInputElement;
  expect(input.type).toBe("password");

  fireEvent.click(screen.getByRole("button", { name: /show/i }));
  expect(input.type).toBe("text");
  expect(screen.getByRole("button", { name: /hide/i })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /hide/i }));
  expect(input.type).toBe("password");
});

it("calls onChange with the new value", () => {
  const onChange = vi.fn();
  render(<PasswordInput id="pw" label="Password" value="" onChange={onChange} />);
  fireEvent.change(screen.getByLabelText(/password/i, { selector: "input" }), { target: { value: "abc" } });
  expect(onChange).toHaveBeenCalledWith("abc");
});
