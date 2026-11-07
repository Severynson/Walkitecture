import type { Node } from "@babylonjs/core";

/** objectId -> nodes created for it, so an edit can dispose and rebuild only what changed. */
export class Registry {
  private readonly byId = new Map<string, Node[]>();

  add(objectId: string, node: Node): void {
    const list = this.byId.get(objectId);
    if (list) list.push(node);
    else this.byId.set(objectId, [node]);
  }

  /** Disposes every node registered under `objectId`. */
  dispose(objectId: string): void {
    for (const node of this.byId.get(objectId) ?? []) node.dispose(false, false);
    this.byId.delete(objectId);
  }

  disposeAll(): void {
    for (const id of [...this.byId.keys()]) this.dispose(id);
  }

  has(objectId: string): boolean {
    return this.byId.has(objectId);
  }
}
