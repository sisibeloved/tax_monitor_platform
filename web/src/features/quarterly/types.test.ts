import { describe, expect, it } from "vitest";

import { formatMoney, formatPercent } from "./types";

describe("exact decimal presentation", () => {
  it("formats amounts beyond Number safe integer without precision loss", () => {
    expect(formatMoney("900719925474099312345.678", "CNY", 2)).toBe(
      "¥900,719,925,474,099,312,345.68",
    );
  });

  it("rounds positive and negative half values with ROUND_HALF_UP", () => {
    expect(formatMoney("1.005", "CNY", 2)).toBe("¥1.01");
    expect(formatMoney("-1.005", "CNY", 2)).toBe("-¥1.01");
  });

  it("honors zero, two, and three ledger scales", () => {
    expect(formatMoney("1234.5", "CNY", 0)).toBe("¥1,235");
    expect(formatMoney("1234.5", "CNY", 2)).toBe("¥1,234.50");
    expect(formatMoney("1234.5", "CNY", 3)).toBe("¥1,234.500");
    expect(formatMoney("0", "CNY", 2, true)).toBe("¥0.00");
  });

  it("shifts and rounds ratios as percentages without floating point", () => {
    expect(formatPercent("0.03255", 2)).toBe("3.26%");
    expect(formatPercent("-0.03255", 2)).toBe("-3.26%");
    expect(formatPercent("0.09", 2)).toBe("9%");
    expect(formatPercent("0.00005", 2)).toBe("0.01%");
  });
});
