<script setup lang="ts">
/** 预检面板：逐项清单（通过/警告/失败 色点+文字）+ 修复指引 + 受影响功能 */
import { NCollapse, NCollapseItem, NEmpty, NTag } from 'naive-ui'
import type { PrecheckItem } from '../api'

const props = defineProps<{ items: PrecheckItem[]; overall: string; checkedAt?: string }>()

const LEVEL_TEXT: Record<string, string> = { pass: '通过', warn: '警告', fail: '失败' }

const counts = () => ({
  pass: props.items.filter((i) => i.level === 'pass').length,
  warn: props.items.filter((i) => i.level === 'warn').length,
  fail: props.items.filter((i) => i.level === 'fail').length,
})
</script>

<template>
  <div>
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px">
      <span class="status-pill" :class="overall">
        <span class="dot" />{{ overall === 'pass' ? '预检通过' : overall === 'warn' ? '有警告' : '有失败项' }}
      </span>
      <span class="mono" style="color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-micro)" v-if="checkedAt">
        {{ checkedAt }}
      </span>
      <span style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-text-tertiary)">
        通过 {{ counts().pass }} · 警告 {{ counts().warn }} · 失败 {{ counts().fail }}
      </span>
    </div>
    <n-empty v-if="items.length === 0" description="尚未执行预检" size="small" />
    <n-collapse v-else>
      <n-collapse-item v-for="item in items" :key="item.key" :name="item.key">
        <template #header>
          <span class="status-pill" :class="item.level">
            <span class="dot" />{{ LEVEL_TEXT[item.level] }}
          </span>
          <span style="margin-left: 8px">{{ item.message }}</span>
        </template>
        <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-secondary)">
          <div v-if="item.affected && item.affected.length" style="color: var(--Semantic-Color-error); margin-bottom: 4px">
            权限/条件不足将影响：{{ item.affected.join('、') }}
          </div>
          <div v-if="item.hint">修复指引：{{ item.hint }}</div>
          <div v-else style="color: var(--Semantic-Color-text-tertiary)">无需处理</div>
        </div>
      </n-collapse-item>
    </n-collapse>
  </div>
</template>
