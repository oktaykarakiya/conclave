/**
 * Config saves send only what changed. A project stores just its delta over the global
 * settings (the server minimises every write anyway), so a page diffs the edited document
 * against the effective one it loaded: `patch` holds changed or added values, `unset` the
 * dotted paths of removed keys, which then inherit again.
 */

export type ConfigDoc = Record<string, unknown>;

export interface ConfigDiff {
  patch: ConfigDoc;
  unset: string[];
}

// Top-level keys compared (and replaced) whole rather than key by key, as on the server.
const WHOLE_KEYS = new Set(["workflow"]);

export function isPlainObject(value: unknown): value is ConfigDoc {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Structural equality of two JSON values. */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => jsonEqual(item, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((key) => Object.hasOwn(b, key) && jsonEqual(a[key], b[key]))
    );
  }
  return false;
}

/**
 * Diff `after` against `before`: plain objects recurse key by key; the top-level `workflow`,
 * arrays and scalars are compared whole; keys missing from `after` become `unset` paths.
 */
export function diffConfig(before: ConfigDoc, after: ConfigDoc): ConfigDiff {
  const unset: string[] = [];
  const walk = (prev: ConfigDoc, next: ConfigDoc, prefix: string): ConfigDoc => {
    const patch: ConfigDoc = {};
    for (const [key, value] of Object.entries(next)) {
      const old = prev[key];
      const whole = prefix === "" && WHOLE_KEYS.has(key);
      if (!whole && isPlainObject(value) && isPlainObject(old)) {
        const sub = walk(old, value, `${prefix}${key}.`);
        if (Object.keys(sub).length > 0) patch[key] = sub;
      } else if (!Object.hasOwn(prev, key) || !jsonEqual(old, value)) {
        patch[key] = value;
      }
    }
    for (const key of Object.keys(prev)) {
      if (!Object.hasOwn(next, key)) unset.push(`${prefix}${key}`);
    }
    return patch;
  };
  return { patch: walk(before, after, ""), unset };
}

export function isEmptyDiff(diff: ConfigDiff): boolean {
  return Object.keys(diff.patch).length === 0 && diff.unset.length === 0;
}
