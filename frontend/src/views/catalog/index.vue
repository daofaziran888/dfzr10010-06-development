<template>
  <section class="page" data-module="catalog">
    <header class="page-head">
      <div>
        <h2>数据目录同步管理</h2>
        <p class="page-desc">读取同步作业生成增量批次与增量水位，先做字段漂移预检再确认；目录元数据与增量水位同成同退，重复批次只写入一次。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="openWatermarks">查看增量水位</button>
        <button class="btn" type="button" @click="openMetadata">目录元数据</button>
        <button class="btn" type="button" @click="openAudit">元数据审计清单</button>
        <button class="btn" type="button" @click="exportRows">导出同步清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="toolbar">
      <label class="check-cell"><input type="checkbox" :checked="allSelected" :indeterminate="someSelected" @change="toggleAll" /></label>
      <button class="btn primary" type="button" :disabled="!selectedIds.length" @click="generateSelected">
        读取作业并生成增量批次（{{ selectedIds.length }}）
      </button>
      <button class="btn primary" type="button" :disabled="!selectedIds.length" @click="bulkConfirmSelected">
        整组提交确认
      </button>
      <button class="btn" type="button" @click="reload">刷新同步列表</button>
      <span class="tag-soft">支持批量选择多条同步任务，一次提交后分别保留结果</span>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th class="check-cell">选择</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>确认水位 / 批次版本</th>
          <th>同步批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td class="check-cell"><input type="checkbox" :value="row.id" v-model="selectedIds" /></td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <div>{{ row['确认水位'] ?? '0' }}</div>
            <div class="tag-soft">{{ row['水位批次版本'] ?? '未初始化' }}</div>
            <div v-if="row['作业状态提示']" class="tag-soft">{{ row['作业状态提示'] }}</div>
          </td>
          <td><button class="link" type="button" @click="openBatches(row)">同步批次</button></td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无数据目录同步数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据目录同步记录</span>
      <span v-if="message" :class="messageOk ? 'success-text' : 'error-text'">{{ message }}</span>
    </footer>

    <!-- 同步批次清单 -->
    <div v-if="modal === 'batches'" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>同步批次 · {{ currentJob?.['作业编号'] }}</h3>
          <button class="modal-close" type="button" @click="closeModal">×</button>
        </div>
        <table class="data-table">
          <thead>
            <tr><th>批次号</th><th>批次版本</th><th>批次状态</th><th>确认边界水位</th><th>新水位</th><th>待处理冲突</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="batch in batchList" :key="batch.id">
              <td>{{ batch['批次号'] }}</td>
              <td>{{ batch['批次版本'] }}</td>
              <td><span :class="statusBadge(batch['批次状态'])">{{ batch['批次状态'] }}</span></td>
              <td>{{ batch['确认边界水位'] }}</td>
              <td>{{ batch['新水位'] }}</td>
              <td>{{ batch['待处理冲突数'] }}</td>
              <td><button class="link" type="button" @click="openBatchDetail(batch)">批次核验</button></td>
            </tr>
            <tr v-if="!batchList.length">
              <td colspan="7" class="empty-state">尚未生成增量批次，回到同步列表选择作业后生成</td>
            </tr>
          </tbody>
        </table>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn primary" type="button" @click="generateForCurrentJob">读取作业生成增量批次</button>
          <button class="btn" type="button" @click="closeModal">回到同步列表</button>
        </div>
      </div>
    </div>

    <!-- 批次详情：字段漂移预检 + 逐条核验 + 确认/忽略 -->
    <div v-if="modal === 'batchDetail' && currentBatch" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>批次核验 · {{ currentBatch['批次号'] }}（{{ currentBatch['批次版本'] }}）</h3>
          <button class="modal-close" type="button" @click="backToBatches">×</button>
        </div>
        <div class="kv-grid">
          <div class="kv-item"><span class="k">批次状态</span><span :class="statusBadge(currentBatch['批次状态'])">{{ currentBatch['批次状态'] }}</span></div>
          <div class="kv-item"><span class="k">增量字段</span><span class="v">{{ currentBatch['增量字段'] }}</span></div>
          <div class="kv-item"><span class="k">确认边界水位</span><span class="v">{{ currentBatch['确认边界水位'] }}</span></div>
          <div class="kv-item"><span class="k">确认后新水位</span><span class="v">{{ currentBatch['新水位'] }}</span></div>
        </div>
        <div class="sub-title">字段漂移预检（源字段类型 vs 目录字段类型，逐条核验）</div>
        <table class="data-table">
          <thead>
            <tr><th>字段名</th><th>源字段类型</th><th>目录字段类型</th><th>漂移结论</th><th>核验状态</th><th>逐条处理</th></tr>
          </thead>
          <tbody>
            <tr v-for="field in currentBatch['漂移预检']" :key="field['字段名']">
              <td>{{ field['字段名'] }} <span v-if="field['增量字段']" class="badge info">增量字段</span></td>
              <td>{{ field['源字段类型'] }}</td>
              <td>{{ field['目录字段类型'] }}</td>
              <td><span :class="driftBadge(field['漂移结论'])">{{ field['漂移结论'] }}</span></td>
              <td>{{ field['核验状态'] }}</td>
              <td>
                <template v-if="field['漂移结论'] === '版本冲突' && isOpenBatch(currentBatch)">
                  <button class="link" type="button" @click="resolveField(field['字段名'], true)">接收兼容字段</button>
                  <button class="link" type="button" @click="resolveField(field['字段名'], false)">不接收</button>
                </template>
                <span v-else class="tag-soft">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="toolbar" style="margin-top:14px">
          <button class="btn primary" type="button" :disabled="!isOpenBatch(currentBatch)" @click="confirmBatch(currentBatch.id)">
            确认同步并推进水位
          </button>
          <button class="btn" type="button" :disabled="!isOpenBatch(currentBatch)" @click="ignoreBatch(currentBatch.id)">
            停在待处理边界（忽略批次）
          </button>
          <button class="btn" type="button" :disabled="currentBatch['批次状态'] !== '已确认'" @click="businessConsume(currentBatch.id)">
            业务面消费同一版本水位
          </button>
          <button class="btn ghost" type="button" @click="backToBatches">返回批次清单</button>
        </div>
      </div>
    </div>

    <!-- 增量水位 -->
    <div v-if="modal === 'watermarks'" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>增量水位（仅来自已确认批次）</h3>
          <button class="modal-close" type="button" @click="closeModal">×</button>
        </div>
        <table class="data-table">
          <thead><tr><th>作业编号</th><th>增量字段</th><th>确认水位</th><th>水位批次版本</th><th>待处理批次号</th></tr></thead>
          <tbody>
            <tr v-for="wm in watermarks" :key="wm['作业编号']">
              <td>{{ wm['作业编号'] }}</td>
              <td>{{ wm['增量字段'] }}</td>
              <td>{{ wm['确认水位'] }}</td>
              <td>{{ wm['水位批次版本'] }}</td>
              <td>{{ wm['待处理批次号'] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn primary" type="button" @click="backToListThenRefresh">回到同步列表并刷新</button>
        </div>
      </div>
    </div>

    <!-- 目录元数据清单 -->
    <div v-if="modal === 'metadata'" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>目录元数据（每条只对应一个完整确认批次）</h3>
          <button class="modal-close" type="button" @click="closeModal">×</button>
        </div>
        <table class="data-table">
          <thead><tr><th>表名称</th><th>作业编号</th><th>批次版本</th><th>确认水位</th><th>增量字段</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="meta in metadataList" :key="meta.id">
              <td>{{ meta['表名称'] }}</td>
              <td>{{ meta['作业编号'] }}</td>
              <td>{{ meta['批次版本'] }}</td>
              <td>{{ meta['确认水位'] }}</td>
              <td>{{ meta['增量字段'] }}</td>
              <td><button class="link" type="button" @click="openMetaDetail(meta)">元数据详情</button></td>
            </tr>
            <tr v-if="!metadataList.length">
              <td colspan="6" class="empty-state">暂无已确认批次，目录元数据尚未写入</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 元数据详情 -->
    <div v-if="modal === 'metaDetail' && currentMeta" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>元数据详情 · {{ currentMeta['表名称'] }}</h3>
          <button class="modal-close" type="button" @click="closeModal">×</button>
        </div>
        <div class="kv-grid">
          <div class="kv-item"><span class="k">作业编号</span><span class="v">{{ currentMeta['作业编号'] }}</span></div>
          <div class="kv-item"><span class="k">批次版本</span><span class="v">{{ currentMeta['批次版本'] }}</span></div>
          <div class="kv-item"><span class="k">确认水位</span><span class="v">{{ currentMeta['确认水位'] }}</span></div>
          <div class="kv-item"><span class="k">增量字段</span><span class="v">{{ currentMeta['增量字段'] }}</span></div>
          <div class="kv-item"><span class="k">源路径</span><span class="v">{{ currentMeta['源路径'] }}</span></div>
          <div class="kv-item"><span class="k">目标目录</span><span class="v">{{ currentMeta['目标目录'] }}</span></div>
        </div>
        <div class="sub-title">字段清单（来自同一确认批次）</div>
        <table class="data-table">
          <thead><tr><th>字段名</th><th>字段类型</th></tr></thead>
          <tbody>
            <tr v-for="field in currentMeta['字段清单']" :key="field['字段名']">
              <td>{{ field['字段名'] }}</td>
              <td>{{ field['字段类型'] }}</td>
            </tr>
          </tbody>
        </table>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn" type="button" @click="openAuditFromMeta">查看元数据审计清单（同版本）</button>
        </div>
      </div>
    </div>

    <!-- 元数据审计清单 -->
    <div v-if="modal === 'audit'" class="modal-mask" @click.self="closeModal">
      <div class="modal-card">
        <div class="modal-head">
          <h3>元数据审计清单</h3>
          <button class="modal-close" type="button" @click="closeModal">×</button>
        </div>
        <table class="data-table">
          <thead><tr><th>批次号</th><th>批次版本</th><th>作业编号</th><th>消费方</th><th>水位版本</th><th>审计状态</th><th>字段结论</th><th>逐条核验</th></tr></thead>
          <tbody>
            <tr v-for="record in auditList" :key="record.id">
              <td>{{ record['批次号'] }}</td>
              <td>{{ record['批次版本'] }}</td>
              <td>{{ record['作业编号'] }}</td>
              <td><span class="badge info">{{ record['消费方'] }}</span></td>
              <td>{{ record['水位版本'] }}</td>
              <td><span :class="record['审计状态'] === '已核验' ? 'badge ok' : 'badge warn'">{{ record['审计状态'] }}</span></td>
              <td>
                <span v-for="field in record['字段结论']" :key="field['字段名']" class="badge" :class="driftBadge(field['漂移结论'])" style="margin-right:4px">
                  {{ field['字段名'] }}：{{ field['漂移结论'] }}
                </span>
              </td>
              <td>
                <button class="link" type="button" :disabled="record['审计状态'] === '已核验'" @click="verifyAudit(record.id)">
                  {{ record['审计状态'] === '已核验' ? '已核验' : '核验' }}
                </button>
              </td>
            </tr>
            <tr v-if="!auditList.length">
              <td colspan="8" class="empty-state">暂无审计记录，确认批次后元数据服务会自动写入</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type DriftField = {
  '字段名': string
  '增量字段': boolean
  '源字段类型': string
  '目录字段类型': string
  '漂移结论': string
  '核验状态': string
}
type SyncBatch = {
  id: number
  '作业id': number
  '作业编号': string
  '批次号': string
  '批次版本': string
  '批次状态': string
  '增量字段': string
  '确认边界水位': string
  '新水位': string
  '漂移预检': DriftField[]
  '待处理冲突数': number
  '元数据记录id': number | null
}
type Watermark = {
  '作业编号': string
  '增量字段': string
  '确认水位': string
  '水位批次版本': string
  '待处理批次号': string | null
}
type CatalogMeta = {
  id: number
  '作业编号': string
  '源路径': string
  '目标目录': string
  '表名称': string
  '字段清单': { '字段名': string; '字段类型': string }[]
  '增量字段': string
  '批次版本': string
  '确认水位': string
}
type AuditRecord = {
  id: number
  '批次号': string
  '批次版本': string
  '作业编号': string
  '消费方': string
  '水位版本': string
  '审计状态': string
  '字段结论': DriftField[]
}
type ActionResponse = { ok: boolean; message: string; entry: Record<string, unknown> | null }

const ENDPOINT = '/api/catalog'
const columns = ['作业编号', '源路径', '目标目录', '同步模式', '增量字段', '过滤规则', '同步频率', '作业状态']
const actions = ['启动同步', '暂停同步', '归档作业']
const stats = ref([
  { label: '同步作业总数', value: 0 },
  { label: '待确认批次', value: 0 },
  { label: '已确认批次', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const message = ref('')
const messageOk = ref(true)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const selectedIds = ref<number[]>([])

const modal = ref<'' | 'batches' | 'batchDetail' | 'watermarks' | 'metadata' | 'metaDetail' | 'audit'>('')
const currentJob = ref<Row | null>(null)
const batchList = ref<SyncBatch[]>([])
const currentBatch = ref<SyncBatch | null>(null)
const watermarks = ref<Watermark[]>([])
const metadataList = ref<CatalogMeta[]>([])
const currentMeta = ref<CatalogMeta | null>(null)
const auditList = ref<AuditRecord[]>([])

const allSelected = computed(() => rows.value.length > 0 && selectedIds.value.length === rows.value.length)
const someSelected = computed(() => selectedIds.value.length > 0 && !allSelected.value)

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function statusBadge(status: string) {
  if (status === '已确认') return 'badge ok'
  if (status === '已阻塞' || status === '已忽略') return 'badge blocked'
  if (status === '待处理') return 'badge warn'
  return 'badge'
}

function driftBadge(conclusion: string) {
  if (conclusion === '一致') return 'badge ok'
  if (conclusion === '兼容可接收') return 'badge info'
  return 'badge blocked'
}

function isOpenBatch(batch: SyncBatch) {
  return batch['批次状态'] === '待处理' || batch['批次状态'] === '已阻塞'
}

async function post(path: string, body?: unknown): Promise<ActionResponse> {
  const response = await request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
  return (await response.json()) as ActionResponse
}

async function getJson<T>(path: string): Promise<{ items: T[]; total: number }> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as { items: T[]; total: number }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function toggleAll(event: Event) {
  selectedIds.value = (event.target as HTMLInputElement).checked ? rows.value.map((r) => Number(r.id)) : []
}

function closeModal() {
  modal.value = ''
  currentJob.value = null
  currentBatch.value = null
  currentMeta.value = null
}

async function reload() {
  message.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const payload = await getJson<Row>(`${ENDPOINT}?${query}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    selectedIds.value = selectedIds.value.filter((id) => rows.value.some((r) => Number(r.id) === id))
    const batchPayload = await getJson<SyncBatch>(`${ENDPOINT}/sync/batches`)
    const batches = batchPayload.items ?? []
    stats.value = [
      { label: '同步作业总数', value: rows.value.length },
      { label: '待确认批次', value: batches.filter((b) => isOpenBatch(b)).length },
      { label: '已确认批次', value: batches.filter((b) => b['批次状态'] === '已确认').length },
    ]
  } catch (error) {
    flash(error instanceof Error ? error.message : '数据目录同步列表读取失败', false)
  }
}

async function runAction(action: string, row: Row) {
  try {
    const result = await post(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
    flash(result.message, result.ok)
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '数据目录同步操作失败', false)
  }
}

// 批量选择：对每个选中作业读取并生成增量批次，结果分别提示
async function generateSelected() {
  const ids = [...selectedIds.value]
  const summaries: string[] = []
  for (const jobId of ids) {
    const result = await post(`${ENDPOINT}/sync/jobs/${jobId}/generate`)
    summaries.push(`作业${jobId}：${result.message}`)
  }
  flash(summaries.join('；'), true)
  await reload()
  if (ids.length === 1) {
    const job = rows.value.find((r) => Number(r.id) === ids[0]) ?? null
    if (job) await openBatches(job)
  }
}

async function generateForCurrentJob() {
  if (!currentJob.value) return
  const result = await post(`${ENDPOINT}/sync/jobs/${currentJob.value.id}/generate`)
  flash(result.message, result.ok)
  await loadBatches(Number(currentJob.value.id))
  await reload()
}

// 整组提交：取选中作业的在途批次一次提交，后端分别保留每个批次结果
async function bulkConfirmSelected() {
  const allBatches = (await getJson<SyncBatch>(`${ENDPOINT}/sync/batches`)).items
  const batchIds = allBatches
    .filter((b) => selectedIds.value.includes(b['作业id']) && isOpenBatch(b))
    .map((b) => b.id)
  if (!batchIds.length) {
    flash('所选作业没有待确认或已阻塞的在途批次', false)
    return
  }
  const result = await post(`${ENDPOINT}/sync/batches/confirm-bulk`, { batch_ids: batchIds })
  const detail = result.entry as { results: { ok: boolean; message: string }[] } | null
  const failed = detail?.results.filter((r) => !r.ok).length ?? 0
  flash(`${result.message}${failed ? `；${failed} 个批次停在待处理边界` : ''}`, result.ok)
  await reload()
}

async function openBatches(job: Row) {
  currentJob.value = job
  modal.value = 'batches'
  await loadBatches(Number(job.id))
}

async function loadBatches(jobId: number) {
  const payload = await getJson<SyncBatch>(`${ENDPOINT}/sync/batches?job_id=${jobId}`)
  batchList.value = payload.items ?? []
}

async function openBatchDetail(batch: SyncBatch) {
  const fresh = await getJson<SyncBatch>(`${ENDPOINT}/sync/batches`)
  currentBatch.value = fresh.items.find((b) => b.id === batch.id) ?? batch
  modal.value = 'batchDetail'
}

function backToBatches() {
  if (!currentBatch.value) {
    closeModal()
    return
  }
  modal.value = 'batches'
  void loadBatches(currentBatch.value['作业id'])
  currentBatch.value = null
}

async function resolveField(fieldName: string, receive: boolean) {
  if (!currentBatch.value) return
  const result = await post(`${ENDPOINT}/sync/batches/${currentBatch.value.id}/fields/resolve`, {
    field_name: fieldName,
    receive,
  })
  flash(result.message, result.ok)
  if (result.entry) currentBatch.value = result.entry as unknown as SyncBatch
  await reload()
}

async function confirmBatch(batchId: number) {
  const result = await post(`${ENDPOINT}/sync/batches/${batchId}/confirm`)
  flash(result.message, result.ok)
  if (result.entry) currentBatch.value = result.entry as unknown as SyncBatch
  await reload()
  if (currentJob.value) await loadBatches(Number(currentJob.value.id))
}

async function ignoreBatch(batchId: number) {
  const result = await post(`${ENDPOINT}/sync/batches/${batchId}/ignore`)
  flash(result.message, result.ok)
  if (result.entry) currentBatch.value = result.entry as unknown as SyncBatch
  await reload()
  if (currentJob.value) await loadBatches(Number(currentJob.value.id))
}

async function businessConsume(batchId: number) {
  const result = await post(`${ENDPOINT}/sync/batches/${batchId}/business-consume`)
  flash(result.message, result.ok)
}

async function openWatermarks() {
  const payload = await getJson<Watermark>(`${ENDPOINT}/sync/watermarks`)
  watermarks.value = payload.items ?? []
  modal.value = 'watermarks'
}

// 核对水位后先回到同步列表，再刷新；之后可重新打开元数据详情核对同一版本
async function backToListThenRefresh() {
  closeModal()
  await reload()
  flash('已回到同步列表并刷新，可打开目录元数据核对同一批次版本', true)
}

async function openMetadata() {
  const payload = await getJson<CatalogMeta>(`${ENDPOINT}/sync/metadata`)
  metadataList.value = payload.items ?? []
  modal.value = 'metadata'
}

function openMetaDetail(meta: CatalogMeta) {
  currentMeta.value = meta
  modal.value = 'metaDetail'
}

async function openAudit() {
  const payload = await getJson<AuditRecord>(`${ENDPOINT}/sync/audit`)
  auditList.value = payload.items ?? []
  modal.value = 'audit'
}

async function openAuditFromMeta() {
  const payload = await getJson<AuditRecord>(`${ENDPOINT}/sync/audit`)
  auditList.value = payload.items ?? []
  if (currentMeta.value) {
    auditList.value = auditList.value.filter((r) => r['批次版本'] === currentMeta.value!['批次版本'])
  }
  modal.value = 'audit'
}

async function verifyAudit(auditId: number) {
  const result = await post(`${ENDPOINT}/sync/audit/${auditId}/verify`)
  flash(result.message, result.ok)
  const payload = await getJson<AuditRecord>(`${ENDPOINT}/sync/audit`)
  auditList.value = payload.items ?? []
}

onMounted(reload)
</script>
