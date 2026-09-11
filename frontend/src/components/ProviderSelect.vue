<script setup lang="ts">
import { listProviders } from "@/api/generated";
import { useOptions } from "@/composables/useOptions";
const value = defineModel<string>({ required: true });
defineProps<{
  id: string;
  required?: boolean;
  describedby?: string;
  invalid?: boolean;
}>();
const { items, pending, hasMore, error, more } = useOptions(
  () => true,
  async (offset, signal) =>
    (await listProviders({ query: { offset, limit: 25 }, signal, throwOnError: true })).data,
);
</script>
<template>
  <select
    :id="id"
    v-model="value"
    :required="required"
    :aria-describedby="describedby"
    :aria-invalid="invalid"
  >
    <option value="">{{ required ? "选择 Provider" : "全部 Provider" }}</option>
    <option v-if="value && !items.some((item) => item.id === value)" :value="value">
      {{ value }}（当前选择）
    </option>
    <option v-for="item in items" :key="item.id" :value="item.id">{{ item.name }}</option>
  </select>
  <small v-if="error" role="alert" class="error">{{ error }}</small>
  <button
    v-if="hasMore || error || pending"
    type="button"
    :disabled="pending"
    @click="more()"
  >
    {{ pending ? "加载中…" : error ? "重试加载 Provider" : "加载更多 Provider" }}
  </button>
</template>
