<script setup lang="ts">
/** 数据源新建/编辑抽屉：三段折叠（连接信息 / SSL / 高级），密码只写不回显 */
import { computed, reactive, ref, watch } from 'vue'
import { NButton, NCollapse, NCollapseItem, NDrawer, NDrawerContent, NForm, NFormItem, NInput, NInputNumber, NSelect, useMessage } from 'naive-ui'
import { dsApi } from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{ show: boolean; editing: import('../api').Datasource | null }>()
const emit = defineEmits<{ (e: 'update:show', v: boolean): void; (e: 'saved'): void }>()

const message = useMessage()
const saving = ref(false)

const form = reactive({
  name: '', host: '', port: 3306, username: '', password: '',
  ssl_mode: 'disabled', default_schema: '', charset: 'utf8mb4',
  connect_timeout: 10, note: '',
})

watch(() => props.show, (v) => {
  if (!v) return
  const e = props.editing
  form.name = e?.name ?? ''
  form.host = e?.host ?? ''
  form.port = e?.port ?? 3306
  form.username = e?.username ?? ''
  form.password = ''                       // 密码只写不回显：留空 = 不变更
  form.ssl_mode = e?.ssl_mode ?? 'disabled'
  form.default_schema = e?.default_schema ?? ''
  form.charset = e?.charset ?? 'utf8mb4'
  form.connect_timeout = e?.connect_timeout ?? 10
  form.note = e?.note ?? ''
})

const isEdit = computed(() => !!props.editing)

async function save() {
  if (!form.name || !form.host || !form.username) {
    message.warning('名称、主机、用户名为必填项')
    return
  }
  if (!isEdit.value && !form.password) {
    message.warning('请输入密码')
    return
  }
  saving.value = true
  try {
    const body: Record<string, unknown> = { ...form, port: Number(form.port) }
    if (isEdit.value && !form.password) delete body.password // 编辑留空 = 不变更
    if (isEdit.value) await dsApi.update(props.editing!.id, body)
    else await dsApi.create(body)
    message.success(isEdit.value ? '已保存' : '已创建')
    emit('update:show', false)
    emit('saved')
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    saving.value = false
  }
}

const sslOptions = [
  { label: '关闭', value: 'disabled' },
  { label: '仅加密', value: 'encrypt' },
  { label: 'CA 证书验证', value: 'verify_ca' },
  { label: '双向证书', value: 'full' },
]

// 连接字符集：常用值预置，filterable+tag 允许手输服务端支持的任意字符集
const charsetOptions = [
  { label: 'utf8mb4（推荐）', value: 'utf8mb4' },
  { label: 'utf8 / utf8mb3', value: 'utf8' },
  { label: 'gbk', value: 'gbk' },
  { label: 'gb2312', value: 'gb2312' },
  { label: 'latin1', value: 'latin1' },
  { label: 'binary', value: 'binary' },
]
</script>

<template>
  <n-drawer :show="props.show" :width="480" @update:show="(v: boolean) => emit('update:show', v)">
    <n-drawer-content :title="isEdit ? `编辑数据源 · ${props.editing?.name}` : '新建数据源'" closable>
      <n-collapse :default-expanded-names="['conn']">
        <n-collapse-item title="连接信息" name="conn">
          <n-form label-placement="top" size="small">
            <n-form-item label="名称" required>
              <n-input v-model:value="form.name" placeholder="如：生产源库" />
            </n-form-item>
            <n-form-item label="主机" required>
              <n-input v-model:value="form.host" placeholder="127.0.0.1" />
            </n-form-item>
            <n-form-item label="端口" required>
              <n-input-number v-model:value="form.port" :min="1" :max="65535" style="width: 100%" />
            </n-form-item>
            <n-form-item label="用户名" required>
              <n-input v-model:value="form.username" placeholder="同步专用账号" />
            </n-form-item>
            <n-form-item :label="isEdit ? '密码（留空表示不变更）' : '密码'" required>
              <n-input v-model:value="form.password" type="password" show-password-on="click" placeholder="密码" />
            </n-form-item>
          </n-form>
        </n-collapse-item>
        <n-collapse-item title="SSL" name="ssl">
          <n-form label-placement="top" size="small">
            <n-form-item label="SSL 模式">
              <n-select v-model:value="form.ssl_mode" :options="sslOptions" />
            </n-form-item>
            <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-tertiary)">
              如需加密连接，请选择上方的 SSL 模式；CA / 双向证书文件导入暂未提供。
            </div>
          </n-form>
        </n-collapse-item>
        <n-collapse-item title="高级" name="adv">
          <n-form label-placement="top" size="small">
            <n-form-item label="默认数据库（留空 = 整实例）">
              <n-input v-model:value="form.default_schema" placeholder="留空表示整实例" />
            </n-form-item>
            <n-form-item label="字符集">
              <n-select v-model:value="form.charset" :options="charsetOptions" filterable tag
                placeholder="连接字符集（默认 utf8mb4）" />
            </n-form-item>
            <n-form-item label="连接超时（秒）">
              <n-input-number v-model:value="form.connect_timeout" :min="1" :max="60" style="width: 100%" />
            </n-form-item>
            <n-form-item label="备注">
              <n-input v-model:value="form.note" type="textarea" :rows="2" placeholder="用途说明" />
            </n-form-item>
          </n-form>
        </n-collapse-item>
      </n-collapse>
      <template #footer>
        <n-button @click="emit('update:show', false)">取消</n-button>
        <n-button type="primary" :loading="saving" @click="save" style="margin-left: 8px">保存</n-button>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
