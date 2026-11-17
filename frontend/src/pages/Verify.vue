<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const expanded = ref({})

async function load() {
  rows.value = await getJson('/api/verify-reports')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runVerify() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/verify-reports')
    msg.value = r.consistent
      ? `互证一致：流水净额 ${(r.ledger_net_cents / 100).toFixed(2)} = 余额合计（记账币 CNY）`
      : `互证不一致：流水 ${(r.ledger_net_cents / 100).toFixed(2)} vs 余额 ${(r.balance_sum_cents / 100).toFixed(2)}`
    expanded.value = {}
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function detail(r) {
  if (!r.detail_json) return null
  try {
    return JSON.parse(r.detail_json)
  } catch {
    return null
  }
}

function toggle(id) {
  expanded.value[id] = !expanded.value[id]
}
</script>

<template>
  <div>
    <h1>净额互证</h1>
    <p class="lead">核对「已归并流水折算净额」与「账户余额合计」，并展示本趟冻结汇率——报告汇率与落库快照同源，逐条折算复算留痕。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runVerify">跑净额互证</button>
    </div>
    <p v-if="msg" :class="msg.includes('一致') && !msg.includes('不一致') ? 'ok' : 'msg'">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>互证报告</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>归并#</th><th>流水净额</th><th>余额合计</th><th>隔离数</th><th>一致</th><th>时间</th><th>冻结汇率</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in rows" :key="r.id">
            <tr class="clickable" @click="toggle(r.id)">
              <td>{{ r.id }}</td>
              <td>{{ r.settle_run_id }}</td>
              <td>{{ yuan(r.ledger_net_cents) }}</td>
              <td>{{ yuan(r.balance_sum_cents) }}</td>
              <td>{{ r.quarantine_count }}</td>
              <td>
                <span class="tag" :class="r.consistent ? 'on' : 'bad'">
                  {{ r.consistent ? '是' : '否' }}
                </span>
              </td>
              <td>{{ r.created_at }}</td>
              <td><span class="tag on">{{ expanded[r.id] ? '收起' : '查看' }}</span></td>
            </tr>
            <tr v-if="expanded[r.id] && detail(r)" class="detail-row">
              <td colspan="8">
                <div class="fx-detail">
                  <h3>本趟冻结汇率（与 fx_rate_snapshots 同源 · 记账币 {{ detail(r)['记账币'] }}）</h3>
                  <table class="sub">
                    <thead>
                      <tr><th>币种</th><th>折记账币</th><th>冻结汇率</th><th>来源</th><th>快照#</th><th>冻结时刻</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="fx in detail(r)['本趟冻结汇率']" :key="fx['快照id']">
                        <td>{{ fx['币种'] }}</td>
                        <td>{{ fx['折记账币'] }}</td>
                        <td class="mono">{{ fx['冻结汇率'] }}</td>
                        <td>{{ fx['来源'] }}</td>
                        <td>{{ fx['快照id'] }}</td>
                        <td>{{ fx['冻结时刻'] }}</td>
                      </tr>
                    </tbody>
                  </table>

                  <h3>同源与净额检查</h3>
                  <ul class="checks">
                    <li>快照归属同源：<span class="tag" :class="detail(r)['同源检查_快照归属'] ? 'on' : 'bad'">{{ detail(r)['同源检查_快照归属'] ? '通过' : '失败' }}</span></li>
                    <li>逐条折算复算：<span class="tag" :class="detail(r)['同源检查_逐条折算复算'] ? 'on' : 'bad'">{{ detail(r)['同源检查_逐条折算复算'] ? '通过' : '失败' }}</span></li>
                    <li>本趟净额一致：<span class="tag" :class="detail(r)['本趟净额一致性'] ? 'on' : 'bad'">{{ detail(r)['本趟净额一致性'] ? '通过' : '失败' }}</span> <em class="muted">重算 {{ detail(r)['本趟流水净额分(折算重算)'] }} = 运行 {{ detail(r)['运行记录净额分'] }}</em></li>
                    <li>余额跨趟重算：<span class="tag" :class="detail(r)['余额跨趟重算一致性'] ? 'on' : 'bad'">{{ detail(r)['余额跨趟重算一致性'] ? '通过' : '失败' }}</span></li>
                  </ul>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!rows.length"><td colspan="8">尚无报告</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.clickable { cursor: pointer; }
.detail-row > td { background: #f7fafd; padding: 16px 18px; }
.fx-detail h3 {
  margin: 12px 0 8px;
  font-family: var(--display);
  font-size: 14px;
  letter-spacing: 0.06em;
}
.fx-detail h3:first-child { margin-top: 0; }
table.sub { font-size: 12.5px; }
.mono { font-family: var(--mono); }
.checks { margin: 0; padding-left: 18px; font-size: 13px; line-height: 2; }
.checks .muted { color: var(--muted); font-family: var(--mono); font-size: 12px; }
</style>
