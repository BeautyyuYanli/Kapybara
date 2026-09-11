<script setup lang="ts">
import { computed } from "vue";
import FormField from "./FormField.vue";
import { parseObject } from "@/forms";
const text = defineModel<string>({ required: true });
const props = defineProps<{ id: string; label: string; error?: string }>();
const problem = computed(() => {
  try {
    parseObject(text.value);
    return props.error;
  } catch {
    return "请输入有效的 JSON 对象，例如 {}；不接受数组或 null。";
  }
});
function format() {
  try {
    text.value = JSON.stringify(parseObject(text.value), null, 2);
  } catch {
    /* Keep the draft for correction. */
  }
}
</script>
<template>
  <FormField
    :id="id"
    :label="label"
    :error="problem"
    help="保存时整体替换，{} 表示清空。"
    v-slot="field"
  >
    <textarea
      v-model="text"
      :id="field.id"
      :aria-describedby="field.describedby"
      :aria-invalid="field.invalid"
      rows="7"
      spellcheck="false"
    />
    <button type="button" @click="format">格式化 JSON</button>
  </FormField>
</template>
