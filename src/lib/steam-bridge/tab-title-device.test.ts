// @vitest-environment jsdom
/**
 * The compatibility tab is titled after the hardware it filters for.
 *
 * Two things must hold: a Steam Machine owner is never told their games
 * are "Great on Deck", and a backend that cannot answer leaves the
 * pre-existing Deck label rather than blanking the tab. The second is
 * the one that would ship silently broken, because on the dev Deck the
 * default and the correct answer are the same string.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@decky/ui", () => ({ gamepadTabbedPageClasses: undefined }));
vi.mock("i18next", () => ({ default: { t: (key: string) => key } }));
vi.mock("../library-filters", () => ({
  runFilters: () => true,
  setStoreCountSink: () => {},
}));
vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) =>
    raw && typeof raw === "object" && "success" in raw ? raw : undefined,
}));

import { call } from "@decky/api";
import { getUnifideckTabs } from "./tab-container";
import {
  loadDeviceType,
  getDeviceType,
  __setDeviceTypeForTests,
  type DeviceType,
} from "../device-type";

const compatTabTitle = (): string =>
  getUnifideckTabs().find((t) => t.id === "unifideck-deck")!.title;

beforeEach(() => {
  vi.mocked(call).mockReset();
  __setDeviceTypeForTests("deck");
});

describe("compatibility tab title", () => {
  it.each([
    ["deck", "deckTabs.greatOnDeck"],
    ["machine", "deckTabs.greatOnMachine"],
    ["other", "deckTabs.steamOSCompatible"],
  ] as [DeviceType, string][])("titles %s as %s", (device, key) => {
    __setDeviceTypeForTests(device);
    expect(compatTabTitle()).toBe(key);
  });

  it("keeps the tab id and filter fixed so no layout moves", () => {
    __setDeviceTypeForTests("machine");
    const tab = getUnifideckTabs().find((t) => t.id === "unifideck-deck")!;
    expect(tab.position).toBe(0);
    expect(tab.filters).toEqual([{ type: "deckCompat", params: {} }]);
  });
});

describe("loadDeviceType", () => {
  it("caches a valid answer and reports the change", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "machine" });
    await expect(loadDeviceType()).resolves.toBe(true);
    expect(getDeviceType()).toBe("machine");
    expect(compatTabTitle()).toBe("deckTabs.greatOnMachine");
  });

  it("reports no change when the answer matches the default", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "deck" });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });

  it("ignores a value outside the known set", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "toaster" });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });

  it("keeps the default when an older backend has no such route", async () => {
    vi.mocked(call).mockRejectedValue(new Error("unknown method"));
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(compatTabTitle()).toBe("deckTabs.greatOnDeck");
  });

  it("keeps the default when the payload omits the field", async () => {
    vi.mocked(call).mockResolvedValue({ success: true });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });
});
