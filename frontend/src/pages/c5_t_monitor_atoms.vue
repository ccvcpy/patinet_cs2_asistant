<script setup lang="ts">
import { RouterLink } from "vue-router";
import {
  Archive,
  Bell,
  BriefcaseBusiness,
  ChartNoAxesColumnIncreasing,
  Check,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleX,
  ClipboardList,
  Download,
  Gem,
  LayoutDashboard,
  ListFilter,
  Pause,
  Play,
  ScanLine,
  Search,
  Settings,
  ShieldCheck,
  Target,
  UserRound,
  X,
} from "@lucide/vue";
</script>

<template>
  <div class="atoms-page">
    <header class="atoms-reference-nav">
      <RouterLink class="brand" to="/c5-t-monitor">
        <span class="brand-mark"><ShieldCheck :size="16" /></span><span>CS2 交易运营中心</span>
      </RouterLink>
      <nav class="topnav">
        <RouterLink class="nav-item" to="/profit-trade/overview"><LayoutDashboard :size="14" />概览</RouterLink>
        <RouterLink class="nav-item" to="/account"><Archive :size="14" />仓库</RouterLink>
        <RouterLink class="nav-item" to="/steam"><Gem :size="14" />饰品管理</RouterLink>
        <RouterLink class="nav-item active" to="/c5-t-monitor"><ScanLine :size="14" />C5 扫描 &amp; 库存运营</RouterLink>
        <RouterLink class="nav-item" to="/guadao/operations"><ClipboardList :size="14" />订单管理</RouterLink>
        <RouterLink class="nav-item" to="/case-ratio"><ChartNoAxesColumnIncreasing :size="14" />数据分析</RouterLink>
        <RouterLink class="nav-item" to="/c5-sweeper"><BriefcaseBusiness :size="14" />工具箱</RouterLink>
        <RouterLink class="nav-item" to="/guadao/settings"><Settings :size="14" />系统设置</RouterLink>
      </nav>
      <span class="top-spacer" />
      <span class="top-action"><Bell :size="14" />消息</span>
      <span class="top-action"><UserRound :size="14" />运营账号<ChevronDown :size="12" /></span>
    </header>
    <h1 class="atoms-title">C5 做T监控 · 原子组件板</h1>
    <div class="atoms-layout">
      <section class="atoms-sheet">
        <article class="atom-cell atom-actions">
          <h2>操作按钮</h2>
          <div class="atom-row">
            <button class="button primary" type="button"><ScanLine :size="14" />开始扫描 C5 市场</button>
            <button class="button atom-blue" type="button"><Download :size="14" />导出选品报告</button>
            <button class="button" type="button"><Pause :size="14" />暂停监控</button>
            <button class="button primary" type="button"><Play :size="14" />开始监控</button>
            <button class="button" type="button" disabled><CircleX :size="14" />不可用</button>
          </div>
        </article>
        <article class="atom-cell atom-statuses">
          <h2>运行状态 Chip</h2>
          <div class="atom-row">
            <span class="status-chip running">监控运行中</span>
            <span class="status-chip paused">已暂停</span>
            <span class="status-chip collecting">正在刷新 82/126</span>
            <span class="status-chip reporting">正在生成报告</span>
            <span class="status-chip error">行情异常</span>
          </div>
        </article>

        <article class="atom-cell atom-filter">
          <h2>市场</h2><span class="atom-control">C5<ChevronDown :size="13" /></span>
          <h2>饰品类型</h2><span class="atom-control">全部<ChevronDown :size="13" /></span>
        </article>
        <article class="atom-cell atom-filter">
          <h2>品质</h2><span class="atom-control">全部<ChevronDown :size="13" /></span>
          <h2>磨损区间</h2><span class="atom-range"><span>0.00</span><i>~</i><span>1.00</span></span>
        </article>
        <article class="atom-cell atom-filter">
          <h2>价格区间</h2><span class="atom-range"><span>¥ 最低价</span><i>~</i><span>¥ 最高价</span></span>
          <h2>搜索框</h2><span class="atom-control"><Search :size="13" />饰品名称 / 关键词</span>
        </article>
        <article class="atom-cell atom-filter-actions">
          <h2>筛选操作</h2>
          <div class="atom-row"><button class="button" type="button">重置</button><button class="button primary" type="button"><ListFilter :size="14" />筛选</button></div>
          <h2>选中与输入状态</h2>
          <span class="atom-control selected-control">C5<Check :size="13" /></span>
          <span class="atom-control selected-control"><Search :size="13" />USP-S | Cortex<CircleX :size="13" /></span>
        </article>

        <article class="atom-cell atom-table">
          <h2>推荐排行表格行（正常 / 选中）</h2>
          <div class="atom-rank-row">
            <span>1</span><span class="atom-product"><i><Target :size="15" /></i><b>USP-S | Cortex<small>脑洞大开 · 崭新出厂</small></b></span>
            <span><i class="quality-tag covert">隐秘</i></span><span>¥376.78</span><span>¥267.80</span>
            <strong>1.37%</strong><strong>0.84%</strong><span>100%</span>
            <span><i class="inventory-status ready">可入库</i></span><span><i class="recommendation">稳定推荐</i></span><ChevronRight :size="16" />
          </div>
          <div class="atom-rank-row selected">
            <span>1</span><span class="atom-product"><i><Target :size="15" /></i><b>USP-S | Cortex<small>脑洞大开 · 崭新出厂</small></b></span>
            <span><i class="quality-tag covert">隐秘</i></span><span>¥376.78</span><span>¥267.80</span>
            <strong>1.37%</strong><strong>0.84%</strong><span>100%</span>
            <span><i class="inventory-status ready">可入库</i></span><span><i class="recommendation">稳定推荐</i></span><ChevronRight :size="16" />
          </div>
        </article>
        <article class="atom-cell atom-feedback">
          <h2>任务反馈条</h2>
          <div class="feedback success"><CircleCheck :size="15" />刷新完成：成功 119，缺 C5 4，缺 Steam 3<X :size="13" /></div>
          <div class="feedback error"><CircleX :size="15" />Steam 行情暂不可用，已保留上一份观察<X :size="13" /></div>
        </article>

        <article class="atom-cell atom-metrics">
          <h2>经营指标原子</h2>
          <div class="atom-metric-row">
            <span><small>今日扫描饰品</small><strong>8,752</strong><i>+13.6% ↑</i></span>
            <span><small>可做T机会</small><strong>186</strong><i>+8.3% ↑</i></span>
            <span><small>预计利润总额</small><strong>¥24,560.75</strong><i>+11.2% ↑</i></span>
            <span><small>库存周转率</small><strong>2.38</strong><i class="reserved">前台预留</i></span>
          </div>
          <h2>统计窗口</h2>
          <div class="segmented"><span>24小时</span><span class="active">7天</span><span>30天</span><span>自定义</span></div>
        </article>
        <article class="atom-cell atom-recommendations">
          <h2>品质与库存状态标签</h2>
          <div class="atom-tag-row">
            <i class="quality-tag covert">隐秘</i><i class="quality-tag classified">保密</i><i class="quality-tag restricted">受限</i><i class="quality-tag unknown">未知</i>
            <i class="inventory-status ready">可入库</i><i class="inventory-status watch">观望</i><i class="inventory-status avoid">不建议</i>
          </div>
          <h2>推荐状态与解释</h2>
          <div class="recommendation-list">
            <div><i class="recommendation">稳定推荐</i><strong>收益持续且样本充分</strong><span>优先研究</span></div>
            <div><i class="recommendation observe">等待回落</i><strong>历史稳定，当前买入偏贵</strong><span>继续观察</span></div>
            <div><i class="recommendation avoid">暂不建议</i><strong>长期均值或覆盖不足</strong><span>不进入候选</span></div>
          </div>
        </article>
      </section>

      <aside class="atomic-drawer">
        <header><strong>USP-S | Cortex (Factory New)</strong><span class="status-chip running">稳定推荐</span><X :size="14" /></header>
        <div class="atomic-drawer-body">
          <div class="drawer-summary"><span><small>当前 ROI</small><strong>1.37%</strong></span><span><small>7日平均</small><strong>0.84%</strong></span><span><small>历史最高</small><strong>4.05%</strong></span></div>
          <h3>ROI 走势（7 天）</h3>
          <svg class="drawer-chart" viewBox="0 0 300 130" preserveAspectRatio="none"><line class="trend-grid" x1="0" y1="20" x2="300" y2="20" /><line class="trend-grid" x1="0" y1="52" x2="300" y2="52" /><line class="trend-grid" x1="0" y1="84" x2="300" y2="84" /><polyline class="trend-line" points="0,78 16,75 31,79 47,70 63,72 79,61 95,66 111,58 126,63 142,48 158,52 174,46 189,54 205,42 221,47 237,40 253,45 268,37 284,41 300,34" /></svg>
          <h3>ROI 区间持续时间</h3>
          <div class="duration-list">
            <div class="duration-row"><span>≥ 2.00%</span><i class="duration-track"><span style="width:13%" /></i><b>21h50m · 13%</b></div>
            <div class="duration-row"><span>1%~2%</span><i class="duration-track"><span style="width:39%" /></i><b>65h31m · 39%</b></div>
            <div class="duration-row"><span>0%~1%</span><i class="duration-track"><span style="width:48%" /></i><b>80h39m · 48%</b></div>
            <div class="duration-row"><span>&lt; 0%</span><i class="duration-track"><span style="width:2%" /></i><b>0m · 0%</b></div>
          </div>
          <div class="drawer-prices"><span><small>Steam 买入</small><strong>¥376.78</strong></span><span><small>C5 售价</small><strong>¥267.80</strong></span><span><small>C5 预计到手</small><strong>¥265.12</strong></span><span><small>单件预计收益</small><strong>+¥5.14</strong></span></div>
          <p class="drawer-note">研究结论：372 个有效样本全部为正 ROI，当前收益高于 7 日平均。该结论只用于选品，不会自动创建 Profit Trade 流水。</p>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped src="./c5_t_monitor.css"></style>
