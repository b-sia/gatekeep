import { beforeEach, describe, expect, it } from "vitest";
import {
  addIdentity,
  clearActiveIdentity,
  getActiveIdentity,
  getActiveKey,
  listIdentities,
  markInvalid,
  reauthenticate,
  removeIdentity,
  setActiveIdentity,
} from "./identityStore";

/** Fresh storage before every test so cases do not leak roster/pointer
 * state into one another. */
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe("addIdentity / listIdentities", () => {
  it("appends an active identity and returns it with a generated id", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    expect(created.id).toBeTruthy();
    expect(created.status).toBe("active");
    const roster = listIdentities();
    expect(roster).toHaveLength(1);
    expect(roster[0]).toEqual(created);
  });

  it("persists the roster in localStorage so other tabs can read it", () => {
    addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    const raw = localStorage.getItem("gatekeep_identities");
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw as string)).toHaveLength(1);
  });
});

describe("per-tab active pointer", () => {
  it("stores only the id in sessionStorage", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    expect(sessionStorage.getItem("gatekeep_active_identity")).toBe(created.id);
  });

  it("resolves the pointer to the roster entry", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    expect(getActiveIdentity()).toEqual(created);
    expect(getActiveKey()).toBe("sk-a");
  });

  it("returns null when no pointer is set (a fresh tab starts logged out)", () => {
    addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    expect(getActiveIdentity()).toBeNull();
    expect(getActiveKey()).toBeNull();
  });

  it("returns null when the pointed-to entry no longer exists", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    removeIdentity(created.id);
    expect(getActiveIdentity()).toBeNull();
  });
});

describe("markInvalid + the active-must-be-active invariant", () => {
  it("flips status to invalid but keeps the entry listed", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    markInvalid(created.id);
    const roster = listIdentities();
    expect(roster).toHaveLength(1);
    expect(roster[0].status).toBe("invalid");
  });

  it("makes getActiveIdentity return null for an invalid active entry", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    markInvalid(created.id);
    expect(getActiveIdentity()).toBeNull();
    expect(getActiveKey()).toBeNull();
  });
});

describe("reauthenticate", () => {
  it("swaps the key and restores active status when the account id matches", () => {
    const created = addIdentity({
      key: "sk-old",
      accountId: 1,
      accountName: "Alice",
      isOperator: false,
    });
    markInvalid(created.id);
    reauthenticate(created.id, "sk-new", 1, "Alice", false);
    const roster = listIdentities();
    expect(roster[0].status).toBe("active");
    expect(roster[0].key).toBe("sk-new");
  });

  it("refreshes accountName and isOperator to the freshly validated values", () => {
    const created = addIdentity({
      key: "sk-old",
      accountId: 1,
      accountName: "Alice",
      isOperator: false,
    });
    markInvalid(created.id);
    reauthenticate(created.id, "sk-new", 1, "Alice Renamed", true);
    const roster = listIdentities();
    expect(roster[0].accountName).toBe("Alice Renamed");
    expect(roster[0].isOperator).toBe(true);
  });

  it("throws and leaves the entry unchanged when the account id does not match", () => {
    const created = addIdentity({
      key: "sk-old",
      accountId: 1,
      accountName: "Alice",
      isOperator: false,
    });
    markInvalid(created.id);
    expect(() => reauthenticate(created.id, "sk-new", 2, "Bob", false)).toThrow(
      "This key belongs to a different account",
    );
    const roster = listIdentities();
    expect(roster[0].status).toBe("invalid");
    expect(roster[0].key).toBe("sk-old");
  });

  it("throws when the entry no longer exists in the roster", () => {
    const created = addIdentity({
      key: "sk-old",
      accountId: 1,
      accountName: "Alice",
      isOperator: false,
    });
    markInvalid(created.id);
    removeIdentity(created.id);
    expect(() => reauthenticate(created.id, "sk-new", 1, "Alice", false)).toThrow(
      "This identity was removed - it may have been forgotten in another tab.",
    );
    expect(listIdentities()).toHaveLength(0);
  });
});

describe("clearActiveIdentity", () => {
  it("removes only the pointer, leaving the roster intact", () => {
    const created = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    clearActiveIdentity();
    expect(getActiveIdentity()).toBeNull();
    expect(listIdentities()).toHaveLength(1);
  });
});

describe("per-tab isolation over one shared roster", () => {
  it("lets two pointers over the same roster resolve to different identities", () => {
    const alice = addIdentity({ key: "sk-a", accountId: 1, accountName: "Alice", isOperator: false });
    const bob = addIdentity({ key: "sk-b", accountId: 2, accountName: "Bob", isOperator: true });

    // Tab 1 picks Alice.
    setActiveIdentity(alice.id);
    expect(getActiveKey()).toBe("sk-a");

    // Tab 2 is a different sessionStorage, simulated by overwriting the
    // pointer; the shared localStorage roster is unchanged.
    setActiveIdentity(bob.id);
    expect(getActiveKey()).toBe("sk-b");
    expect(listIdentities()).toHaveLength(2);
  });
});
