/** Minor units → display string. Money is only ever integers on the wire. */
export function formatMinor(amountMinor: number, currency: string): string {
  const major = (amountMinor / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${currency} ${major}`;
}

/**
 * Parse a user-typed amount like "50,000.00" into integer minor units, or
 * null if it isn't an exact, positive amount with at most two decimals.
 * Never rounds: "12.345" is rejected, not silently turned into 1235.
 */
export function parseMajorToMinor(input: string): number | null {
  const cleaned = input.replace(/,/g, "").trim();
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  const [whole, frac = ""] = cleaned.split(".");
  const minor = Number(whole) * 100 + Number(frac.padEnd(2, "0"));
  return Number.isSafeInteger(minor) && minor > 0 ? minor : null;
}

export function secondsUntil(epochSeconds: number | null): number | null {
  if (epochSeconds == null) return null;
  return Math.max(0, Math.round(epochSeconds - Date.now() / 1000));
}
