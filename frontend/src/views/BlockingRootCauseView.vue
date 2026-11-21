<template>
  <section class="page blocking-page">
    <header class="page-head">
      <div>
        <h2>分区阻塞根因分析</h2>
        <p class="page-desc">
          按目标表与业务日期追溯每一条未齐依赖路径的源分区阻塞明细。只承认分区就绪登记，
          节点已上线不代表分区数据确已产出；同一根因分区在恢复计划中只允许一个写入项。
        </p>
      </div>
    </header>

    <!-- 检索区 -->
    <form class="analysis-form" @submit.prevent="runAnalyze">
      <label>
        目标表
        <input
          v-model="targetTable"
          data-testid="blocking-target-table"
          placeholder="如 ads.ads_sales_daily"
          required
        />
      </label>
      <label>
        业务日期
        <input
          v-model="targetPartition"
          data-testid="blocking-target-partition"
          type="date"
          required
        />
      </label>
      <label class="check-label">
        <input v-model="onlyBlocked" data-testid="blocking-only-blocked" type="checkbox" />
        仅看未齐路径
      </label>
      <button class="btn primary" data-testid="blocking-query" type="submit" :disabled="loading">
        {{ loading ? '核对中…' : '检索根因' }}
      </button>
      <button class="btn ghost" type="button" @click="loadRunnable">查询可启动日表</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="analysis">
      <!-- 汇总卡片 -->
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">依赖路径</span>
          <strong class="stat-value">{{ analysis.path_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">未齐路径</span>
          <strong class="stat-value" :class="{ 'trend-down': analysis.blocked_path_count > 0 }">
            {{ analysis.blocked_path_count }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">根因数</span>
          <strong class="stat-value">{{ analysis.root_causes.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">目标分区状态</span>
          <strong class="stat-value">{{ analysis.target_partition_status }}</strong>
        </article>
      </div>

      <!-- 路径明细 -->
      <h3>依赖路径与源分区阻塞明细</h3>
      <table class="data-table" data-testid="path-table">
        <thead>
          <tr>
            <th>路径标识</th>
            <th>依赖链路</th>
            <th>源分区核对（{{ targetPartition }}）</th>
            <th>路径状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="path in analysis.paths" :key="path.path_id" :data-testid="`path-row-${path.path_id}`">
            <td>{{ path.path_id }}</td>
            <td class="chain-cell">{{ path.path_chain.join(' → ') }}</td>
            <td>
              <div v-for="src in path.sources" :key="`${src.source_table}@${src.partition_value}`" class="source-line">
                <span class="tag" :class="src.ready ? 'tag-ready' : 'tag-blocking'">
                  {{ src.ready ? '已齐' : src.status }}
                </span>
                <code>{{ src.source_table }}@{{ src.partition_value }}</code>
                <span class="source-reason">{{ src.reason }}</span>
              </div>
            </td>
            <td>
              <span class="tag" :class="path.ready ? 'tag-ready' : 'tag-blocking'">
                {{ path.ready ? '输入齐全' : '未齐' }}
              </span>
            </td>
            <td>
              <button
                v-for="src in path.blocking_sources"
                :key="`btn-${src.source_table}`"
                class="link block-link"
                type="button"
                :data-testid="`mark-ready-${src.source_table}`"
                @click="markReady(src.source_table, src.partition_value)"
              >
                补齐 {{ shortTable(src.source_table) }}
              </button>
              <span v-if="!path.blocking_sources.length" class="muted-text">无阻塞</span>
            </td>
          </tr>
          <tr v-if="!analysis.paths.length">
            <td colspan="5" class="empty-state">未检索到该目标表的依赖路径配置</td>
          </tr>
        </tbody>
      </table>

      <!-- 根因聚合 -->
      <h3>根因聚合（保留全部依赖路径与仍待齐的源）</h3>
      <div class="root-grid" data-testid="root-grid">
        <article v-for="root in analysis.root_causes" :key="root.root_key" class="root-card">
          <header class="root-head">
            <code>{{ root.root_key }}</code>
            <span class="tag tag-blocking">{{ root.status }}</span>
          </header>
          <p class="root-reason">{{ root.reason }}</p>
          <p class="muted-text">
            阻塞 {{ root.blocked_path_count }} 条路径：{{ root.blocked_path_ids.join('、') }}
          </p>
          <dl class="root-pending">
            <template v-for="(pending, pathId) in root.pending_sources_by_path" :key="pathId">
              <dt>{{ pathId }} 仍待齐：</dt>
              <dd v-if="pending.length">
                <span v-for="item in pending" :key="item.source_table" class="tag tag-blocking">
                  {{ item.source_table }}@{{ item.partition_value }}
                </span>
              </dd>
              <dd v-else><span class="muted-text">无其它缺口（本路径仅缺该共同根因）</span></dd>
            </template>
          </dl>
        </article>
        <p v-if="!analysis.root_causes.length" class="empty-state">所有路径输入齐全，无阻塞根因</p>
      </div>

      <!-- 恢复计划 -->
      <h3>恢复计划编排（按源分区去重）</h3>
      <div class="recovery-bar">
        <button
          class="btn primary"
          data-testid="create-recovery-plan"
          type="button"
          :disabled="planning"
          @click="openRangeEditor"
        >
          {{ planning ? '提交中…' : '按阻塞明细生成补写计划' }}
        </button>
        <button class="btn" type="button" @click="loadPlans">刷新恢复批次</button>
        <span class="muted-text">并发提交时同一根因日期只会保留一个写入项</span>
      </div>

      <div v-if="showRangeEditor" class="modal-backdrop" data-testid="range-modal">
        <form class="modal" @submit.prevent="submitPlan">
          <h3>生成恢复计划</h3>
          <p class="muted-text">
            目标：{{ targetTable }} / {{ targetPartition }}。附加范围用于模拟同时发起的其它日期恢复，
            与根因重叠的分区不会复制写入。
          </p>
          <div v-for="(range, idx) in extraRanges" :key="idx" class="range-row">
            <input v-model="range.source_table" data-testid="range-source" placeholder="源表" required />
            <input v-model="range.start_partition" type="date" title="开始分区" required />
            <input v-model="range.end_partition" type="date" title="结束分区" required />
            <button class="link" type="button" @click="extraRanges.splice(idx, 1)">移除</button>
          </div>
          <button class="btn" type="button" @click="addRange">添加恢复范围</button>
          <div class="page-actions">
            <button class="btn" type="button" @click="showRangeEditor = false">取消</button>
            <button class="btn primary" data-testid="submit-recovery" type="submit" :disabled="planning">
              确认生成
            </button>
          </div>
        </form>
      </div>

      <table class="data-table" data-testid="recovery-table">
        <thead>
          <tr>
            <th>批次</th>
            <th>状态</th>
            <th>实际输入分区写入项</th>
            <th>去重跳过</th>
            <th>根因清单</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in plans" :key="plan.plan_id" :data-testid="`plan-row-${plan.plan_id}`">
            <td>{{ plan.plan_no }}</td>
            <td><span class="tag" :class="plan.status === '已取消' ? 'tag-muted' : 'tag-ready'">{{ plan.status }}</span></td>
            <td>
              <div v-for="item in plan.write_items" :key="`${item.源表}@${item.分区值}`" class="source-line">
                <code>{{ item.源表 }}@{{ item.分区值 }}</code>
                <span class="muted-text">{{ item.写入来源 }} {{ item.关联路径.length ? `· ${item.关联路径.join('、')}` : '' }}</span>
              </div>
              <span v-if="!plan.write_items.length" class="muted-text">无新写入项（根因已在其它批次）</span>
            </td>
            <td>
              <div v-for="skip in plan.dedup_skipped" :key="skip.dedup_key" class="source-line">
                <span class="tag tag-muted">{{ skip.source_table }}@{{ skip.partition_value }}</span>
              </div>
              <span v-if="!plan.dedup_skipped.length" class="muted-text">—</span>
            </td>
            <td>{{ plan.root_causes.join('，') || '—' }}</td>
          </tr>
          <tr v-if="!plans.length">
            <td colspan="5" class="empty-state">尚未生成恢复计划</td>
          </tr>
        </tbody>
      </table>
      <p v-if="Object.keys(writeKeyOwner).length" class="muted-text">
        写入键唯一归属核对：
        <template v-for="(owner, key) in writeKeyOwner" :key="key">
          <code>{{ key }}</code> → {{ owner }}；
        </template>
      </p>

      <!-- 补齐回查 -->
      <h3>上游补齐回查</h3>
      <div class="recovery-bar">
        <button class="btn primary" data-testid="recheck" type="button" :disabled="loading" @click="runRecheck">
          重新查询目标日
        </button>
        <span class="muted-text">回查真正能解锁的日加工任务，并核对原来哪个源分区没有产出</span>
      </div>
      <table v-if="recheck" class="data-table" data-testid="recheck-table">
        <thead>
          <tr><th>路径</th><th>本次解除的根因</th><th>剩余阻塞</th><th>结论</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in recheck.unlocked_paths" :key="`u-${item.path_id}`">
            <td>{{ item.path_id }}</td>
            <td>{{ item.cleared_root_causes.join('，') || '此前已就绪' }}</td>
            <td>—</td>
            <td>
              <span class="tag tag-ready">{{ item.newly_unlocked ? '本次解除阻塞' : '已就绪' }}</span>
            </td>
          </tr>
          <tr v-for="item in recheck.still_blocked_paths" :key="`s-${item.path_id}`">
            <td>{{ item.path_id }}</td>
            <td>{{ item.cleared_root_causes.join('，') || '—' }}</td>
            <td>
              <span v-for="blocker in item.remaining_blockers" :key="blocker" class="tag tag-blocking">
                {{ blocker }}
              </span>
            </td>
            <td><span class="tag tag-blocking">仍阻塞</span></td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 可启动日表 -->
    <h3>输入齐全、可启动的日表加工（{{ targetPartition }}）</h3>
    <table class="data-table" data-testid="runnable-table">
      <thead>
        <tr><th>目标表</th><th>路径数</th><th>已齐输入分区</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in runnableItems" :key="item.target_table">
          <td>{{ item.target_table }}</td>
          <td>{{ item.path_count }}</td>
          <td>
            <span v-for="input in item.ready_inputs" :key="input" class="tag tag-ready">{{ input }}</span>
          </td>
        </tr>
        <tr v-if="!runnableItems.length">
          <td colspan="3" class="empty-state">暂无输入齐全的日表，或尚未查询</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  analyzeBlocking,
  createRecoveryPlan,
  listRecoveryPlans,
  listRunnableTargets,
  markPartitionReady,
  recheckBlocking,
} from '@/api/blocking-service'
import type {
  BlockingAnalysis,
  RecheckResult,
  RecoveryPlan,
  RunnableTarget,
} from '@/api/blocking-service'

type PlanRow = Awaited<ReturnType<typeof listRecoveryPlans>>['plans'][number]
type ExtraRange = { source_table: string; start_partition: string; end_partition: string }

const targetTable = ref('ads.ads_sales_daily')
const targetPartition = ref('2026-11-18')
const onlyBlocked = ref(false)
const loading = ref(false)
const planning = ref(false)
const errorMessage = ref('')

const analysis = ref<BlockingAnalysis | null>(null)
const recheck = ref<RecheckResult | null>(null)
const plans = ref<PlanRow[]>([])
const writeKeyOwner = ref<Record<string, string>>({})
const runnableItems = ref<RunnableTarget[]>([])
const showRangeEditor = ref(false)
const extraRanges = ref<ExtraRange[]>([])

function shortTable(name: string): string {
  return name.includes('.') ? name.split('.').pop() ?? name : name
}

async function runAnalyze() {
  loading.value = true
  errorMessage.value = ''
  try {
    analysis.value = await analyzeBlocking(targetTable.value, targetPartition.value, onlyBlocked.value)
    await Promise.all([loadPlans(), loadRunnable()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '根因检索失败'
  } finally {
    loading.value = false
  }
}

function openRangeEditor() {
  extraRanges.value = []
  showRangeEditor.value = true
}

function addRange() {
  extraRanges.value.push({ source_table: '', start_partition: targetPartition.value, end_partition: targetPartition.value })
}

async function submitPlan() {
  planning.value = true
  errorMessage.value = ''
  try {
    const ranges = extraRanges.value
      .filter((item) => item.source_table.trim())
      .map((item) => ({ ...item }))
    const result = await createRecoveryPlan({
      target_table: targetTable.value,
      target_partition: targetPartition.value,
      reason: `值班恢复：${targetTable.value} 分区 ${targetPartition.value} 上游未齐`,
      requested_ranges: ranges,
    })
    showRangeEditor.value = false
    const plan = result.recovery_plan as RecoveryPlan
    errorMessage.value = plan.dedup_skipped_count
      ? `计划 ${plan.plan_no} 已生成：${plan.write_item_count} 个写入项，${plan.dedup_skipped_count} 个根因分区因已有写入项被去重跳过`
      : `计划 ${plan.plan_no} 已生成：${plan.write_item_count} 个写入项`
    await Promise.all([loadPlans(), runAnalyzeSilent()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复计划生成失败'
  } finally {
    planning.value = false
  }
}

async function runAnalyzeSilent() {
  analysis.value = await analyzeBlocking(targetTable.value, targetPartition.value, onlyBlocked.value)
}

async function markReady(table: string, partitionValue: string) {
  loading.value = true
  errorMessage.value = ''
  try {
    await markPartitionReady({ table, partition_value: partitionValue, producer: '值班手动补齐' })
    await runAnalyzeSilent()
    await runRecheck()
    await loadRunnable()
    errorMessage.value = `已登记 ${table}@${partitionValue} 分区就绪，并重新核对目标日`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区补齐登记失败'
  } finally {
    loading.value = false
  }
}

async function runRecheck() {
  errorMessage.value = ''
  try {
    recheck.value = await recheckBlocking(targetTable.value, targetPartition.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回查失败'
  }
}

async function loadPlans() {
  try {
    const payload = await listRecoveryPlans(targetTable.value, targetPartition.value)
    plans.value = payload.plans
    writeKeyOwner.value = payload.write_key_owner
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复批次读取失败'
  }
}

async function loadRunnable() {
  try {
    const payload = await listRunnableTargets(targetPartition.value)
    runnableItems.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可启动日表读取失败'
  }
}

onMounted(() => {
  void runAnalyze()
})
</script>

<style scoped>
.blocking-page h3 {
  margin: 20px 0 8px;
  font-size: 15px;
}
.check-label {
  flex-direction: row !important;
  align-items: center;
  gap: 6px !important;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  white-space: nowrap;
}
.tag-ready {
  background: #dcfae6;
  color: #067647;
}
.tag-blocking {
  background: #fee4e2;
  color: #b42318;
}
.tag-muted {
  background: #f2f4f7;
  color: #475467;
}
.source-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.source-line code,
.root-head code {
  font-family: monospace;
  font-size: 12px;
  background: #f8fafc;
  padding: 1px 6px;
  border-radius: 4px;
}
.source-reason {
  color: var(--muted);
  font-size: 12px;
}
.chain-cell {
  font-size: 12px;
  max-width: 280px;
}
.muted-text {
  color: var(--muted);
  font-size: 12px;
}
.block-link {
  display: block;
  margin-bottom: 4px;
}
.root-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 12px;
}
.root-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.root-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.root-reason {
  font-size: 13px;
  margin: 6px 0;
}
.root-pending dt {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}
.root-pending dd {
  margin: 4px 0 0;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.recovery-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.range-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.range-row input {
  flex: 1;
  min-width: 0;
}
.trend-down {
  color: #b42318;
}
</style>
