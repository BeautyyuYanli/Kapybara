import { describe, expect, it } from "vitest";
import { effectScope, nextTick, ref } from "vue";
import { useResource } from "@/composables/useResource";
import { ApiError, describeError } from "@/api/errors";
import { parseObject, optionalInteger } from "@/forms";

describe("read lifetime", () => {
  it("aborts prior reads and ignores stale responses, including after disposal", async () => {
    const key = ref(0),
      signals: AbortSignal[] = [],
      resolves: Array<(value: string) => void> = [];
    const scope = effectScope();
    const resource = scope.run(() =>
      useResource(key, (signal) => {
        signals.push(signal);
        return new Promise<string>((resolve) => resolves.push(resolve));
      }),
    )!;
    key.value = 1;
    await nextTick();
    expect(signals[0]!.aborted).toBe(true);
    resolves[1]!("new");
    await nextTick();
    resolves[0]!("old");
    await nextTick();
    expect(resource.data.value).toBe("new");
    key.value = 2;
    await nextTick();
    scope.stop();
    expect(signals[2]!.aborted).toBe(true);
    resolves[2]!("unmounted");
    await nextTick();
    expect(resource.data.value).toBeUndefined();
  });
  it("shows read failure separately from an empty result", async () => {
    const scope = effectScope();
    const resource = scope.run(() =>
      useResource(
        () => 0,
        async () => {
          throw new ApiError(404, null);
        },
      ),
    )!;
    await nextTick();
    expect(resource.error.value).toBe("资源不存在。");
    expect(resource.data.value).toBeUndefined();
    expect(resource.pending.value).toBe(false);
    scope.stop();
  });
});
it("validates JSON object and integer clearing semantics", () => {
  expect(parseObject("{}")).toEqual({});
  for (const value of ["null", "[]", '"text"', "", "{"]) expect(() => parseObject(value)).toThrow();
  expect(optionalInteger("")).toBeNull();
  expect(optionalInteger("0", 0)).toBe(0);
  expect(optionalInteger(0, 0)).toBe(0);
  for (const value of ["-1", "1.5", "Infinity"]) expect(() => optionalInteger(value)).toThrow();
});
it("maps field validation and keeps unknown errors generic", () => {
  expect(
    describeError(
      new ApiError(422, [{ loc: ["body", "name"], msg: "Required", type: "missing" }]),
      ["name"],
    ).fields,
  ).toEqual({ name: "Required" });
  expect(describeError(new ApiError(500, "<script>secret</script>")).message).not.toContain(
    "secret",
  );
});
