/**
 * RPC route registry — single source of truth.
 *
 * Every route in this table is documented in the backend
 * operational plan PDF and registered in one of the RPC
 * mixins (Store, Sync, Download, Launch, Playtime, UI,
 * Action, CloudFailure, Observability, Account, Security,
 * ConfigValidation).
 *
 * Components import these constants so a backend rename is a
 * one-file change on the TS side ; raw string method names
 * never appear elsewhere.
 */
export const rpcRoutes = {
  // Store + auth (StoreRPCMixin)
  storeAuth: "store_auth",
  checkStoreStatus: "check_store_status",
  getStoreInfos: "get_store_infos",
  clearStoreAuths: "clear_store_auths",
  // Edge prereq (EdgeRPCMixin)
  isEdgeInstalled: "is_edge_installed",
  installEdge: "install_edge",
  // Auth-shortcut context (AuthShortcutsRPCMixin)
  getEpicAuthShortcutContext: "get_epic_auth_shortcut_context",
  getGogAuthShortcutContext: "get_gog_auth_shortcut_context",
  getAmazonAuthShortcutContext: "get_amazon_auth_shortcut_context",
  getMicrosoftAuthShortcutContext: "get_microsoft_auth_shortcut_context",
  getUbisoftAuthShortcutContext: "get_ubisoft_auth_shortcut_context",
  getBattlenetAuthShortcutContext: "get_battlenet_auth_shortcut_context",
  getCompatToolForGame: "get_compat_tool_for_game",
  saveProtonSetting: "save_proton_setting",
  // Library sync (SyncRPCMixin)
  syncLibraries: "sync_libraries",
  forceSyncLibraries: "force_sync_libraries",
  cancelSync: "cancel_sync",
  requestAuthSync: "request_auth_sync",
  getSyncStatus: "get_sync_status",
  getSyncProgress: "get_sync_progress",
  getAllUnifideckGames: "get_all_unifideck_games",
  getGameSizeBytes: "get_game_size_bytes",
  getInstalledDiskInfo: "get_installed_disk_info",
  updateSteamOwnedTitles: "update_steam_owned_titles",
  setActiveSteamUser: "set_active_steam_user",
  // Orphaned-shortcut sweep — boot-time cleanup (CleanupRPCMixin)
  scanOrphanedShortcuts: "scan_orphaned_shortcuts",
  // Downloads (DownloadRPCMixin)
  installGame: "install_game",
  uninstallGame: "uninstall_game",
  cancelDownload: "cancel_download",
  getDownloadQueue: "get_download_queue",
  clearDownloadHistory: "clear_download_history",
  checkGameUpdate: "check_game_update",
  getAvailableUpdates: "get_available_updates",
  updateGame: "update_game",
  // Game info / metadata (StoreRPCMixin)
  getGameInfo: "get_game_info",
  getGameMetadata: "get_game_metadata",
  getGameMetadataDisplay: "get_game_metadata_display",
  getStorageLocations: "get_storage_locations",
  getGogGameLanguages: "get_gog_game_languages",
  getEpicGameLanguages: "get_epic_game_languages",
  getProtondbCache: "get_protondb_cache",
  // Library facets — per-shortcut enrichment for native Sort/Filters
  // + shortcut-keyed Great-on-Deck compat (LibraryFacetsRPCMixin)
  getOverviewEnrichment: "get_overview_enrichment",
  // Steam Store spoofing (StoreRPCMixin)
  getRealSteamAppidMappings: "get_real_steam_appid_mappings",
  getSteamMetadataCache: "get_steam_metadata_cache",
  injectGameToAppinfo: "inject_game_to_appinfo",
  // Library cleanup (SyncRPCMixin)
  performFullCleanup: "perform_full_cleanup",
  // UI helpers (UIRPCMixin)
  injectHideCss: "inject_hide_css",
  setLanguagePreference: "set_language_preference",
  getLanguagePreference: "get_language_preference",
  getDeviceType: "get_device_type",
  setDefaultStorageLocation: "set_default_storage_location",
  setCustomInstallPath: "set_custom_install_path",
  listDirectory: "list_directory",
  createDirectory: "create_directory",
  getBrowseableDevices: "get_browseable_devices",
  // Playtime (PlaytimeRPCMixin)
  notifyGameLaunched: "notify_game_launched",
  notifyGameStopped: "notify_game_stopped",
  getPlaytime: "get_playtime",
  getAllPlaytimes: "get_all_playtimes",
  syncPlaytimeNow: "sync_playtime_now",
  // Action dispatcher (ActionRPCMixin) — bidirectional bridge
  dispatchUnifideckAction: "dispatch_unifideck_action",
  // Cloud-save behaviour preferences (CloudFailureRPCMixin)
  setCloudFailureBehavior: "set_cloud_failure_behavior",
  getCloudFailureBehaviors: "get_cloud_failure_behaviors",
  // Manual cloud-save status / pull / push (CloudSaveRPCMixin)
  getCloudSaveStatus: "get_cloud_save_status",
  cloudSavePull: "cloud_save_pull",
  cloudSavePush: "cloud_save_push",
  setGameSavePath: "set_game_save_path",
  // Launch executable override (ExecutableRPCMixin)
  listGameExecutables: "list_game_executables",
  setGameExecutable: "set_game_executable",
  resetGameExecutable: "reset_game_executable",
  // Achievements (AchievementsRPCMixin) — GOG display + last-session summary
  getGameAchievements: "get_game_achievements",
  getLastSessionAchievements: "get_last_session_achievements",
  // Observability (ObservabilityRPCMixin) — event bridge + diagnostics
  subscribeReplay: "subscribe_replay",
  getLauncherToasts: "get_launcher_toasts",
  getBusHealth: "get_bus_health",
  getPluginMetrics: "get_plugin_metrics",
  getFeatureFlags: "get_feature_flags",
  captureLogs: "capture_logs",
  // Account switch + migration (AccountRPCMixin)
  checkAccountSwitch: "check_account_switch",
  migrateAccountData: "migrate_account_data",
  // Plugin self-update (UpdaterRPCMixin)
  checkPluginUpdate: "check_plugin_update",
  getAvailableVersions: "get_available_versions",
  forceCheckPluginUpdate: "force_check_plugin_update",
  forceGetAvailableVersions: "force_get_available_versions",
  getReleaseNotes: "get_release_notes",
  logUpdateEvent: "log_update_event",
} as const;

/**
 * String key identifying an RPC route. Always
 * derived from `rpcRoutes` so renaming a backend
 * method causes a TypeScript error here, not a
 * runtime 404.
 */
export type RouteName = (typeof rpcRoutes)[keyof typeof rpcRoutes];

/** Defensive predicate — used by tests and the RPC wrapper
 *  to detect typos when dynamically composing route names. */
export function isKnownRoute(name: string): name is RouteName {
  for (const v of Object.values(rpcRoutes)) {
    if (v === name) return true;
  }
  return false;
}

/** Backend action verbs accepted by `dispatch_unifideck_action`.
 *  The URI form is `unifideck://<verb>[/arg1/arg2...]`. */
export const ActionVerbs = {
  AUTH: "auth",
  RETRY_SYNC: "retry-sync",
  REFRESH_LIBRARY: "refresh-library",
  REFRESH_ALL_LIBRARIES: "refresh-all-libraries",
} as const;

/**
 * Verb accepted by the generic `store_auth` RPC.
 * Replaces the 14 per-store auth methods of the
 * current architecture with a single dispatcher.
 */
export type ActionVerb = (typeof ActionVerbs)[keyof typeof ActionVerbs];
