import { ref, shallowRef, watch, onScopeDispose, type WatchSource } from "vue";
import { describeError } from "@/api/errors";
/** A paginated select directory. Selected values are rendered separately by the form. */
export function useOptions<T>(
  source: WatchSource,
  read: (offset: number, signal: AbortSignal) => Promise<{ items: T[]; has_more: boolean }>,
) {
  const items = shallowRef<T[]>([]);
  const hasMore = ref(false),
    pending = ref(false),
    error = ref("");
  let controller: AbortController | undefined;
  let generation = 0;
  async function more(reset = false) {
    if (pending.value && !reset) return;
    if (reset) {
      controller?.abort();
      items.value = [];
      hasMore.value = false;
    }
    controller = new AbortController();
    const current = ++generation;
    pending.value = true;
    error.value = "";
    try {
      const page = await read(items.value.length, controller.signal);
      if (current === generation) {
        items.value = [...items.value, ...page.items];
        hasMore.value = page.has_more;
      }
    } catch (failure) {
      if (current === generation && !controller.signal.aborted)
        error.value = describeError(failure).message;
    } finally {
      if (current === generation) pending.value = false;
    }
  }
  watch(source, () => more(true), { immediate: true });
  onScopeDispose(() => {
    ++generation;
    controller?.abort();
  });
  return { items, hasMore, pending, error, more };
}
