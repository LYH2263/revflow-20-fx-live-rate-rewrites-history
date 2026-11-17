<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/entries?kind=正向')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>正向流水</h1>
    <p class="lead">已装载的正向入账；归并后状态变为「已归并」，并盖上本趟冻结汇率与折算记账币（CNY）金额。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr><th>流水键</th><th>账户</th><th>原币金额（元）</th><th>币种</th><th>折算 CNY（元）</th><th>业务单</th><th>状态</th><th>入账时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td><span class="tag">{{ e.currency }}</span></td>
            <td>{{ e.converted_cents == null ? '—' : yuan(e.converted_cents) }}</td>
            <td>{{ e.biz_ref }}</td>
            <td><span class="tag" :class="{ on: e.status === '已归并' }">{{ e.status }}</span></td>
            <td>{{ e.booked_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
