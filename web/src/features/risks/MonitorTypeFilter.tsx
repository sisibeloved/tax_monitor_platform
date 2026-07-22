import { Select } from "antd";

import type { MonitorType } from "./types";

const options: Array<{ label: string; value: MonitorType }> = [
  { label: "所得税计提", value: "ACCRUAL_ACCURACY" },
  { label: "递延所得税计提/转回", value: "DEFERRED_TAX_ACCURACY" },
  { label: "累计税负率", value: "TAX_BURDEN" },
  { label: "潜在税务成本", value: "POTENTIAL_TAX_COST" },
  {
    label: "所得税退税进度监控及入账科目准确性检查",
    value: "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
  },
  { label: "业务招待费", value: "BUSINESS_ENTERTAINMENT" },
  { label: "福利费", value: "WELFARE" },
  { label: "公益性捐赠", value: "DONATION" },
];

export function MonitorTypeFilter(props: {
  value?: MonitorType;
  onChange: (value: MonitorType | undefined) => void;
}) {
  return (
    <Select
      allowClear
      aria-label="监测类型"
      options={options}
      placeholder="全部监测类型"
      style={{ width: 340 }}
      value={props.value}
      onChange={(value) => props.onChange(value)}
    />
  );
}
