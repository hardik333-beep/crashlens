import { describe, expect, it } from "vitest";
import { BreadcrumbBuffer } from "../src/breadcrumbs";
import type { Breadcrumb } from "../src/types";

function crumb(message: string): Breadcrumb {
  return { timestamp: new Date().toISOString(), message };
}

describe("BreadcrumbBuffer ring buffer", () => {
  it("keeps insertion order (newest last)", () => {
    const buf = new BreadcrumbBuffer(10);
    buf.add(crumb("a"));
    buf.add(crumb("b"));
    buf.add(crumb("c"));
    expect(buf.all().map((c) => c.message)).toEqual(["a", "b", "c"]);
  });

  it("drops the oldest when over capacity", () => {
    const buf = new BreadcrumbBuffer(3);
    for (const m of ["a", "b", "c", "d", "e"]) buf.add(crumb(m));
    expect(buf.all().map((c) => c.message)).toEqual(["c", "d", "e"]);
  });

  it("keeps nothing when max is 0", () => {
    const buf = new BreadcrumbBuffer(0);
    buf.add(crumb("a"));
    expect(buf.all()).toEqual([]);
  });

  it("returns a copy, not the internal array", () => {
    const buf = new BreadcrumbBuffer(5);
    buf.add(crumb("a"));
    const snapshot = buf.all();
    snapshot.push(crumb("mutation"));
    expect(buf.all()).toHaveLength(1);
  });

  it("clears", () => {
    const buf = new BreadcrumbBuffer(5);
    buf.add(crumb("a"));
    buf.clear();
    expect(buf.all()).toEqual([]);
  });
});
