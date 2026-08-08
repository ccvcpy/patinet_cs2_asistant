<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";
import {
  formatInteger,
  formatRatio,
  recommendedRatio,
  speedLabel,
  stabilityLabel,
  stabilityTone,
} from "./format";
import type { CaseRatioItem } from "./types";

const props = defineProps<{
  item: CaseRatioItem;
  rank: number;
  selected?: boolean;
}>();

const sourceLabel = computed(() => {
  const label = String(props.item.steamReferenceSourceLabel || "");
  if (!label || /wall|墙/i.test(label)) return "20墙挂价";
  if (/buy|求购/i.test(label)) return "最高求购";
  return label;
});
</script>

<template>
  <button
    class="cm-recommendation-row"
    :class="{ 'is-selected': selected }"
    type="button"
    :aria-label="`查看 ${item.marketHashName} 详情`"
  >
    <span class="cm-recommendation-row__rank">
      <span v-if="rank === 1" class="cm-rank-badge">{{ rank }}</span>
      <span v-else>{{ rank }}</span>
      <FolioIcon
        v-if="rank <= 3"
        class="cm-rank-crown"
        :class="`cm-rank-crown--${rank}`"
        name="crown"
        :size="15"
        :stroke-width="2"
      />
    </span>
    <span class="cm-recommendation-row__name" :title="item.marketHashName">
      {{ item.marketHashName }}
    </span>
    <span class="cm-recommendation-row__ratio">{{ formatRatio(recommendedRatio(item)) }}</span>
    <span>{{ sourceLabel }}</span>
    <span>{{ formatInteger(item.steamVolume24h) }}</span>
    <span class="cm-dot-value">{{ speedLabel(item) }}</span>
    <span
      class="cm-dot-value"
      :class="{
        'cm-dot-value--medium': stabilityTone(item) === 'medium',
        'cm-dot-value--low': stabilityTone(item) === 'low',
      }"
    >
      {{ stabilityLabel(item) }}
    </span>
    <span>{{ formatRatio(item.minRatio) }} / {{ item.minRatioDurationLabel }}</span>
    <span>{{ formatRatio(item.maxRatio) }} / {{ item.maxRatioDurationLabel }}</span>
    <span class="cm-recommendation-row__detail">
      <FolioIcon name="chevron-right" :size="16" />
    </span>
  </button>
</template>
