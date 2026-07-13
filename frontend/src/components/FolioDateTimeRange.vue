<script setup lang="ts">
import { VueDatePicker } from "@vuepic/vue-datepicker";
import "@vuepic/vue-datepicker/dist/main.css";
import { zhCN } from "date-fns/locale";

const model = defineModel<Date[]>({ required: true });

function startOfToday(): Date {
  const value = new Date();
  value.setHours(0, 0, 0, 0);
  return value;
}

function startOfCurrentMonth(): Date {
  const value = new Date();
  return new Date(value.getFullYear(), value.getMonth(), 1, 0, 0, 0, 0);
}

function daysAgo(days: number): Date {
  const value = startOfToday();
  value.setDate(value.getDate() - days);
  return value;
}

function formatDateTime(value: Date): string {
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())} ${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function formatRange(value: Date[]): string {
  if (!Array.isArray(value) || !value.length) return "";
  if (!value[1]) return formatDateTime(value[0]);
  return `${formatDateTime(value[0])}  至  ${formatDateTime(value[1])}`;
}

const presets = [
  { label: "今天", value: [startOfToday(), new Date()] },
  { label: "最近 7 天", value: [daysAgo(6), new Date()] },
  { label: "本月", value: [startOfCurrentMonth(), new Date()] },
  { label: "最近 30 天", value: [daysAgo(29), new Date()] },
];
</script>

<template>
  <div class="folio-date-range">
    <VueDatePicker
      v-model="model"
      :locale="zhCN"
      :range="{ partialRange: false }"
      :multi-calendars="{ count: 2, solo: true }"
      :time-config="{
        enableTimePicker: true,
        enableSeconds: false,
        enableMinutes: true,
        is24: true,
        minutesIncrement: 1,
        timePickerInline: true,
      }"
      :formats="{ input: formatRange, preview: formatRange }"
      :preset-dates="presets"
      :action-row="{
        showSelect: true,
        showCancel: true,
        showNow: false,
        showPreview: true,
        selectBtnLabel: '确认范围',
        cancelBtnLabel: '取消',
      }"
      :text-input="{ enterSubmit: true, tabSubmit: true, openMenu: 'open', rangeSeparator: ' 至 ' }"
      :input-attrs="{ clearable: false, alwaysClearable: false, required: true, autocomplete: 'off' }"
      week-start="1"
      placeholder="选择开始和结束时间"
      aria-label="报表时间范围"
    />
  </div>
</template>

<style scoped>
.folio-date-range {
  width: 100%;
  min-width: 0;
  --dp-font-family: var(--folio-font);
  --dp-font-size: 13px;
  --dp-background-color: #fff;
  --dp-text-color: var(--folio-ink);
  --dp-hover-color: var(--folio-green-soft);
  --dp-hover-text-color: var(--folio-green-dark);
  --dp-hover-icon-color: var(--folio-green);
  --dp-primary-color: var(--folio-green);
  --dp-primary-disabled-color: #96b7a6;
  --dp-primary-text-color: #fff;
  --dp-secondary-color: #a0aaa3;
  --dp-border-color: #dfe4df;
  --dp-menu-border-color: var(--folio-line);
  --dp-border-color-hover: #bfcac2;
  --dp-border-color-focus: var(--folio-green);
  --dp-icon-color: #6b756e;
  --dp-highlight-color: var(--folio-green-soft);
  --dp-range-between-dates-background-color: #edf5f0;
  --dp-range-between-dates-text-color: var(--folio-green-dark);
  --dp-range-between-border-color: #edf5f0;
  --dp-border-radius: 11px;
  --dp-cell-border-radius: 9px;
  --dp-menu-min-width: 690px;
  --dp-input-padding: 10px 38px 10px 42px;
  --dp-action-row-padding: 12px;
}

:deep(.dp--input) {
  min-height: 44px;
  font-weight: 650;
  letter-spacing: .005em;
  box-shadow: none;
}

:deep(.dp--input-focus) {
  box-shadow: 0 0 0 3px rgba(35, 106, 76, .1);
}

:deep(.dp--menu) {
  border-radius: 16px;
  box-shadow: 0 18px 50px rgba(25, 45, 35, .14);
  overflow: hidden;
}

:deep(.dp--preset-dates) {
  min-width: 104px;
  padding: 10px;
  background: var(--folio-surface-soft);
}

:deep(.dp--preset-range) {
  border-radius: 9px;
  padding: 9px 10px;
  font-size: 12px;
  font-weight: 650;
}

:deep(.dp--action-row) {
  border-top: 1px solid var(--folio-line);
}

:deep(.dp--action-select),
:deep(.dp--action-cancel) {
  min-height: 34px;
  border-radius: 9px;
  padding: 5px 12px;
  font-weight: 700;
}
</style>
