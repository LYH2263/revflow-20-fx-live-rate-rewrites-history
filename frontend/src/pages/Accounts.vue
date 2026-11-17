<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')
const expanded = ref({})
const runDetail = ref({})

onMounted(async () => {
  try {
    rows.value = await getJson('/api/accounts')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function toggle(a) {
  const runId = a.last_settle_run_id
  if (!runId) return
  if (expanded.value[a.id]) {
    expanded.value[a.id] = false
    return
  }
  expanded.value[a.id] = true
  if (!runDetail.value[runId]) {
    const [snap, nets] = await Promise.all([
      getJson(`/api/settle-runs/${runId}/fx-snapshot`),
      getJson(`/api/settle-runs/${runId}/account-nets`),
    ])
    runDetail.value[runId] = { snap, nets: nets.account_nets }
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>账户</h1>
    <p class="lead">结算账户与归并后余额；外币账户余额已按归并趟冻结汇率折算为记账币（CNY）分。点行可回看该账户最近一趟的汇率快照与折算净额。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr><th>编码</th><th>名称</th><th>币种</th><th>折算余额（CNY，元）</th><th>最近归并</th><th>快照</th></tr>
        </thead>
        <tbody>
          <template v-for="a in rows" :key="a.id">
            <tr :class="{ clickable: a.last_settle_run_id }" @click="toggle(a)">
              <td>{{ a.code }}</td>
              <td>{{ a.name }}</td>
              <td><span class="tag">{{ a.currency }}</span></td>
              <td>{{ yuan(a.balance_cents) }}</td>
              <td>{{ a.last_settle_run_id || '—' }}</td>
              <td>
                <span v-if="a.last_settle_run_id" class="tag on">{{ expanded[a.id] ? '收起' : '查看' }}</span>
                <span v-else class="tag">未归并</span>
              </td>
            </tr>
            <tr v-if="expanded[a.id] && a.last_settle_run_id && runDetail[a.last_settle_run_id]" class="detail-row">
              <td colspan="6">
                <div class="fx-detail">
                  <h3>归并 #{{ a.last_settle_run_id }} 冻结汇率（{{ runDetail[a.last_settle_run_id].snap.frozen_at }}）</h3>
                  <table class="sub">
                    <thead><tr><th>币种</th><th>折记账币</th><th>冻结汇率</th><th>来源</th><th>快照#</th></tr></thead>
                    <tbody>
                      <tr v-for="fx in runDetail[a.last_settle_run_id].snap.rates" :key="fx.id">
                        <td>{{ fx.base_currency }}</td>
                        <td>{{ fx.quote_currency }}</td>
                        <td class="mono">{{ fx.rate_text }}</td>
                        <td>{{ fx.source }}</td>
                        <td>{{ fx.id }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <h3>本账户本趟折算净额</h3>
                  <table class="sub">
                    <thead><tr><th>币种</th><th>原币净额（分）</th><th>折算净额（CNY 分）</th><th>折算净额（元）</th></tr></thead>
                    <tbody>
                      <tr v-for="n in runDetail[a.last_settle_run_id].nets.filter(x => x.account_id === a.id)" :key="n.id">
                        <td>{{ n.currency }}</td>
                        <td class="mono">{{ n.net_cents }}</td>
                        <td class="mono">{{ n.net_converted_cents }}</td>
                        <td>{{ yuan(n.net_converted_cents) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </td>
            </tr>
          </template>
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
