import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MonitorTypeFilter } from "./MonitorTypeFilter";

describe("MonitorTypeFilter", () => {
  it("emits the welfare and donation monitor filters", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <MonitorTypeFilter value={undefined} onChange={onChange} />,
    );
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("福利费"));
    expect(onChange).toHaveBeenCalledWith("WELFARE");

    rerender(<MonitorTypeFilter value={undefined} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("公益性捐赠"));
    expect(onChange).toHaveBeenCalledWith("DONATION");
  });
});
