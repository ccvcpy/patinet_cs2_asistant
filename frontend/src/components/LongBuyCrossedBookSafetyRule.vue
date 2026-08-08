<script setup lang="ts">
import FolioIcon from "./FolioIcon.vue";

const rules = [
  {
    id: "existing-unmatched",
    condition: "有旧长期单 + 未成交",
    conditionIcon: "calendar",
    outcome: "冻结：不撤、不改、不新建、不直购",
    outcomeIcon: "lock",
    tone: "freeze",
  },
  {
    id: "existing-matched",
    condition: "有旧长期单 + 已成交",
    conditionIcon: "calendar",
    outcome: "锁老库存 A → C5 上架",
    outcomeIcon: "lock",
    tone: "complete",
  },
  {
    id: "no-existing",
    condition: "无旧长期单",
    conditionIcon: "circle-dashed",
    outcome: "原直购链路",
    outcomeIcon: "rocket",
    tone: "purchase",
  },
] as const;
</script>

<template>
  <div class="crossed-book-shell">
    <span class="atomic-component-label">CrossedBookSafetyRule</span>
    <article
      class="crossed-book-rule"
      data-testid="long-buy-crossed-book-rule"
      aria-label="交叉盘口安全护栏"
    >
      <header>
        <h3><FolioIcon name="shield" :size="18" />交叉盘口安全护栏</h3>
        <span class="safety-badge">☆&nbsp; 安全第一</span>
      </header>

      <ol>
        <li
          v-for="rule in rules"
          :key="rule.id"
          :class="rule.tone"
          :data-testid="`crossed-book-${rule.id}`"
        >
          <span class="rule-node condition">
            <FolioIcon :name="rule.conditionIcon" :size="15" />
            <strong>{{ rule.condition }}</strong>
          </span>
          <span class="rule-connector" aria-hidden="true"><i></i><b>→</b></span>
          <span class="rule-node outcome">
            <FolioIcon :name="rule.outcomeIcon" :size="15" />
            <strong>{{ rule.outcome }}</strong>
          </span>
        </li>
      </ol>
    </article>
  </div>
</template>

<style scoped>
.crossed-book-shell {
  display: grid;
  grid-template-rows: auto 1fr;
  gap: 6px;
  min-width: 0;
}

.atomic-component-label {
  color: #89958e;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 9px;
  letter-spacing: .04em;
}

.crossed-book-rule {
  display: grid;
  align-content: start;
  gap: 13px;
  height: 100%;
  min-height: 158px;
  padding: 13px 14px;
  border: 1px solid #dfe6e1;
  border-radius: 11px;
  background: #fff;
  box-shadow: 0 5px 16px rgba(34, 54, 43, .045);
}

.crossed-book-rule header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.crossed-book-rule h3 {
  display: flex;
  gap: 7px;
  align-items: center;
  margin: 0;
  color: #173e2f;
  font-size: 15px;
  line-height: 1.2;
}

.crossed-book-rule h3 :deep(svg) {
  color: #d49321;
}

.safety-badge {
  flex: 0 0 auto;
  padding: 4px 9px;
  border: 1px solid #eedbb3;
  border-radius: 999px;
  color: #9b6b14;
  background: #fff7e4;
  font-size: 9px;
  font-weight: 750;
  white-space: nowrap;
}

.crossed-book-rule ol {
  display: grid;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.crossed-book-rule li {
  display: grid;
  grid-template-columns: minmax(140px, .86fr) 38px minmax(190px, 1.14fr);
  align-items: center;
  min-width: 0;
}

.rule-node {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 32px;
  padding: 7px 10px;
  border: 1px solid #e7eee9;
  border-radius: 7px;
  color: #2c5945;
  background: #f2f7f3;
  min-width: 0;
}

.rule-node strong {
  min-width: 0;
  color: #34483e;
  font-size: 10px;
  font-weight: 650;
  line-height: 1.25;
}

.rule-node :deep(svg) {
  flex: 0 0 auto;
  color: #23815b;
}

.rule-connector {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  color: #9a7a3f;
}

.rule-connector i {
  height: 1px;
  background: repeating-linear-gradient(90deg, #cdb987 0 4px, transparent 4px 7px);
}

.rule-connector b {
  margin-left: 1px;
  font-size: 15px;
  font-weight: 500;
  line-height: 1;
}

.freeze .outcome {
  border-color: #eee2c7;
  color: #805d21;
  background: #fff8e9;
}

.freeze .outcome :deep(svg) {
  color: #c58417;
}

.complete .outcome,
.purchase .outcome {
  border-color: #e0ebe4;
  background: #f2f7f3;
}

.purchase .condition {
  border-style: dashed;
  color: #718078;
  background: #f8faf8;
}

.purchase .condition :deep(svg) {
  color: #91a199;
}

@media (max-width: 900px) {
  .crossed-book-rule li {
    grid-template-columns: minmax(0, 1fr) 32px minmax(0, 1.25fr);
  }
}

@media (max-width: 620px) {
  .crossed-book-rule li {
    grid-template-columns: 1fr;
    gap: 4px;
  }

  .rule-connector {
    display: none;
  }
}
</style>
