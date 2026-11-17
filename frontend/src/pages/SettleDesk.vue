<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const expanded = ref({})
const runDetail = ref({})

async function load() {
  runs.value = await getJson('/api/settle-runs')
}

async function toggle(runId) {
  if (expanded.value[runId]) {
    expanded.value[runId] = false
    return
  }
  expanded.value[runId] = true
  if (!runDetail.value[runId]) {
    const [snap, nets] = await Promise.all([
      getJson(`/api/settle-runs/${runId}/fx-snapshot`),
      getJson(`/api/settle-runs/${runId}/account-nets`),
    ])
    runDetail.value[runId] = { snap, nets: nets.account_nets }
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：折算净额 ${(r.net_cents / 100).toFixed(2)}（记账币 CNY），隔离 ${r.quarantine_count}`
    runDetail.value = {}
    await load()
    expanded.value = { [r.id]: true }
    await toggle(r.id)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：开跑先冻结本趟汇率集，全部折算只读冻结快照，配平落余额，未平/超额进隔离。</p>
    <p class="note-strip">本趟冻结的「币种→记账币(CNY)」汇率落库后不可改删；点运行行可回看冻结快照与每账户折算净额。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th><th>折算净额（元）</th><th>结束</th><th>汇率快照</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in runs" :key="r.id">
            <tr class="clickable" @click="toggle(r.id)">
              <td>{{ r.id }}</td>
              <td>{{ r.status }}</td>
              <td>{{ r.forward_count }}</td>
              <td>{{ r.reversal_count }}</td>
              <td>{{ r.settled_count }}</td>
              <td>{{ r.quarantine_count }}</td>
              <td>{{ yuan(r.net_cents) }}</td>
              <td>{{ r.finished_at || '—' }}</td>
              <td><span class="tag on">{{ expanded[r.id] ? '收起' : '查看' }}</span></td>
            </tr>
            <tr v-if="expanded[r.id] && runDetail[r.id]" class="detail-row">
              <td colspan="9">
                <div class="fx-detail">
                  <h3>本趟冻结汇率（快照 · 记账币 {{ runDetail[r.id].snap.quote_currency }} · {{ runDetail[r.id].snap.frozen_at }}）</h3>
                  <table class="sub">
                    <thead>
                      <tr><th>币种</th><th>折记账币</th><th>冻结汇率</th><th>来源</th><th>快照#</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="fx in runDetail[r.id].snap.rates" :key="fx.id">
                        <td>{{ fx.base_currency }}</td>
                        <td>{{ fx.quote_currency }}</td>
                        <td class="mono">{{ fx.rate_text }}</td>
                        <td>{{ fx.source }}</td>
                        <td>{{ fx.id }}</td>
                      </tr>
                    </tbody>
                  </table>

                  <h3>每账户折算净额</h3>
                  <table class="sub">
                    <thead>
                      <tr><th>账户</th><th>币种</th><th>原币净额（分）</th><th>折算净额（记账币分）</th><th>折算净额（元）</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="n in runDetail[r.id].nets" :key="n.id">
                        <td>{{ n.account_code }} · {{ n.account_name }}</td>
                        <td>{{ n.currency }}</td>
                        <td class="mono">{{ n.net_cents }}</td>
                        <td class="mono">{{ n.net_converted_cents }}</td>
                        <td>{{ yuan(n.net_converted_cents) }}</td>
                      </tr>
                      <tr v-if="!runDetail[r.id].nets.length"><td colspan="5">本趟无配平净额（全部隔离或无待归并）</td></tr>
                    </tbody>
                  </table>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!runs.length"><td colspan="9">尚无归并运行</td></tr>
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
</style>
