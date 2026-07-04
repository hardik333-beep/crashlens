// A fixed-size ring buffer of breadcrumbs. When full, the oldest crumb is
// dropped so the buffer always holds the most recent `max` (newest last), which
// matches the protocol ordering (docs/PROTOCOL.md section 3.4).

import type { Breadcrumb } from "./types";

export class BreadcrumbBuffer {
  private items: Breadcrumb[] = [];
  readonly max: number;

  constructor(max: number) {
    this.max = Math.max(0, max);
  }

  add(crumb: Breadcrumb): void {
    if (this.max === 0) return;
    this.items.push(crumb);
    while (this.items.length > this.max) {
      this.items.shift();
    }
  }

  all(): Breadcrumb[] {
    return this.items.slice();
  }

  clear(): void {
    this.items = [];
  }

  clone(): BreadcrumbBuffer {
    const copy = new BreadcrumbBuffer(this.max);
    copy.items = this.items.slice();
    return copy;
  }
}
