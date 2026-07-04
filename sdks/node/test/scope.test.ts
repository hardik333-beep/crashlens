import { afterEach, describe, expect, it } from "vitest";
import {
  addBreadcrumb,
  currentScope,
  resetFallback,
  runInScope,
  setTag,
  setUser,
} from "../src/scope";

afterEach(() => resetFallback());

describe("scope fallback (outside any ALS run)", () => {
  it("applies tags / user / breadcrumbs to the module-level fallback", () => {
    setTag("server", "web-1");
    setUser({ id: "u-1" });
    addBreadcrumb({ message: "started" });
    const s = currentScope();
    expect(s.tags.server).toBe("web-1");
    expect(s.user).toEqual({ id: "u-1" });
    expect(s.breadcrumbs.all()).toHaveLength(1);
  });
});

describe("ALS scope isolation", () => {
  it("does not share tags between two concurrent async contexts", async () => {
    const seen: Record<string, Record<string, string>> = {};

    async function ctx(name: string, wait: number): Promise<void> {
      await new Promise<void>((r) =>
        runInScope(() => {
          setTag("ctx", name);
          // Yield so the two contexts interleave; ALS must keep them apart.
          setTimeout(() => {
            seen[name] = { ...currentScope().tags };
            r();
          }, wait);
        }),
      );
    }

    await Promise.all([ctx("A", 30), ctx("B", 10)]);

    expect(seen.A.ctx).toBe("A");
    expect(seen.B.ctx).toBe("B");
    // Neither context saw the other's tag value.
    expect(seen.A.ctx).not.toBe(seen.B.ctx);
  });

  it("forks from the current scope so process-wide tags still apply", () => {
    setTag("global", "yes");
    runInScope(() => {
      expect(currentScope().tags.global).toBe("yes");
      setTag("local", "only-here");
    });
    // The per-request write did not leak back to the fallback.
    expect(currentScope().tags.local).toBeUndefined();
    expect(currentScope().tags.global).toBe("yes");
  });

  it("isolates breadcrumbs added inside a request scope", () => {
    addBreadcrumb({ message: "global-crumb" });
    runInScope(() => {
      addBreadcrumb({ message: "request-crumb" });
      expect(currentScope().breadcrumbs.all()).toHaveLength(2);
    });
    // Fallback still has only its own crumb.
    expect(currentScope().breadcrumbs.all()).toHaveLength(1);
  });
});
