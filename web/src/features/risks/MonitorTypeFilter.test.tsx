import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MonitorTypeFilter } from "./MonitorTypeFilter";

describe("MonitorTypeFilter", () => {
  it("emits deferred-tax, refund, welfare, and donation monitor filters", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <MonitorTypeFilter value={undefined} onChange={onChange} />,
    );
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("递延所得税计提/转回"));
    expect(onChange).toHaveBeenCalledWith("DEFERRED_TAX_ACCURACY");

    rerender(<MonitorTypeFilter value={undefined} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("所得税退税进度监控及入账科目准确性检查"));
    expect(onChange).toHaveBeenCalledWith("INCOME_TAX_REFUND_ACCOUNT_ACCURACY");

    rerender(<MonitorTypeFilter value={undefined} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("福利费"));
    expect(onChange).toHaveBeenCalledWith("WELFARE");

    rerender(<MonitorTypeFilter value={undefined} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("公益性捐赠"));
    expect(onChange).toHaveBeenCalledWith("DONATION");
  });
});
