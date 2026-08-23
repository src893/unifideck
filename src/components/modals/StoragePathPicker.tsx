/**
 * StoragePathPicker — full-featured directory browser.
 *
 * Provides device quick-access dropdown (from `get_browseable_devices`
 * RPC — actual mount points, not game subdirectories), Up navigation,
 * show-hidden toggle, 8-option sort dropdown, new-folder creation,
 * and a scrollable directory listing with folder icons.
 *
 * Backed by `list_directory`, `create_directory`, and
 * `get_browseable_devices` RPCs. Device list is fetched internally
 * so the picker works standalone inside `showModal` portals.
 */
import { FC, useCallback, useEffect, useRef, useState } from "react";
import {
  DialogButton,
  Dropdown,
  DropdownOption,
  Field,
  Focusable,
  Spinner,
  TextField,
  Toggle,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useRPC } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";

/** Props. */
interface Props {
  startPath: string;
  onConfirm: (path: string, freeSpaceGb: number) => void;
}

/** get_browseable_devices response item. */
interface BrowseableDevice {
  id: string;
  label: string;
  path: string;
  free_space_gb: number;
}

/** get_browseable_devices response envelope (unwrapped by useRPC). */
interface BrowseableDevicesResponse {
  devices: BrowseableDevice[];
}

/** list_directory response (unwrapped by useRPC). */
interface ListResponse {
  path: string;
  directories: string[];
}

/** create_directory response (unwrapped by useRPC). */
interface CreateDirResponse {
  path: string;
}

/* ---- inline SVG icons ---- */

const FolderIcon: FC = () => (
  <svg
    viewBox="0 0 24 24"
    fill="currentColor"
    width="1em"
    height="1em"
    style={{ flexShrink: 0, opacity: 0.7 }}
  >
    <path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z" />
  </svg>
);

const UpArrowIcon: FC = () => (
  <svg viewBox="0 0 24 24" fill="currentColor" width="1em" height="1em">
    <path d="M4 12l1.41 1.41L11 7.83V20h2V7.83l5.58 5.59L20 12l-8-8-8 8z" />
  </svg>
);

const SORT_OPTIONS: DropdownOption[] = [
  { data: "name", label: "" },
  { data: "name_desc", label: "" },
  { data: "modified_newest", label: "" },
  { data: "modified_oldest", label: "" },
  { data: "created_newest", label: "" },
  { data: "created_oldest", label: "" },
  { data: "size_largest", label: "" },
  { data: "size_smallest", label: "" },
];

const SORT_I18N: Record<string, string> = {
  name: "storageSettings.sortAZ",
  name_desc: "storageSettings.sortZA",
  modified_newest: "storageSettings.sortModifiedNewest",
  modified_oldest: "storageSettings.sortModifiedOldest",
  created_newest: "storageSettings.sortCreatedNewest",
  created_oldest: "storageSettings.sortCreatedOldest",
  size_largest: "storageSettings.sortSizeLargest",
  size_smallest: "storageSettings.sortSizeSmallest",
};

/**
 * Full-featured directory navigator. Fetches browseable device
 * mount points internally so the picker works standalone inside
 * `showModal` portals.
 */
export const StoragePathPicker: FC<Props> = ({ startPath, onConfirm }) => {
  const { t } = useTranslation();

  const listDir = useRPC<[string, boolean, string], ListResponse>(
    rpcRoutes.listDirectory,
  );
  const createDir = useRPC<[string], CreateDirResponse>(
    rpcRoutes.createDirectory,
  );
  const getDevices = useRPC<[], BrowseableDevicesResponse>(
    rpcRoutes.getBrowseableDevices,
  );

  const [path, setPath] = useState(startPath);
  const [dirs, setDirs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [showHidden, setShowHidden] = useState(false);
  const [sortBy, setSortBy] = useState<string>("name");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [unsupported, setUnsupported] = useState(false);

  /* Browseable devices for the quick-access dropdown. */
  const [devices, setDevices] = useState<BrowseableDevice[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(true);

  /* Refs for stable fetchDirectory closure. */
  const showHiddenRef = useRef(showHidden);
  const sortByRef = useRef(sortBy);
  showHiddenRef.current = showHidden;
  sortByRef.current = sortBy;

  /* Fetch browseable devices once on mount. */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await getDevices();
        if (!cancelled) setDevices(r.devices);
      } catch {
        console.warn("[StoragePathPicker] get_browseable_devices unavailable");
      } finally {
        if (!cancelled) setDevicesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [getDevices]);

  const fetchDirectory = useCallback(
    async (target: string) => {
      setLoading(true);
      try {
        const r = await listDir(
          target,
          showHiddenRef.current,
          sortByRef.current,
        );
        setPath(r.path);
        setDirs(r.directories);
        setUnsupported(false);
      } catch {
        console.warn("[StoragePathPicker] list_directory unavailable");
        setUnsupported(true);
      } finally {
        setLoading(false);
      }
    },
    [listDir],
  );

  /* Initial load + reload on toggle/sort changes. */
  useEffect(() => {
    void fetchDirectory(path);
  }, [fetchDirectory, showHidden, sortBy]); // eslint-disable-line react-hooks/exhaustive-deps

  const goUp = () => {
    if (path === "/") return;
    const parent = path.substring(0, path.lastIndexOf("/")) || "/";
    void fetchDirectory(parent);
  };

  const navigateTo = (dir: string) => {
    const next = path === "/" ? `/${dir}` : `${path}/${dir}`;
    void fetchDirectory(next);
  };

  const handleCreateFolder = async () => {
    const name = newFolderName.trim();
    if (!name) return;
    const fullPath = `${path}/${name}`;
    try {
      await createDir(fullPath);
      setNewFolderName("");
      setCreatingFolder(false);
      await fetchDirectory(fullPath);
    } catch {
      // useRPC surfaces the error — no separate toast needed here.
    }
  };

  /* Device dropdown options — actual mount points from /proc/mounts. */
  const deviceOptions: DropdownOption[] = devices.map((d) => ({
    data: d.path,
    label:
      d.free_space_gb != null
        ? t("storageSettings.freeSpaceLabel", {
            label: d.label,
            freeSpace: d.free_space_gb,
          })
        : d.label,
  }));

  const currentDevicePath = devices.find(
    (d) => path === d.path || path.startsWith(d.path + "/"),
  )?.path;

  const currentFreeSpace =
    devices.find((d) => path === d.path || path.startsWith(d.path + "/"))
      ?.free_space_gb ?? 0;

  /* Sort options with translated labels. */
  const sortOptions: DropdownOption[] = SORT_OPTIONS.map((o) => ({
    data: o.data,
    label: t(SORT_I18N[o.data as string] ?? o.data),
  }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {/* Device quick-access + Up */}
      <Focusable style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {devicesLoading ? (
          <div style={{ flex: 1, minWidth: 0, padding: "6px 0" }}>
            <Spinner />
          </div>
        ) : deviceOptions.length > 0 ? (
          <div style={{ flex: 1, minWidth: 0 }}>
            <Dropdown
              rgOptions={deviceOptions}
              selectedOption={currentDevicePath}
              onChange={(opt) => {
                void fetchDirectory(opt.data as string);
              }}
              strDefaultLabel={t("storageSettings.selectDevice")}
            />
          </div>
        ) : null}
        <DialogButton
          onClick={goUp}
          disabled={path === "/"}
          style={{
            minWidth: 48,
            width: 48,
            height: 40,
            padding: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <UpArrowIcon />
        </DialogButton>
      </Focusable>

      {/* The device list always carries internal storage, so "loaded,
          but nothing except internal" is exactly an undetected drive —
          worth saying out loud rather than showing a one-entry
          dropdown. An empty list means the RPC itself failed, which is
          a different problem and gets no such claim. */}
      {!devicesLoading &&
        devices.length > 0 &&
        !devices.some((d) => d.id !== "internal") && (
          <div style={{ fontSize: 12, color: "#8f98a0" }}>
            {t("storageSettings.noExternalDrives")}
          </div>
        )}

      {/* Current path breadcrumb */}
      <div
        style={{
          fontSize: 12,
          color: "#b0b0b0",
          wordBreak: "break-all",
          padding: "4px 0",
        }}
      >
        {path}
      </div>

      {/* Toolbar: show hidden + sort */}
      <Focusable style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Focusable
          onActivate={() => setShowHidden(!showHidden)}
          onClick={() => setShowHidden(!showHidden)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            cursor: "pointer",
          }}
        >
          <Toggle
            value={showHidden}
            onChange={(checked) => setShowHidden(checked)}
          />
          <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>
            {t("storageSettings.showHidden")}
          </span>
        </Focusable>
        <div style={{ minWidth: 140, flexShrink: 0 }}>
          <Dropdown
            rgOptions={sortOptions}
            selectedOption={sortBy}
            onChange={(opt) => setSortBy(opt.data as string)}
          />
        </div>
      </Focusable>

      {/* Directory listing */}
      <Focusable
        style={{
          maxHeight: 280,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 2,
        }}
      >
        {loading && dirs.length === 0 && (
          <div style={{ color: "#666", fontSize: 12, padding: 8 }}>
            {t("common.loading")}
          </div>
        )}
        {!loading && dirs.length === 0 && !unsupported && (
          <div style={{ color: "#666", fontSize: 12, padding: 8 }}>
            {t("storageSettings.emptyDirectory")}
          </div>
        )}
        {unsupported && (
          <Field
            label={t("storage.customPath")}
            description={path}
            childrenContainerWidth="fixed"
          />
        )}
        {dirs.map((d) => (
          <DialogButton
            key={d}
            onClick={() => navigateTo(d)}
            style={{
              width: "100%",
              textAlign: "left",
              fontSize: 13,
              padding: "8px 12px",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <FolderIcon />
              <span>{d}</span>
            </div>
          </DialogButton>
        ))}
      </Focusable>

      {/* New folder inline input */}
      {creatingFolder && (
        <Focusable style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <div style={{ flex: 1 }}>
            <TextField
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.currentTarget.value)}
              focusOnMount
            />
          </div>
          <DialogButton
            onClick={handleCreateFolder}
            disabled={!newFolderName.trim()}
            style={{
              minWidth: 48,
              width: 48,
              height: 40,
              padding: 0,
              fontSize: 12,
            }}
          >
            {t("storageSettings.newFolderCreate")}
          </DialogButton>
          <DialogButton
            onClick={() => {
              setCreatingFolder(false);
              setNewFolderName("");
            }}
            style={{
              minWidth: 48,
              width: 48,
              height: 40,
              padding: 0,
              fontSize: 12,
            }}
          >
            {t("common.cancel")}
          </DialogButton>
        </Focusable>
      )}

      {/* Action buttons */}
      <Focusable style={{ display: "flex", gap: 8 }}>
        <DialogButton
          onClick={() => onConfirm(path, currentFreeSpace)}
          style={{ flex: 1, fontSize: 13 }}
        >
          {t("storageSettings.selectFolder")}
        </DialogButton>
        {!creatingFolder && (
          <DialogButton
            onClick={() => setCreatingFolder(true)}
            style={{ flex: 1, fontSize: 13 }}
          >
            {t("storageSettings.newFolder")}
          </DialogButton>
        )}
      </Focusable>
    </div>
  );
};
