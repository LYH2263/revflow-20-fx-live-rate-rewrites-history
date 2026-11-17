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
    const [snap, nets, proofs] = await Promise.all([
      getJson(`/api/settle-runs/${runId}/fx-snapshot`),
      getJson(`/api/settle-runs/${runId}/account-nets`),
      getJson(`/api/settle-runs/${runId}/conversion-proofs`),
    ])
    runDetail.value[runId] = {
      snap,
      nets: nets.account_nets,
      proofs: proofs.proofs,
    }
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>账户</h1>
    <p class="lead">结算账户与归并后余额；外币账户余额已按归并趟钉扎汇率折算为记账币（CNY）分。点行可回看该账户最近一趟的钉扎汇率、折算证明与折算净额。</p>
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
                  <h3>归并 #{{ a.last_settle_run_id }} 钉扎汇率（{{ runDetail[a.last_settle_run_id].snap.frozen_at }}）</h3>
                  <table class="sub">
                    <thead><tr><th>币种</th><th>折记账币</th><th>钉扎汇率</th><th>来源</th><th>快照#</th></tr></thead>
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
                  <h3>本账户折算证明（钉扎汇率与触及流水键 · 落库不可改删）</h3>
                  <table class="sub">
                    <thead><tr><th>币种</th><th>钉扎汇率</th><th>折算前本币合计（分）</th><th>折算后本币合计（CNY 分）</th><th>本账户触及流水键</th></tr></thead>
                    <tbody>
                      <template v-for="p in runDetail[a.last_settle_run_id].proofs" :key="p.id">
                        <tr v-if="p.entries.some(pe => pe.account_id === a.id)">
                          <td><span class="tag">{{ p.base_currency }}</span></td>
                          <td class="mono">{{ p.pinned_rate_text }}</td>
                          <td class="mono">{{ p.native_total_cents }}</td>
                          <td class="mono">{{ p.converted_total_cents }}</td>
                          <td class="keys">
                            <span
                              v-for="pe in p.entries.filter(x => x.account_id === a.id)"
                              :key="pe.id"
                              class="key-chip"
                              :class="{ rev: pe.sign < 0 }"
                            >
                              {{ pe.entry_key }}<em v-if="pe.sign < 0">（冲正-）</em>
                            </span>
                          </td>
                        </tr>
                      </template>
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
.keys { white-space: normal; line-height: 1.9; }
.key-chip {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 1px 7px;
  border: 1px solid var(--line, #d8dee6);
  border-radius: 10px;
  background: #f2f6fb;
  font-family: var(--mono);
  font-size: 11.5px;
}
.key-chip.rev { background: #fdf3f2; border-color: #e3c4c0; }
.key-chip em { font-style: normal; color: #b04a3f; }
</style>
