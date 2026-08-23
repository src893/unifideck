/**
 * Which Valve device this is, for labelling the compatibility tab.
 *
 * The backend answers from DMI (`utils/device.py`) because nothing
 * reachable from the frontend discriminates a Deck from a Steam
 * Machine: both run SteamOS, and Steam launches with the same
 * `-steamdeck -steamos3` flags on either.
 *
 * Cached module-level after the single startup fetch. The value cannot
 * change without a reboot, so re-asking would only add a round trip.
 * The default is `"deck"` — the label every user saw before this
 * existed — so a backend that never answers degrades to the previous
 * behaviour rather than to a blank or a wrong name.
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";

export type DeviceType = "deck" | "machine" | "other";

/** The `data` payload of `get_device_type`, envelope already stripped. */
interface DeviceTypePayload {
  device_type: DeviceType;
}

const VALID: readonly DeviceType[] = ["deck", "machine", "other"];

let deviceType: DeviceType = "deck";

export function getDeviceType(): DeviceType {
  return deviceType;
}

/** Test seam — reset the cache between cases. */
export function __setDeviceTypeForTests(value: DeviceType): void {
  deviceType = value;
}

/**
 * Fetch the device type once and cache it.
 *
 * @returns true if the cached value actually changed, so the caller
 *   can decide whether a re-render is warranted rather than firing one
 *   unconditionally on every boot.
 */
export async function loadDeviceType(): Promise<boolean> {
  try {
    const raw = await call<[], unknown>(rpcRoutes.getDeviceType);
    const r = unwrapRpcEnvelope<DeviceTypePayload>(raw, {
      route: rpcRoutes.getDeviceType,
      throwing: false,
    });
    const next = r?.device_type;
    // Validate rather than trust: an older backend answers this route
    // with an error envelope, and assigning undefined here would blank
    // the tab title instead of leaving the default in place.
    if (!next || !VALID.includes(next)) return false;
    if (next === deviceType) return false;
    deviceType = next;
    return true;
  } catch {
    // Backend not ready — the "deck" default stays.
    return false;
  }
}
