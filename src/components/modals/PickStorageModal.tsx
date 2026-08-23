/**
 * PickStorageModal — install-time storage location selector.
 *
 * Lists every writable device the backend reports as a radio-style
 * row, plus a "Choose Another Location..." option that expands an
 * inline `StoragePathPicker`. When the user confirms a folder via
 * "Use This Folder", the picker collapses and the selected path
 * becomes a persistent radio option.  Clicking the confirmed row
 * re-opens the picker.
 */
import { FC, useCallback, useState } from "react";
import { ConfirmModal, Focusable, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { StoragePathPicker } from "./StoragePathPicker";
import type {
  StorageLocation,
  StorageLocationInfo,
} from "../../types/downloads";

/* ---- Props ---- */

interface Props {
  gameTitle: string;
  gameSizeBytes?: number;
  locations: StorageLocationInfo[];
  defaultLocation: StorageLocation;
  setCustomPath: (path: string) => Promise<boolean>;
  onConfirm: (storage: StorageLocation, customPath?: string) => void;
  onCancel: () => void;
}

/* ---- Helpers ---- */

function formatSize(bytes: number): string {
  const gb = bytes / 1e9;
  return `${gb.toFixed(1)} GB`;
}

/* ---- Radio row for a storage location ---- */

const LocationRow: FC<{
  selected: boolean;
  onSelect: () => void;
  label: string;
  path: string;
  freeGb: number;
}> = ({ selected, onSelect, label, path, freeGb }) => (
  <Focusable
    onActivate={onSelect}
    style={{
      display: "flex",
      alignItems: "flex-start",
      gap: 10,
      padding: "10px 12px",
      cursor: "pointer",
    }}
  >
    <div
      style={{
        width: 16,
        height: 16,
        borderRadius: "50%",
        border: `2px solid ${selected ? "#dadedf" : "#8f98a0"}`,
        background: selected ? "#dadedf" : "transparent",
        flexShrink: 0,
        marginTop: 2,
      }}
    />
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 600 }}>{label}</div>
      <div
        style={{
          fontSize: 11,
          color: "#8f98a0",
          wordBreak: "break-all",
          marginTop: 2,
        }}
      >
        {path}
      </div>
      {freeGb > 0 && (
        <div style={{ fontSize: 11, color: "#8f98a0", marginTop: 1 }}>
          {freeGb.toFixed(1)} GB free
        </div>
      )}
    </div>
  </Focusable>
);

/* ---- Component ---- */

export const PickStorageModal: FC<Props> = ({
  gameTitle,
  gameSizeBytes,
  locations,
  defaultLocation,
  setCustomPath,
  onConfirm,
  onCancel,
}) => {
  const { t } = useTranslation();

  const available = locations.filter((l) => l.available);
  /* Externals are every row that isn't internal storage or the custom
     path. When the list arrived and holds none, say so — a drive the
     backend failed to detect used to render as nothing at all, leaving
     the user staring at a picker that silently offered only internal
     storage. An empty list means the RPC failed, a different problem
     that this note must not claim to explain. */
  const noExternal =
    available.length > 0 &&
    !available.some((l) => l.id !== "internal" && l.id !== "custom");
  const [selectedId, setSelectedId] =
    useState<StorageLocation>(defaultLocation);
  const [customPathPicked, setCustomPathPicked] = useState<string | null>(null);
  const [customFreeGb, setCustomFreeGb] = useState<number>(0);
  const [pickExpanded, setPickExpanded] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleInstall = useCallback(async () => {
    setSaving(true);
    try {
      console.log(
        "[PickStorageModal] install: storage=%s customPath=%s",
        selectedId,
        customPathPicked ?? "none",
      );
      if (selectedId === "custom" && customPathPicked) {
        console.log(
          "[PickStorageModal] persisting custom path:",
          customPathPicked,
        );
        await setCustomPath(customPathPicked);
      }
      onConfirm(selectedId, customPathPicked ?? undefined);
    } finally {
      setSaving(false);
    }
  }, [selectedId, customPathPicked, setCustomPath, onConfirm]);

  const handleCustomPicked = (path: string, freeGb: number) => {
    setCustomPathPicked(path);
    setCustomFreeGb(freeGb);
    setPickExpanded(false);
    setSelectedId("custom");
  };

  return (
    <ConfirmModal
      strTitle={t("pickStorage.title")}
      strOKButtonText={saving ? t("common.working") : t("playButton.install")}
      strCancelButtonText={t("common.cancel")}
      bOKDisabled={saving || (selectedId === "custom" && !customPathPicked)}
      onOK={handleInstall}
      onCancel={onCancel}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          minWidth: 320,
        }}
      >
        {/* Game info header */}
        <div style={{ marginBottom: 4 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{gameTitle}</div>
          {gameSizeBytes != null && gameSizeBytes > 0 && (
            <div style={{ fontSize: 12, color: "#8f98a0", marginTop: 2 }}>
              {t("pickStorage.gameSize")}: {formatSize(gameSizeBytes)}
            </div>
          )}
        </div>

        {/* Predefined locations (internal, sdcard, etc.) */}
        {available.map((loc) => {
          if (loc.id === "custom") return null;
          return (
            <LocationRow
              key={loc.id}
              selected={selectedId === loc.id}
              onSelect={() => {
                setSelectedId(loc.id);
                setPickExpanded(false);
              }}
              label={loc.label}
              path={loc.path}
              freeGb={loc.free_space_gb}
            />
          );
        })}

        {noExternal && (
          <div style={{ fontSize: 12, color: "#8f98a0", padding: "0 12px" }}>
            {t("storageSettings.noExternalDrives")}
          </div>
        )}

        {/* Confirmed custom path (collapsed) — clicking reopens picker */}
        {customPathPicked && !pickExpanded && (
          <LocationRow
            selected={selectedId === "custom"}
            onSelect={() => {
              setSelectedId("custom");
              setPickExpanded(true);
              setCustomPathPicked(null);
            }}
            label={t("pickStorage.customLabel")}
            path={customPathPicked}
            freeGb={customFreeGb}
          />
        )}

        {/* Choose Another Location… (hidden when custom already confirmed) */}
        {!customPathPicked && (
          <Focusable
            onActivate={() => {
              setSelectedId("custom");
              setPickExpanded(true);
            }}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 10,
              padding: "10px 12px",
              cursor: "pointer",
            }}
          >
            <div
              style={{
                width: 16,
                height: 16,
                borderRadius: "50%",
                border: `2px solid ${
                  selectedId === "custom" ? "#dadedf" : "#8f98a0"
                }`,
                background: selectedId === "custom" ? "#dadedf" : "transparent",
                flexShrink: 0,
                marginTop: 2,
              }}
            />
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              {t("pickStorage.customOption")}
            </div>
          </Focusable>
        )}

        {/* Inline file picker (expanded) */}
        {selectedId === "custom" && pickExpanded && (
          <div
            style={{
              padding: "8px 8px 8px 26px",
              background: "rgba(0,0,0,0.15)",
              borderRadius: 6,
            }}
          >
            <StoragePathPicker
              startPath={
                locations.find((l) => l.id === "custom")?.path ?? "/home"
              }
              onConfirm={handleCustomPicked}
            />
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};

/* ---- Promise helper ---- */

export interface PickStorageResult {
  storage: StorageLocation;
  customPath?: string;
}

/**
 * Open the storage picker modal and resolve with the user's
 * choice, or `null` if they cancelled.
 *
 * ``onConfirm`` closes the modal and resolves with the pick.
 * ``onCancel`` (Cancel button / B key) resolves with ``null``.
 * A ``settled`` guard prevents double-resolution.
 */
export function pickStorageForInstall(
  gameTitle: string,
  gameSizeBytes: number | undefined,
  locations: StorageLocationInfo[],
  defaultLocation: StorageLocation,
  setCustomPath: (path: string) => Promise<boolean>,
): Promise<PickStorageResult | null> {
  return new Promise((resolve) => {
    let settled = false;
    const handle = showModal(
      <PickStorageModal
        gameTitle={gameTitle}
        gameSizeBytes={gameSizeBytes}
        locations={locations}
        defaultLocation={defaultLocation}
        setCustomPath={setCustomPath}
        onConfirm={(storage, customPath) => {
          if (settled) return;
          settled = true;
          handle?.Close();
          resolve({ storage, customPath });
        }}
        onCancel={() => {
          if (settled) return;
          settled = true;
          handle?.Close();
          resolve(null);
        }}
      />,
    );
  });
}
