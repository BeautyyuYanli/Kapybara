<script setup lang="ts">
import {
  AlertDialogRoot,
  AlertDialogTrigger,
  AlertDialogPortal,
  AlertDialogOverlay,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogCancel,
} from "reka-ui";
import { ref } from "vue";
const props = defineProps<{ name: string; provider?: boolean; remove: () => Promise<boolean> }>();
const open = ref(false);
const pending = ref(false);
async function confirm() {
  pending.value = true;
  try {
    if (await props.remove()) open.value = false;
  } finally {
    pending.value = false;
  }
}
</script>
<template>
  <AlertDialogRoot v-model:open="open">
    <AlertDialogTrigger class="danger">删除</AlertDialogTrigger>
    <AlertDialogPortal>
      <AlertDialogOverlay class="overlay" />
      <AlertDialogContent
        class="dialog"
        :aria-busy="pending"
        @escape-key-down="pending && $event.preventDefault()"
      >
        <AlertDialogTitle>删除 {{ name }}？</AlertDialogTitle>
        <AlertDialogDescription>{{
          provider
            ? "将一并移除其本地模型配置。被 Session 引用的配置无法删除。"
            : "将移除此本地模型配置。被 Session 引用时无法删除。"
        }}</AlertDialogDescription>
        <slot />
        <div class="actions">
          <AlertDialogCancel :disabled="pending">返回</AlertDialogCancel>
          <button class="danger" :disabled="pending" @click="confirm">
            {{ pending ? "删除中…" : "确认删除" }}
          </button>
        </div>
      </AlertDialogContent>
    </AlertDialogPortal>
  </AlertDialogRoot>
</template>
