import { ref, shallowRef, watch, onScopeDispose, type WatchSource } from "vue";
import { describeError } from "@/api/errors";

/** Each read owns a generation: aborted or late responses cannot replace newer state. */
export function useResource<T>(source: WatchSource, read: (signal: AbortSignal) => Promise<T>) {
  const data = shallowRef<T>();
  const pending = ref(false);
  const error = ref("");
  let controller: AbortController | undefined;
  let generation = 0;
  async function refresh() {
    controller?.abort();
    const current = ++generation;
    controller = new AbortController();
    pending.value = true;
    error.value = "";
    data.value = undefined;
    try {
      const result = await read(controller.signal);
      if (current === generation) data.value = result;
    } catch (failure) {
      if (current === generation && !controller.signal.aborted)
        error.value = describeError(failure).message;
    } finally {
      if (current === generation) pending.value = false;
    }
  }
  watch(source, refresh, { immediate: true });
  onScopeDispose(() => {
    ++generation;
    controller?.abort();
  });
  return { data, pending, error, refresh };
}
